"""分块策略：按章节切，章内按段落聚到目标长度。

为什么不用固定滑窗：学术论文的语义单元是章节/段落，跨章节的 chunk 会让引用
定位（"第 3 页 related_work"）失去意义，也让 NLI 校验更容易判错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

# 目标 chunk 长度（字符）。bge-m3 max_length=8192，此处远小于上限，
# 是为了让检索粒度更细（引用定位更准），而非模型限制。
TARGET_CHARS = 900
MAX_CHARS = 1600
MIN_CHARS = 120
OVERLAP_CHARS = 120

_PARA_SPLIT = re.compile(r"\n\s*\n+")


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
) -> list[Chunk]:
    """把一段文本切成 chunk。"""
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text or "") if p.strip()]
    units: list[str] = []
    for p in paragraphs:
        units.extend(_split_long(p) if len(p) > MAX_CHARS else [p])

    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0
    for unit in units:
        if size and size + len(unit) > TARGET_CHARS:
            chunks.append(
                Chunk(
                    index=start_index + len(chunks),
                    content="\n\n".join(buf),
                    section=section,
                    page_start=page_start,
                    page_end=page_end,
                )
            )
            # 尾部重叠：把上一块的最后一段带到下一块开头，避免答案被切断
            tail = buf[-1][-OVERLAP_CHARS:] if buf and len(buf[-1]) > OVERLAP_CHARS else ""
            buf = [tail] if tail else []
            size = len(tail)
        buf.append(unit)
        size += len(unit)
    if buf:
        content = "\n\n".join(buf)
        if len(content) >= MIN_CHARS or not chunks:
            chunks.append(
                Chunk(
                    index=start_index + len(chunks),
                    content=content,
                    section=section,
                    page_start=page_start,
                    page_end=page_end,
                )
            )
        elif chunks:  # 过短就并回上一块，别留碎片
            chunks[-1].content += "\n\n" + content
            chunks[-1].token_count = estimate_tokens(chunks[-1].content)
    return chunks


def chunk_document(doc: object, *, include_references: bool = False) -> list[Chunk]:
    """按章节切分 `PdfDocument`。

    参考文献默认**不入库**（它不是论据，是元数据，进检索只会污染召回），
    但会被 `extract_references` 单独抽出来建引文图 —— 丢掉的只是"参与检索"。
    """
    buckets = doc.section_of()  # type: ignore[attr-defined]
    chunks: list[Chunk] = []
    for page, section, text in buckets:
        if section == "references" and not include_references:
            continue
        chunks.extend(
            chunk_text(
                text,
                section=section,
                page_start=page,
                page_end=page,
                start_index=len(chunks),
            )
        )
    logger.info("分块完成 chunks={} sections={}", len(chunks), len(buckets))
    return chunks
