"""分块策略：按章节切，章内按段落聚到目标长度。

为什么不用固定滑窗：学术论文的语义单元是章节/段落，跨章节的 chunk 会让引用
定位（"第 3 页 related_work"）失去意义，也让 NLI 校验更容易判错。

三种 chunk_type 都在这里产出：
- `text` —— 正文（默认）；
- `formula` —— 块级公式，content 是 LaTeX（`formula_parser` 已把公式从正文流摘出）；
- `figure_caption` —— 图注（`figure_parser` 摘出，见该模块头）。

**`bbox` 的粒度是"段"不是"句"**：坐标来自版面块，而 chunk 是按字符数拼的，
一段可能跨好几个块、一个块也可能被切进两个 chunk。这里取"参与本 chunk 的段落框
的并集"，够前端圈出大致位置。`ponytail:` 要精确到行得按行分块，那会让 chunk
碎成一句一块、检索粒度反而变差；真需要行级高亮时改为在检索层按 bbox 反查行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.parsers.pdf import bbox_union

# 目标 chunk 长度（字符）。bge-m3 max_length=8192，此处远小于上限，
# 是为了让检索粒度更细（引用定位更准），而非模型限制。
TARGET_CHARS = 900
MAX_CHARS = 1600
MIN_CHARS = 120
OVERLAP_CHARS = 120

_PARA_SPLIT = re.compile(r"\n\s*\n+")

Box = tuple[float, float, float, float]


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中文约 1 字 1 token，英文约 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk + max(1, (len(text) - cjk) // 4)


@dataclass(slots=True)
class Chunk:
    index: int
    content: str
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    char_offset: int | None = None
    token_count: int = 0
    #: 归一化定位框（见 `app.models.chunk.Chunk.bbox`）；拿不到坐标时为 None
    bbox: Any | None = None
    #: text | formula | table | figure_caption —— 与 chunks 表的 CHECK 约束一致
    chunk_type: str = "text"

    def __post_init__(self) -> None:
        if not self.token_count:
            self.token_count = estimate_tokens(self.content)


def _split_long(paragraph: str) -> list[str]:
    """超长段落按句号切，保持语义完整。"""
    sentences = re.split(r"(?<=[。！？.!?])\s*", paragraph)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if len(buf) + len(s) <= MAX_CHARS:
            buf += s
        else:
            if buf:
                out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def chunk_text(
    text: str,
    *,
    section: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    start_index: int = 0,
    bboxes: list[Box | None] | None = None,
) -> list[Chunk]:
    """把一段文本切成 chunk。

    `bboxes` 与段落一一对应（第 i 段的版面框），用于给 chunk 记定位框。
    长度不匹配时按 None 处理 —— 位置信息缺失不该让分块失败。
    """
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text or "") if p.strip()]
    units: list[tuple[str, Box | None]] = []
    for position, paragraph in enumerate(paragraphs):
        box = bboxes[position] if bboxes and position < len(bboxes) else None
        units.extend((piece, box) for piece in (_split_long(paragraph) if len(paragraph) > MAX_CHARS else [paragraph]))

    chunks: list[Chunk] = []
    buf: list[tuple[str, Box | None]] = []
    size = 0

    def emit() -> None:
        chunks.append(
            Chunk(
                index=start_index + len(chunks),
                content="\n\n".join(part for part, _ in buf),
                section=section,
                page_start=page_start,
                page_end=page_end,
                bbox=bbox_union(*(box for _, box in buf)),
            )
        )

    for unit, box in units:
        if size and size + len(unit) > TARGET_CHARS:
            emit()
            # 尾部重叠：把上一块的最后一段带到下一块开头，避免答案被切断
            last_text, last_box = buf[-1]
            tail = last_text[-OVERLAP_CHARS:] if len(last_text) > OVERLAP_CHARS else ""
            buf = [(tail, last_box)] if tail else []
            size = len(tail)
        buf.append((unit, box))
        size += len(unit)

    if buf:
        content = "\n\n".join(part for part, _ in buf)
        if len(content) >= MIN_CHARS or not chunks:
            emit()
        elif chunks:  # 过短就并回上一块，别留碎片
            chunks[-1].content += "\n\n" + content
            chunks[-1].token_count = estimate_tokens(chunks[-1].content)
            chunks[-1].bbox = bbox_union(chunks[-1].bbox, *(box for _, box in buf))
    return chunks


def chunk_document(doc: object, *, include_references: bool = False) -> list[Chunk]:
    """按章节切分 `PdfDocument`，并把公式 / 图注作为独立 chunk 追加。

    参考文献默认**不入库**（它不是论据，是元数据，进检索只会污染召回），
    但会被 `extract_references` 单独抽出来建引文图 —— 丢掉的只是"参与检索"。
    """
    units = doc.section_units()  # type: ignore[attr-defined]
    chunks: list[Chunk] = []

    for page, section, paragraphs in units:
        if section == "references" and not include_references:
            continue
        chunks.extend(
            chunk_text(
                "\n\n".join(text for text, _ in paragraphs),
                section=section,
                page_start=page,
                page_end=page,
                start_index=len(chunks),
                bboxes=[box for _, box in paragraphs],
            )
        )

    # 公式与图注各成 chunk：chunk_type 不同，检索侧可以按类型加权重或过滤。
    # 所属章节取"该页第一个出现的章节"，够溯源定位用。
    page_section: dict[int, str] = {}
    for page, section, _ in units:
        page_section.setdefault(page, section)
    for text, page, box, chunk_type in _extras(doc):
        chunks.append(
            Chunk(
                index=len(chunks),
                content=text,
                section=page_section.get(page),
                page_start=page,
                page_end=page,
                bbox=box,
                chunk_type=chunk_type,
            )
        )

    logger.info(
        "分块完成 chunks={} sections={} formulas={} figures={}",
        len(chunks),
        len(units),
        sum(1 for c in chunks if c.chunk_type == "formula"),
        sum(1 for c in chunks if c.chunk_type == "figure_caption"),
    )
    return chunks


def _extras(doc: object) -> list[tuple[str, int, Any, str]]:
    """从文档上取公式与图注块，返回 (文本, 页码, 框, chunk_type)。"""
    out: list[tuple[str, int, Any, str]] = []
    for block in getattr(doc, "formulas", None) or []:
        if block.text.strip():
            out.append((block.text, block.page, block.bbox, "formula"))
    for block in getattr(doc, "figures", None) or []:
        if block.text.strip():
            out.append((block.text, block.page, block.bbox, "figure_caption"))
    return out


__all__ = ["Chunk", "chunk_document", "chunk_text", "estimate_tokens"]
