"""PDF 解析与分块。

链路：`parse_pdf(path)` → `PdfDocument(sections, full_text, page_count)`
     → `chunk_document(doc)` → `list[Chunk]`（带 section / 页码，供 Milvus 过滤与引用定位）
"""

from app.parsers.chunker import Chunk, chunk_document, chunk_text, estimate_tokens
from app.parsers.pdf import PdfDocument, PdfPage, extract_references, parse_pdf

__all__ = [
    "Chunk",
    "PdfDocument",
    "PdfPage",
    "chunk_document",
    "chunk_text",
    "estimate_tokens",
    "extract_references",
    "parse_pdf",
]
