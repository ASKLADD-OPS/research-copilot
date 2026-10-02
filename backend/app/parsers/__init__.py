"""PDF 解析、版面还原、OCR、公式/图表抽取与语义去重。

链路
----
`parse_pdf(path)` → `PdfDocument`（正文按阅读序重排、公式与图注已分流）
  → `chunk_document(doc)` → `list[Chunk]`（带 section / 页码 / bbox / chunk_type）
  → `pipeline` 写 `chunks` 表 + 双向量入 Milvus

去重
----
`semantic_dedup.resolve_duplicate(...)` 在写向量**之前**判定：同一篇论文的
不同 arXiv 版本进 `paper_versions` 谱系（两版都留），跨库重复合并到已有那一篇。
"""

from app.parsers.chunker import Chunk, chunk_document, chunk_text, estimate_tokens
from app.parsers.layout_parser import build_heading_tree, detect_columns, merge_paragraphs, order_blocks
from app.parsers.pdf import (
    Block,
    PdfDocument,
    PdfPage,
    bbox_union,
    enrich_document,
    extract_references,
    parse_pdf,
)
from app.parsers.semantic_dedup import (
    DedupVerdict,
    classify,
    normalize_arxiv_id,
    resolve_duplicate,
    semantic_fingerprint,
)

__all__ = [
    "Block",
    "Chunk",
    "DedupVerdict",
    "PdfDocument",
    "PdfPage",
    "bbox_union",
    "build_heading_tree",
    "chunk_document",
    "chunk_text",
    "classify",
    "detect_columns",
    "enrich_document",
    "estimate_tokens",
    "extract_references",
    "merge_paragraphs",
    "normalize_arxiv_id",
    "order_blocks",
    "parse_pdf",
    "resolve_duplicate",
    "semantic_fingerprint",
]
