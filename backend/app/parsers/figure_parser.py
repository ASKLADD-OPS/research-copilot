"""图表提取 + Caption 关联。

图本身没有可检索的文本，能进检索库的是**图注**。所以这个模块的产出物是
`kind="figure_caption"` 的块（→ `chunks.chunk_type='figure_caption'`），
文本是图注原文，bbox 是**图 + 图注的并集** —— 前端据此高亮时圈出的是整块图，
而不是孤零零一行字。

图注识别不走模型：学术论文的图注格式极规矩（`Figure 3: ...` / `图 3 ...` /
`Table 2. ...`），一条正则就够。误判的代价是"一句正文被当成图注"，
所以加了长度与分隔符两道闸（见 `caption_of`）。
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.parsers.pdf import Block, bbox_union

#: `Figure 3: xxx` / `Fig. 3 xxx` / `Table 2. xxx` / `图 3 xxx` / `表 2：xxx`
#: `re.I` 不能省：标签在论文里大写（Figure / Fig. / TABLE），少了它只剩中文分支能命中。
CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?s?\.?|tab(?:le)?s?\.?|图|表)\s*"
    r"([0-9]{1,3}[a-z]?|[ivxIVX]{1,5})\s*"
    r"([:：.．、,，\-—|)）]|\s)\s*"
    r"(.+)$",
    re.S | re.I,
)
#: 图注不会太长；超过它是正文段落
CAPTION_MAX_CHARS = 400
#: 图注与图形的垂直距离上限（占页高）：超过它就不算同一组图表
CAPTION_MAX_GAP = 0.12
#: 空格分隔时用来挡正文的句末标点（"表 1 说明了结果。"不是图注）
_SENTENCE_END = ("。", "！", "？", ".", "!", "?", "；", ";")


def caption_of(text: str) -> str | None:
    """若这段文字是图注，返回规范化后的图注（原文，压掉换行）；否则 None。

    分隔符是**空白**时最危险（`Table 1 shows that ...` 就是正文），所以额外加
    两道闸：正文首字符是小写字母 → 不是图注；以句末标点收尾 → 不是图注。

    **已知边界**：中文正文若无空格且不以标点收尾（"图 3 展示了我们的方法"），
    这里会误判成图注。中文图注与中文正文之间没有可靠的正则判据（要靠句法），
    为一个低频误判去接一个句法模型不值。`ponytail:` 真被这类语料咬到时，
    判据换成"该行是否紧跟在一个图形框下方"（版式事实，比文本更可靠）。
    """
    stripped = " ".join((text or "").split())
    if not stripped or len(stripped) > CAPTION_MAX_CHARS:
        return None
    match = CAPTION_RE.match(stripped)
    if match is None:
        return None
    sep, body = match.group(2), match.group(3)
    if sep.isspace() and (body[:1].islower() or body.endswith(_SENTENCE_END)):
        return None
    return stripped


def split_captions(blocks: list[Block]) -> tuple[list[Block], list[Block]]:
    """把图注块从正文流里摘出来。返回 `(其余块, 图注块)`。"""
    kept: list[Block] = []
    captions: list[Block] = []
    for block in blocks:
        if block.kind in {"title", "formula"}:
            kept.append(block)
            continue
        text = caption_of(block.text)
        if text is None:
            kept.append(block)
            continue
        captions.append(Block(text=text, page=block.page, bbox=block.bbox, kind="figure_caption"))
    return kept, captions


def attach_images(
    captions: list[Block],
    images: list[tuple[float, float, float, float]],
    page: int,
) -> list[Block]:
    """给每条图注挂上它所属图形的框。

    就近规则：优先取**图注上方**最近的图形（学术排版里图注在图下、表注在表上，
    两种都覆盖到），否则取下方最近的；距离超过 `CAPTION_MAX_GAP` 就不挂，
    bbox 只留图注自己的位置 —— 挂错的框比没框更糟，前端会圈到别的内容上。
    """
    del page  # 页码已在 caption 里
    out: list[Block] = []
    for caption in captions:
        if caption.bbox is None or not images:
            out.append(caption)
            continue
        top = caption.bbox[1]
        bottom = caption.bbox[3]
        above = [box for box in images if box[3] <= top]
        below = [box for box in images if box[1] >= bottom]
        nearest: tuple[float, float, float, float] | None = None
        best = CAPTION_MAX_GAP
        for box in above:
            gap = top - box[3]
            if gap <= best:
                best, nearest = gap, box
        if nearest is None:
            best = CAPTION_MAX_GAP
            for box in below:
                gap = box[1] - bottom
                if gap <= best:
                    best, nearest = gap, box
        out.append(replace(caption, bbox=bbox_union(caption.bbox, nearest) or caption.bbox))
    return out


__all__ = ["CAPTION_RE", "attach_images", "caption_of", "split_captions"]
