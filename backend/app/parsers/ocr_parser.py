"""扫描件检测与 OCR。

检测判据是**有文本层的页占比**：整份 PDF 一页都抽不出字 → 占比 0。
这个定义比"字符数 / 页面积"稳 —— 后者随页边距、字号、留白剧烈波动，
阈值只能在某一类版式上凑准。

OCR 后端只挂 PaddleOCR（`pip install -e ".[heavy]"`）。**没有后端时不报错**，
原样返回并留日志：装不上 PaddleOCR 的机器（没 GPU、装不下 paddle）不该因此
连整篇论文都入不了库。MinerU 那条路已经覆盖了 OCR —— 把
`MINERU_METHOD` 设成 `ocr` 就是它的扫描件模式，那条路在本模块之外。

坐标照旧归一化。PaddleOCR 给的是像素框，按渲染尺寸除回去 ——
渲染 DPI 只影响精度，不影响归一化后的结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import settings
from app.parsers.pdf import Block, PdfPage

#: 有文本层的页占比低于它 → 判为扫描件
DEFAULT_RATIO_THRESHOLD = 0.5


def text_layer_ratio(pages: list[PdfPage]) -> float:
    """有文本层的页占比 ∈ [0,1]。没有页时返回 0.0（宁可去试 OCR）。"""
    if not pages:
        return 0.0
    return sum(1 for page in pages if page.text.strip()) / len(pages)


def needs_ocr(pages: list[PdfPage], *, threshold: float | None = None) -> bool:
    """整份文档是否该转 OCR。"""
    limit = settings.OCR_TEXT_RATIO_THRESHOLD if threshold is None else threshold
    return text_layer_ratio(pages) < limit


def missing_text_pages(pages: list[PdfPage]) -> list[int]:
    """没有文本层的页码（1-based）。OCR 只跑这几页 —— 全篇重跑是纯浪费。"""
    return [page.number for page in pages if not page.text.strip()]


def resolve_backend() -> str | None:
    """可用后端名，没有返回 None。PaddleOCR 自带中英模型，且不依赖外部服务。"""
    try:
        import paddleocr  # noqa: F401
    except Exception:  # noqa: BLE001 - paddle 装不上的姿势很多，一律当"没有"
        return None
    return "paddleocr"


def _render_page(path: Path, page_number: int) -> tuple[Any, int, int]:
    """把某页渲成 PNG 字节。返回 (png_bytes, width_px, height_px)。"""
    import fitz

    with fitz.open(path) as doc:
        page = doc[int(page_number) - 1]
        pix = page.get_pixmap(dpi=settings.OCR_DPI)
        return pix.tobytes("png"), int(pix.width), int(pix.height)


def _paddle_blocks(png: bytes) -> list[tuple[str, list[list[float]]]]:
    """PaddleOCR 识别一页，返回 [(文本, 四点框)]。"""
    from paddleocr import PaddleOCR

    engine = _engine()
    if engine is None:  # pragma: no cover - resolve_backend 已经拦过一道
        engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        _ENGINES["paddleocr"] = engine
    raw = engine.ocr(png, cls=True)
    lines = raw[0] if raw and isinstance(raw[0], list) else (raw or [])
    out: list[tuple[str, list[list[float]]]] = []
    for item in lines:
        try:
            box, (text, _score) = item[0], item[1]
        except (TypeError, IndexError, ValueError):
            continue
        if str(text).strip():
            out.append((str(text).strip(), box))
    return out


#: 引擎构造很贵（几百 MB 权重），按后端名缓存
_ENGINES: dict[str, Any] = {}
_BACKENDS: dict[str, Any] = {"paddleocr": _paddle_blocks}


def _engine() -> Any | None:
    return _ENGINES.get("paddleocr")


def _compose_page_text(blocks: list[Block]) -> str:
    """OCR 出来的行拼成页文本。按 y 再按 x 排序 —— OCR 的返回顺序不保证是阅读序。"""
    ordered = sorted(
        blocks,
        key=lambda b: ((b.bbox[1] + b.bbox[3]) / 2 if b.bbox else 0.0, b.bbox[0] if b.bbox else 0.0),
    )
    return "\n".join(b.text for b in ordered)


def ocr_document(path: str | Path, pages: list[PdfPage]) -> list[PdfPage]:
    """对**缺文本层的页**做 OCR，返回新的页列表（原本有文本的页原样保留）。

    没有可用后端 / 渲染或识别失败时原样返回，并记一条 warning：
    这个函数永远不抛异常 —— 它的调用点是在降级链内部，抛出去会让整篇论文
    直接判失败，而"OCR 没装上"不该是致命的。
    """
    backend = resolve_backend()
    if backend is None:
        logger.warning("检测到无文本层，但没有可用的 OCR 后端（未安装 paddleocr，或 MinerU 未启用 ocr 模式）")
        return pages
    targets = set(missing_text_pages(pages))
    if not targets:
        return pages

    recognize = _BACKENDS.get(backend)
    out: list[PdfPage] = []
    for page in pages:
        if page.number not in targets:
            out.append(page)
            continue
        try:
            png, width, height = _render_page(Path(path), page.number)
            raw = recognize(png)
        except Exception as exc:  # noqa: BLE001 - OCR 失败不该拖垮整篇
            logger.warning("OCR 失败 page={} backend={}: {}", page.number, backend, exc)
            out.append(page)
            continue
        blocks = [_to_block(text, box, page.number, width, height) for text, box in raw if box]
        blocks = [b for b in blocks if b is not None]
        page.blocks = blocks
        page.text = _compose_page_text(blocks)
        page.width, page.height = float(width), float(height)
        logger.info("OCR 完成 page={} backend={} lines={}", page.number, backend, len(blocks))
        out.append(page)
    return out


def _to_block(text: str, box: list[list[float]], page: int, width: int, height: int) -> Block | None:
    """PaddleOCR 的四点框 → 归一化矩形块。"""
    try:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
    except (TypeError, IndexError, ValueError):
        return None
    w = float(width) or 1.0
    h = float(height) or 1.0
    return Block(
        text=text,
        page=page,
        bbox=(min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h),
    )


__all__ = [
    "DEFAULT_RATIO_THRESHOLD",
    "missing_text_pages",
    "needs_ocr",
    "ocr_document",
    "resolve_backend",
    "text_layer_ratio",
]
