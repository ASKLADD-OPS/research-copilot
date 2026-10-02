"""版面分析：阅读顺序（XY-Cut）、标题树（H1/H2/H3）、段落合并。

双栏 PDF 的"文本流"是坏的：PyMuPDF 按内容流给块，左栏第 1 段后面跟着的是
**右栏第 1 段**，而不是左栏第 2 段。直接拼出来的正文语义错乱 —— 检索会召回
半句话，引用定位会指到隔壁栏，NLI 校验也会跟着判错。

本模块只做一件事：把行级块重排成"人眼读的顺序"，并把切碎的行使者并回段落。

分栏判定刻意用**启发式**而不是上 LayoutParser / 模型：学术论文的双栏是规矩的
等宽两栏，x 中心一分为二就够；为此拖进一个版面模型（几百 MB 权重 + 一次前向）
换来的准确率提升，在"反正是给人读的正文流"这件事上不值。
`ponytail:` 真正的 XY-Cut 递归切分是升级路径：块布局不是规整两栏时（三栏、
栏内插图打乱）现在的判据会退化成单栏，结果只是顺序差，不会丢内容。
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.parsers.pdf import Block

#: x 中心落在它左边的算左栏、右边的算右栏
COLUMN_SPLIT_X = 0.5
#: 横跨页宽 70% 以上 → 整幅块（大标题/跨栏表格/整幅图），不参与分栏
SPANNER_MIN_WIDTH = 0.7
#: 每栏至少这么多块才敢判双栏（块太少时 x 分布本身就没意义）
COLUMN_MIN_BLOCKS = 3
#: 栏内相邻行的垂直间隙上限（占页高）。正文行距约 0.1%~0.4%，段间距约 1%~1.4%，
#: 0.8% 落在两者中间 —— 大了会把段间并掉，小了会把段落切碎。
MERGE_GAP_RATIO = 0.008
#: 同一段的行左边界应当基本对齐（首行缩进吃掉的量）
MERGE_X_TOL = 0.035
#: 字号超过正文中位数的这个倍数 → 判标题
HEADING_SIZE_RATIO = 1.12
HEADING_MAX_CHARS = 120
#: 标题层级最多到 H3（H4 往下对章节切分没有意义，只会把正文切碎）
MAX_HEADING_LEVEL = 3

_SENTENCE_END = ("。", "！", "？", ".", "!", "?", "：", ":", "；", ";")
_HYPHEN_END_RE = re.compile(r"[A-Za-z]-$")
_LATIN_TAIL_RE = re.compile(r"[A-Za-z0-9]$")
_LATIN_HEAD_RE = re.compile(r"^[A-Za-z0-9]")


def join_lines(left: str, right: str) -> str:
    """按边界字符决定两行之间补什么：英文补空格、中文直接接、连字符断词要复原。

    `informa-` + `tion` → `information`（不补空格、去掉连字符）是这里唯一
    容易做错的点：少了它，"information" 在全文里永远搜不到。
    """
    a = left.rstrip()
    b = right.lstrip()
    if not a:
        return b
    if not b:
        return a
    if _HYPHEN_END_RE.search(a):
        return a[:-1] + b
    if _LATIN_TAIL_RE.search(a) and _LATIN_HEAD_RE.match(b):
        return f"{a} {b}"
    return a + b


# ---------------------------------------------------------------- 坐标小工具
def _cx(block: Block) -> float:
    return (block.bbox[0] + block.bbox[2]) / 2 if block.bbox else 0.5


def _cy(block: Block) -> float:
    return (block.bbox[1] + block.bbox[3]) / 2 if block.bbox else 0.5


def _reading_key(block: Block) -> tuple[float, float]:
    """单栏内的阅读序：先上后下，同一行再左到右。"""
    if not block.bbox:
        return (0.0, 0.0)
    return (block.bbox[1], block.bbox[0])


# ---------------------------------------------------------------- 分栏
def detect_columns(blocks: list[Block]) -> int:
    """判 1 栏还是 2 栏。返回 1 或 2。"""
    boxes = [b for b in blocks if b.bbox]
    if len(boxes) < COLUMN_MIN_BLOCKS * 2:
        return 1
    plain = [b for b in boxes if b.width < SPANNER_MIN_WIDTH]
    if len(plain) < COLUMN_MIN_BLOCKS * 2:
        # 整幅块占多数：剩下的块不足以判断分栏（常见于首页大标题 + 少量正文）
        plain = boxes
    left = sum(1 for b in plain if _cx(b) < COLUMN_SPLIT_X - 0.05)
    right = sum(1 for b in plain if _cx(b) > COLUMN_SPLIT_X + 0.05)
    middle = len(plain) - left - right
    # 跨中线的块（居中大标题）多起来就不是两栏了
    if left >= COLUMN_MIN_BLOCKS and right >= COLUMN_MIN_BLOCKS and middle * 3 <= len(plain):
        return 2
    return 1


def order_blocks(blocks: list[Block]) -> list[Block]:
    """重排为阅读顺序。**没有坐标时原样返回** —— 无从判断就别乱动。"""
    blocks = [b for b in blocks if b.text.strip()]
    if not any(b.bbox for b in blocks):
        return list(blocks)
    if detect_columns(blocks) == 1:
        return sorted(blocks, key=_reading_key)

    # 双栏：整幅块（大标题、跨栏表格）把页面切成若干"带"，带内先左栏后右栏
    spanners = sorted((b for b in blocks if b.width >= SPANNER_MIN_WIDTH), key=_cy)
    remaining = [b for b in blocks if b.width < SPANNER_MIN_WIDTH]
    out: list[Block] = []
    cursor = float("-inf")
    for spanner in spanners:
        band = [b for b in remaining if cursor <= _cy(b) < _cy(spanner)]
        out.extend(_two_column(band))
        out.append(spanner)
        used = {id(b) for b in band}
        remaining = [b for b in remaining if id(b) not in used]
        cursor = _cy(spanner)
    out.extend(_two_column(remaining))
    return out


def _two_column(band: list[Block]) -> list[Block]:
    """带内：左栏整体在前，右栏在后（栏内各按阅读序）。"""
    left = sorted((b for b in band if _cx(b) <= COLUMN_SPLIT_X), key=_reading_key)
    right = sorted((b for b in band if _cx(b) > COLUMN_SPLIT_X), key=_reading_key)
    return left + right


# ---------------------------------------------------------------- 标题树
def build_heading_tree(blocks: list[Block]) -> list[Block]:
    """按字号给标题分级：最大字号 → H1，次之 H2，其余 H3（最多 3 级）。

    没有字号信息（MinerU / 纯文本后端）时原样返回 —— 那些后端的章节判定靠
    `pdf.section_of()` 的正则，不靠字号。
    """
    sized = [b.size for b in blocks if b.size > 0]
    if not sized:
        return list(blocks)
    body = _median(sized)

    marked: list[Block] = []
    for block in blocks:
        if block.kind == "title" or _looks_like_title(block, body):
            marked.append(replace(block, kind="title", level=0))
        else:
            marked.append(block)

    sizes = sorted({round(b.size, 1) for b in marked if b.kind == "title" and b.size > 0}, reverse=True)
    level_of = {size: min(idx + 1, MAX_HEADING_LEVEL) for idx, size in enumerate(sizes)}
    return [
        replace(b, level=level_of.get(round(b.size, 1), MAX_HEADING_LEVEL)) if b.kind == "title" else b for b in marked
    ]


def _looks_like_title(block: Block, body_size: float) -> bool:
    if block.size < body_size * HEADING_SIZE_RATIO:
        return False
    text = block.text.strip()
    if not text or len(text) > HEADING_MAX_CHARS:
        return False
    # 句子（带句末标点）不是标题 —— 字号大只说明它是引文/结论块
    return not text.endswith(_SENTENCE_END)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


# ---------------------------------------------------------------- 段落合并
def merge_paragraphs(blocks: list[Block]) -> list[Block]:
    """把被版面切碎的同段行并回一段。

    只对**有坐标**的块动手：纯文本后端的"块"本身就是段落，再合并只会把
    两个真段落粘成一个，而且没有坐标可依据。
    """
    out: list[Block] = []
    for block in blocks:
        prev = out[-1] if out else None
        if prev is not None and _mergeable(prev, block):
            out[-1] = replace(
                prev,
                text=join_lines(prev.text, block.text),
                bbox=_union(prev.bbox, block.bbox),
            )
            continue
        out.append(block)
    return out


def _mergeable(prev: Block, block: Block) -> bool:
    if prev.kind != "text" or block.kind != "text":
        return False  # 标题/公式/图注是独立单元，不许被并进正文
    if prev.bbox is None or block.bbox is None:
        return False
    if abs(prev.bbox[0] - block.bbox[0]) > MERGE_X_TOL:
        return False  # 左边界差得多 → 不同栏（或缩进层级变了）
    gap = block.bbox[1] - prev.bbox[3]
    if gap < 0 or gap > MERGE_GAP_RATIO:
        return False
    # 上一行已经收句 → 这是新段落
    return not prev.text.rstrip().endswith(_SENTENCE_END)


def _union(a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float] | None) -> tuple | None:
    if a is None or b is None:
        return a or b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


__all__ = [
    "detect_columns",
    "build_heading_tree",
    "join_lines",
    "merge_paragraphs",
    "order_blocks",
]
