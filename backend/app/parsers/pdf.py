"""PDF 解析降级链：MinerU（版面还原 / OCR / 公式 / 表格）→ PyMuPDF → pdfplumber → pypdf。

MinerU 是首选：双栏、公式、表格、扫描件这些 PyMuPDF 拿不准的版面它都能还原，
代价是慢（CPU 上每页数秒起）且要吃几个 GB 的模型权重。不可用时自动降级到
纯文本抽取 —— 检索要的是"文本 + 页码"，降级后页码仍在，只是版式信息变粗。

本模块只做三件事：抽文本（带页码）、按标题切章节、抽参考文献条目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.errors import ParseError, UnsupportedFileTypeError

# ---------------------------------------------------------------- 章节标题
# 常见学术论文章节（中英）。只认独立成行的短标题，避免把正文里的词误判成标题。
_SECTION_PATTERNS = [
    ("abstract", r"^\s*(abstract|摘\s*要)\s*$"),
    ("introduction", r"^\s*(\d+[\.\s]*)?(introduction|引\s*言|绪\s*论)\s*$"),
    ("related_work", r"^\s*(\d+[\.\s]*)?(related\s+work|literature\s+review|相关工作|研究现状)\s*$"),
    ("method", r"^\s*(\d+[\.\s]*)?(method(s|ology)?|approach|proposed\s+method|方\s*法|模型)\s*$"),
    ("experiment", r"^\s*(\d+[\.\s]*)?(experiments?|evaluation|results?|实验|实验结果|评\s*估)\s*$"),
    ("discussion", r"^\s*(\d+[\.\s]*)?(discussion|讨\s*论)\s*$"),
    ("conclusion", r"^\s*(\d+[\.\s]*)?(conclusions?|concluding\s+remarks|结\s*论|总\s*结)\s*$"),
    ("references", r"^\s*(references?|bibliography|参考文献)\s*$"),
    ("appendix", r"^\s*(appendix|附录)\s*[A-Z0-9]?\s*$"),
]
_SECTION_RE = [(name, re.compile(p, re.I)) for name, p in _SECTION_PATTERNS]

# 参考文献条目：以 [n] / n. / n) 开头，或 "Author, A. (2020)."
_REF_START = re.compile(r"^\s*(\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[\.\)])\s+")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_ARXIV_RE = re.compile(r"arXiv[:\s]+(\d{4}\.\d{4,5})(v\d+)?", re.I)
_YEAR_RE = re.compile(r"(19|20)\d{2}")


@dataclass(slots=True)
class PdfPage:
    number: int  # 1-based
    text: str


@dataclass(slots=True)
class PdfDocument:
    path: str
    pages: list[PdfPage] = field(default_factory=list)
    parser: str = "pymupdf"
    title: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    def section_of(self) -> list[tuple[int, str, str]]:
        """返回 [(page_number, section_name, text)]，按章节切好的正文块。"""
        buckets: list[tuple[int, str, str]] = []
        current_name = "body"
        current_page = self.pages[0].number if self.pages else 1
        buf: list[str] = []

        def flush() -> None:
            text = "\n".join(buf).strip()
            if text:
                buckets.append((current_page, current_name, text))

        for page in self.pages:
            for line in page.text.splitlines():
                hit = _match_section(line)
                if hit:
                    flush()
                    buf = []
                    current_name = hit
                    current_page = page.number
                    continue
                buf.append(line)
        flush()
        return buckets


def _match_section(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:  # 标题不会太长
        return None
    for name, pattern in _SECTION_RE:
        if pattern.match(stripped):
            return name
    return None


def _parse_with_mineru(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    """延迟导入：`mineru.py` 要拿本模块的 `PdfPage`，模块级互导会成环。"""
    from app.parsers.mineru import parse_with_mineru

    return parse_with_mineru(path)


def _parse_with_pymupdf(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    import fitz  # PyMuPDF

    pages: list[PdfPage] = []
    with fitz.open(path) as doc:
        meta = dict(doc.metadata or {})
        for i, page in enumerate(doc, start=1):
            pages.append(PdfPage(number=i, text=page.get_text("text") or ""))
    return pages, meta, (meta.get("title") or "").strip()


def _parse_with_pdfplumber(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    import pdfplumber

    pages: list[PdfPage] = []
    with pdfplumber.open(path) as pdf:
        meta = dict(pdf.metadata or {})
        for i, page in enumerate(pdf.pages, start=1):
            pages.append(PdfPage(number=i, text=page.extract_text() or ""))
    return pages, meta, (meta.get("Title") or "").strip()


def _parse_with_pypdf(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = {k: str(v) for k, v in (reader.metadata or {}).items()}
    pages = [PdfPage(number=i, text=p.extract_text() or "") for i, p in enumerate(reader.pages, start=1)]
    return pages, meta, (meta.get("/Title") or "").strip()


_BACKENDS = (
    ("mineru", _parse_with_mineru),
    ("pymupdf", _parse_with_pymupdf),
    ("pdfplumber", _parse_with_pdfplumber),
    ("pypdf", _parse_with_pypdf),
)


def parse_pdf(path: str | Path, *, prefer: str | None = None) -> PdfDocument:
    """解析 PDF。逐级降级，三级全挂才抛 ParseError。"""
    p = Path(path)
    if not p.exists():
        raise ParseError(f"文件不存在: {p}")
    if p.suffix.lower() not in {".pdf"}:
        raise UnsupportedFileTypeError(f"仅支持 PDF，收到 {p.suffix}")

    candidates = list(_BACKENDS)
    if prefer:
        candidates.sort(key=lambda kv: kv[0] != prefer)

    errors: list[str] = []
    for name, fn in candidates:
        try:
            pages, meta, title = fn(p)
        except ImportError as exc:
            errors.append(f"{name}: 未安装({exc.name})")
            continue
        except Exception as exc:  # noqa: BLE001 - 任一后端失败都要能继续降级
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        text_pages = [pg for pg in pages if pg.text.strip()]
        if not text_pages:
            errors.append(f"{name}: 未抽出任何文本（可能是扫描件，需要 OCR）")
            continue

        doc = PdfDocument(path=str(p), pages=pages, parser=name, title=title, meta=meta)
        doc.references = extract_references(doc.full_text)
        logger.info(
            "PDF 解析完成 parser={} pages={} chars={} refs={}",
            name,
            doc.page_count,
            sum(len(pg.text) for pg in pages),
            len(doc.references),
        )
        return doc

    raise ParseError("PDF 解析失败：" + "；".join(errors))


def extract_references(text: str, *, max_items: int = 500) -> list[str]:
    """从参考文献章节抽条目。只做粗筛（编号开头），够建引文边即可。"""
    lowered = text.lower()
    cut = max(lowered.rfind("\nreferences"), lowered.rfind("\n参考文献"))
    if cut < 0:
        cut = max(0, len(text) - 20000)  # 找不到就只看尾部，参考文献通常在最后
    tail = text[cut:]

    items: list[str] = []
    current: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _REF_START.match(stripped):
            if current:
                items.append(" ".join(current))
            current = [stripped]
        elif current:
            # 续行：参考文献条目常跨行，但要防止把正文正文吸进来
            if len(" ".join(current)) < 1500:
                current.append(stripped)
    if current:
        items.append(" ".join(current))

    cleaned = [" ".join(i.split())[:1200] for i in items if len(i) > 20]
    logger.debug("参考文献抽取：{} 条", len(cleaned))
    return cleaned[:max_items]


def parse_reference_metadata(entry: str) -> dict[str, Any]:
    """从单条参考文献里抽 doi / arxiv_id / year / title(粗猜)。"""
    doi = _DOI_RE.search(entry)
    arxiv = _ARXIV_RE.search(entry)
    year = _YEAR_RE.search(entry)
    # 标题猜测：去掉编号与前导作者串后的第一段（引号内的最常见）
    quoted = re.search(r"[\"“]([^\"”]{10,300})[\"”]", entry)
    title = quoted.group(1) if quoted else ""
    return {
        "doi": doi.group(0) if doi else None,
        "arxiv_id": arxiv.group(1) if arxiv else None,
        "year": int(year.group(0)) if year else None,
        "title": title.strip(),
        "raw": entry,
    }
