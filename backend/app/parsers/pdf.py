"""PDF 解析降级链：MinerU（版面还原 / OCR / 公式 / 表格）→ PyMuPDF → pdfplumber → pypdf。

MinerU 是首选：双栏、公式、表格、扫描件这些 PyMuPDF 拿不准的版面它都能还原，
代价是慢（CPU 上每页数秒起）且要吃几个 GB 的模型权重。不可用时自动降级到
纯文本抽取 —— 检索要的是"文本 + 页码"，降级后页码仍在，只是版式信息变粗。

本模块负责四件事
----------------
1. 抽文本（带页码）。PyMuPDF 路径额外抽**行级块 + 归一化坐标 + 字号**，
   交 `layout_parser` 重排阅读顺序、`formula_parser` / `figure_parser` 分流；
2. `enrich_document()` 把版面/公式/图表三件事的结果写回 `PdfPage.text`
   —— 顺序正确之后，下游 `chunk_document()` 一个字都不用改；
3. 按标题切章节（`section_of`）、抽参考文献条目；
4. 扫描件走 OCR（`ocr_parser`）。

**坐标一律归一化**（各分量 ∈ [0,1]，见 `chunks.bbox` 的注释）：存点值的话
前端一缩放高亮框就飘。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import settings
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

# 数学字体：PyMuPDF 报的是字体名，LaTeX 出来的公式基本都落在这几个族里。
# 只当**加分项**用（命中即判公式），不做否定判断 —— 换一套模板字体名就变了。
_MATH_FONT_RE = re.compile(r"CMMI|CMSY|CMEX|CMR\d|MSAM|MSBM|Symbol|Mathematical|Euclid|MathJax", re.I)


# ---------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class Block:
    """一个版面块：文本 + 它在页面上的位置。

    `bbox` 归一化（各分量 ∈ [0,1]）并且**可以为 None** —— MinerU 与纯文本后端
    只吐文本流，没有坐标。凡是"没有坐标就不该做的事"（分栏判断、段落合并）
    都以 `bbox is None` 为闸门，而不是猜一个默认值。
    """

    text: str
    page: int = 1
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "text"  # text | title | formula | figure_caption | table
    level: int = 0  # 标题层级 1/2/3，仅 kind="title"
    inline: bool = False  # 行内公式 vs 块级公式，仅 kind="formula"
    size: float = 0.0  # 主字号（pt）—— 标题层级靠它排，纯文本后端为 0

    @property
    def width(self) -> float:
        return (self.bbox[2] - self.bbox[0]) if self.bbox else 0.0

    @property
    def height(self) -> float:
        return (self.bbox[3] - self.bbox[1]) if self.bbox else 0.0


def bbox_union(*boxes: Any) -> tuple[float, float, float, float] | None:
    """合并若干归一化框。全为 None 时返回 None（宁可没框，也别给个假框）。"""
    valid = [b for b in boxes if b]
    if not valid:
        return None
    return (
        min(float(b[0]) for b in valid),
        min(float(b[1]) for b in valid),
        max(float(b[2]) for b in valid),
        max(float(b[3]) for b in valid),
    )


@dataclass(slots=True)
class PdfPage:
    number: int  # 1-based
    text: str
    blocks: list[Block] = field(default_factory=list)
    images: list[tuple[float, float, float, float]] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0


@dataclass(slots=True)
class PdfDocument:
    path: str
    pages: list[PdfPage] = field(default_factory=list)
    parser: str = "pymupdf"
    title: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    # 从正文流里摘出来的独立内容（不进 page.text，各成 chunk_type）
    formulas: list[Block] = field(default_factory=list)
    figures: list[Block] = field(default_factory=list)
    headings: list[Block] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    def blocks_of(self, page: PdfPage) -> list[Block]:
        """取页面的块。没有块的后端（MinerU / pdfplumber / pypdf）按行造伪块。

        统一成块之后，章节判定、公式分流、图表说明识别就只有一条代码路径 ——
        否则每个后端都要分叉一次，迟早有一条没人测到。
        """
        if page.blocks:
            return list(page.blocks)
        return [Block(text=line.strip(), page=page.number) for line in page.text.splitlines() if line.strip()]

    def section_units(self) -> list[tuple[int, str, list[tuple[str, tuple[float, float, float, float] | None]]]]:
        """`[(起页, 章节名, [(段落文本, 段落框), ...]), ...]` —— 分块的真正输入。

        段落**同时带着自己的版面框**，这样 chunk 才能记下"这段话在哪儿"。
        没有坐标的后端给 None，下游一律容忍（宁可没框，也别给个假框）。

        逐**行**匹配章节名而不是逐块：PyMuPDF 常把"小标题 + 紧随的第一段"塞进
        同一行块，逐块匹配会漏掉这种标题。
        """
        units: list[tuple[int, str, list[tuple[str, tuple[float, float, float, float] | None]]]] = []
        current_name = "body"
        current_page = self.pages[0].number if self.pages else 1
        buf: list[tuple[str, tuple[float, float, float, float] | None]] = []

        def flush() -> None:
            if buf:
                units.append((current_page, current_name, list(buf)))

        for page in self.pages:
            for block in self.blocks_of(page):
                pieces: list[str] = []
                for line in block.text.splitlines() or [block.text]:
                    hit = _match_section(line)
                    if hit:
                        if pieces:
                            buf.append(("\n".join(pieces).strip(), block.bbox))
                            pieces = []
                        flush()
                        buf = []
                        current_name = hit
                        current_page = page.number
                        continue
                    pieces.append(line)
                if pieces:
                    text = "\n".join(pieces).strip()
                    if text:
                        buf.append((text, block.bbox))
        flush()
        return [(page, name, paras) for page, name, paras in units if any(t for t, _ in paras)]

    def section_of(self) -> list[tuple[int, str, str]]:
        """返回 [(page_number, section_name, text)]，按章节切好的正文块。"""
        return [
            (page, name, "\n\n".join(text for text, _ in paragraphs)) for page, name, paragraphs in self.section_units()
        ]


def _match_section(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:  # 标题不会太长
        return None
    for name, pattern in _SECTION_RE:
        if pattern.match(stripped):
            return name
    return None


# ---------------------------------------------------------------- 后端：PyMuPDF
def _norm_box(raw: Any, width: float, height: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in raw)
    w = width or 1.0
    h = height or 1.0
    return (
        max(0.0, min(1.0, x0 / w)),
        max(0.0, min(1.0, y0 / h)),
        max(0.0, min(1.0, x1 / w)),
        max(0.0, min(1.0, y1 / h)),
    )


def _line_from_fitz(line: dict[str, Any], number: int, width: float, height: float) -> Block | None:
    """一行 → Block。行内 span 直接拼（同一行的 span 是字体切换切出来的，不补空格）。"""
    spans = [s for s in (line.get("spans") or []) if (s.get("text") or "").strip()]
    if not spans:
        return None
    text = "".join(s.get("text") or "" for s in spans).strip()
    if not text:
        return None
    size = max(float(s.get("size") or 0.0) for s in spans)
    # 数学字体覆盖了这行多少字 → 过半就按公式收走（比符号密度启发式稳）
    math_chars = sum(len(s.get("text") or "") for s in spans if _MATH_FONT_RE.search(str(s.get("font") or "")))
    total = sum(len(s.get("text") or "") for s in spans) or 1
    is_math = math_chars * 2 >= total
    return Block(
        text=text,
        page=number,
        bbox=_norm_box(line.get("bbox") or (0, 0, 0, 0), width, height),
        kind="formula" if is_math else "text",
        size=size,
    )


def _parse_with_pymupdf(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    """PyMuPDF：**行级**取块（不用它自己的块分割）。

    为什么丢掉 PyMuPDF 的块分组：它的块粒度随版本摇摆，且常在块里混进标题行，
    导致"标题 + 首段"粘在一起 → 章节判定漏掉。行级是最小可信单元，
    段落由 `layout_parser.merge_paragraphs` 按坐标间隙重建，规则在我们手里。
    """
    import fitz  # PyMuPDF

    pages: list[PdfPage] = []
    with fitz.open(path) as doc:
        meta = dict(doc.metadata or {})
        for i, page in enumerate(doc, start=1):
            rect = page.rect
            w, h = float(rect.width), float(rect.height)
            blocks: list[Block] = []
            for pb in page.get_text("dict").get("blocks") or []:
                if int(pb.get("type", 0)) != 0:  # 1 = 图片块，图片走 get_image_info
                    continue
                for line in pb.get("lines") or []:
                    block = _line_from_fitz(line, i, w, h)
                    if block is not None:
                        blocks.append(block)
            images = [
                _norm_box(info.get("bbox") or (0, 0, 0, 0), w, h)
                for info in (page.get_image_info() or [])
                if info.get("bbox")
            ]
            text = "\n\n".join(b.text for b in blocks)
            pages.append(PdfPage(number=i, text=text, blocks=blocks, images=images, width=w, height=h))
    return pages, meta, (meta.get("title") or "").strip()


# ---------------------------------------------------------------- 其他后端：纯文本
def _parse_with_pdfplumber(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    import pdfplumber

    pages: list[PdfPage] = []
    with pdfplumber.open(path) as pdf:
        meta = dict(pdf.metadata or {})
        for i, page in enumerate(pdf.pages, start=1):
            pages.append(
                PdfPage(
                    number=i,
                    text=page.extract_text() or "",
                    width=float(page.width or 0),
                    height=float(page.height or 0),
                )
            )
    return pages, meta, (meta.get("Title") or "").strip()


def _parse_with_pypdf(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta = {k: str(v) for k, v in (reader.metadata or {}).items()}
    pages = [PdfPage(number=i, text=p.extract_text() or "") for i, p in enumerate(reader.pages, start=1)]
    return pages, meta, (meta.get("/Title") or "").strip()


def _parse_with_mineru(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    """延迟导入：`mineru.py` 要拿本模块的 `PdfPage`，模块级互导会成环。"""
    from app.parsers.mineru import parse_with_mineru

    return parse_with_mineru(path)


_BACKENDS = (
    ("mineru", _parse_with_mineru),
    ("pymupdf", _parse_with_pymupdf),
    ("pdfplumber", _parse_with_pdfplumber),
    ("pypdf", _parse_with_pypdf),
)


# ---------------------------------------------------------------- 增强（版面/公式/图表/OCR）
def enrich_document(doc: PdfDocument) -> PdfDocument:
    """把"块"变成"读得顺的正文 + 分流出来的公式/图表说明"。

    顺序是有讲究的，改之前先想清楚：
    1. `order_blocks`   —— 双栏重排。必须在合并段落之前：合并依赖"左右栏 x0 不同"
       这个信号，乱序会让左栏末行与右栏首行被并成一段。
    2. `build_heading_tree` —— 按字号标标题。必须在合并之前，否则标题会被并进正文。
    3. `split_formulas` / `split_captions` —— 分流。也必须在合并之前，
       否则公式行被并进段落就再也认不出来。
    4. `merge_paragraphs` —— 最后才把正文行并回段落。
    """
    from app.parsers import figure_parser, formula_parser, layout_parser

    if not settings.LAYOUT_ENABLED:
        return doc

    kept_pages: list[PdfPage] = []
    for page in doc.pages:
        blocks = doc.blocks_of(page)
        blocks = layout_parser.order_blocks(blocks)
        blocks = layout_parser.build_heading_tree(blocks)
        kept, formulas = formula_parser.split_formulas(blocks, inline_latex=settings.FORMULA_INLINE_LATEX)
        kept, captions = figure_parser.split_captions(kept)
        kept = layout_parser.merge_paragraphs(kept)
        doc.formulas.extend(formulas)
        doc.figures.extend(figure_parser.attach_images(captions, page.images, page.number))
        doc.headings.extend(b for b in kept if b.kind == "title")
        page.blocks = kept
        # 只重写正文：公式与图表说明已经分流出去了，留在正文里会让它们被检索两遍。
        # 用空行分隔 = 段落边界，下游 `chunk_text` 正是按空行切段。
        page.text = "\n\n".join(b.text for b in kept if b.kind in {"text", "title"})
        kept_pages.append(page)
    doc.pages = kept_pages
    logger.debug(
        "版面增强完成 pages={} headings={} formulas={} figures={}",
        doc.page_count,
        len(doc.headings),
        len(doc.formulas),
        len(doc.figures),
    )
    return doc


# ---------------------------------------------------------------- 主入口
def parse_pdf(path: str | Path, *, prefer: str | None = None) -> PdfDocument:
    """解析 PDF。逐级降级，全挂才抛 ParseError。"""
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

        if not [pg for pg in pages if pg.text.strip()]:
            # 抽不出文本 —— 扫描件。在**这里**补 OCR，而不是另起一条链路：
            # 这是"没有文本层"这个判定唯一的落点。
            from app.parsers.ocr_parser import ocr_document, text_layer_ratio

            ratio = text_layer_ratio(pages)
            pages = ocr_document(p, pages)
            if not [pg for pg in pages if pg.text.strip()]:
                errors.append(f"{name}: 未抽出任何文本（文本层占比 {ratio:.2f}，OCR 也没接上）")
                continue
            meta = {**meta, "ocr_from": name, "text_layer_ratio": ratio}
            name = f"{name}+ocr"

        doc = PdfDocument(path=str(p), pages=pages, parser=name, title=title, meta=meta)
        enrich_document(doc)
        doc.references = extract_references(doc.full_text)
        logger.info(
            "PDF 解析完成 parser={} pages={} chars={} refs={} formulas={} figures={}",
            name,
            doc.page_count,
            sum(len(pg.text) for pg in pages),
            len(doc.references),
            len(doc.formulas),
            len(doc.figures),
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


__all__ = [
    "Block",
    "PdfDocument",
    "PdfPage",
    "bbox_union",
    "enrich_document",
    "extract_references",
    "parse_pdf",
    "parse_reference_metadata",
]
