"""MinerU 解析：调用 `mineru` CLI，把版面还原后的内容块折回本项目的 PDF 结构。

**为什么走 CLI 而不是 import**：MinerU 的 Python 入口在 2.x → 3.x → 4.x 之间改过名
（`magic_pdf` → `mineru`），CLI 是唯一稳定的契约。而且它依赖 torch 加一堆模型权重，
放进 API 进程只会把后端镜像撑到几个 GB —— 独立 venv / 独立容器才是它该待的地方，
所以 `MINERU_CMD` 是一个可指向任意解释器的路径。

**为什么递归找 `*_content_list.json`**：各版本输出目录层数不一样
（`<stem>/<method>/xxx_content_list.json`），写死路径必然随版本失效。
拿到内容块后按 `page_idx` 归页，页码（引用定位的锚点）就保住了。

拿不到 content_list 时退化为整篇 markdown：能检索，但页码丢失，日志会警告。
"""

from __future__ import annotations

import html
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import settings
from app.parsers.pdf import PdfPage

# 表格是 HTML（<table><tr><td>），检索和嵌入都不需要标签，压成纯文本
_TAG_RE = re.compile(r"<[^>]+>")

# 正文块类型。MinerU 用 "text" / "title" / "equation" / "table" / "image"
_TEXT_KINDS = {"text", "title", "equation", "list", "caption"}


def resolve_cmd(cmd: str | None = None) -> str | None:
    """把 MINERU_CMD 解析成真实存在的可执行文件路径；找不到返回 None。

    支持三种写法：裸命令（`mineru`，走 PATH）、绝对路径、以及 venv 里的 `mineru.exe`。
    """
    raw = (cmd or settings.MINERU_CMD or "").strip()
    if not raw:
        return None
    if Path(raw).is_file():
        return raw
    return shutil.which(raw)


def _build_argv(cmd: str, path: Path, out_dir: str) -> list[str]:
    argv = [cmd, "-p", str(path), "-o", out_dir]
    if settings.MINERU_METHOD:
        argv += ["-m", settings.MINERU_METHOD]
    if settings.MINERU_DEVICE:
        argv += ["-d", settings.MINERU_DEVICE]
    argv += shlex.split(settings.MINERU_EXTRA_ARGS or "")
    return argv


def _load_content_list(out_dir: Path) -> list[dict[str, Any]] | None:
    for candidate in sorted(out_dir.rglob("*content_list.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("MinerU content_list 读不动 {}: {}", candidate.name, exc)
            continue
        if isinstance(data, list) and data:
            return [b for b in data if isinstance(b, dict)]
    return None


def _block_text(block: dict[str, Any]) -> str:
    kind = str(block.get("type") or "")
    if kind in _TEXT_KINDS:
        return str(block.get("text") or "").strip()
    if kind == "table":
        body = str(block.get("table_body") or block.get("text") or "")
        return html.unescape(_TAG_RE.sub(" ", body)).strip()
    if kind == "image":
        # 图本身没字，但图注有 —— 留着，图表检索要用
        caps = block.get("img_caption") or block.get("image_caption") or []
        return " ".join(str(c).strip() for c in caps if str(c).strip())
    return ""


def pages_from_blocks(blocks: list[dict[str, Any]]) -> list[PdfPage]:
    """内容块 → 分页文本。`page_idx` 是 0-based，转成 1-based 页码。"""
    by_page: dict[int, list[str]] = {}
    for block in blocks:
        text = _block_text(block)
        if not text:
            continue
        idx = block.get("page_idx")
        page = int(idx) + 1 if isinstance(idx, int) else 1
        by_page.setdefault(page, []).append(text)

    if not by_page:
        raise RuntimeError("content_list.json 里没有任何可用文本块")
    return [PdfPage(number=n, text="\n".join(by_page[n])) for n in sorted(by_page)]


def _read_output(out_dir: Path) -> list[PdfPage]:
    blocks = _load_content_list(out_dir)
    if blocks is not None:
        return pages_from_blocks(blocks)

    md = next(iter(sorted(out_dir.rglob("*.md"))), None)
    if md is None:
        raise RuntimeError(f"MinerU 输出目录里既没有 content_list.json 也没有 .md：{out_dir}")
    logger.warning("MinerU 未产出 content_list.json，退化为整篇 markdown（页码信息丢失）")
    return [PdfPage(number=1, text=md.read_text(encoding="utf-8"))]


def parse_with_mineru(path: Path) -> tuple[list[PdfPage], dict[str, Any], str]:
    """签名与 `pdf.py` 里其他后端一致，方便直接挂进降级链。

    失败一律抛异常（不返回空结果）—— 由 `parse_pdf` 决定要不要降级。
    缺 CLI / 被禁用抛 `ImportError`，与其他后端的"未安装"走同一条分支。
    """
    if not settings.MINERU_ENABLED:
        raise ImportError("MINERU_ENABLED=false")

    cmd = resolve_cmd()
    if not cmd:
        raise ImportError(f"找不到 mineru 可执行文件（MINERU_CMD={settings.MINERU_CMD!r}）")

    with tempfile.TemporaryDirectory(prefix="mineru-") as out_dir:
        argv = _build_argv(cmd, path, out_dir)
        logger.info("MinerU 解析开始：{}", " ".join(argv[:1] + ["..."]))
        try:
            proc = subprocess.run(  # noqa: S603 - argv 来自配置，不是外部输入
                argv,
                capture_output=True,
                text=True,
                timeout=settings.MINERU_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"MinerU 超时（>{settings.MINERU_TIMEOUT}s）") from exc

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise RuntimeError(f"MinerU 退出码 {proc.returncode}: {tail}")

        pages = _read_output(Path(out_dir))

    meta: dict[str, Any] = {
        "mineru_cmd": cmd,
        "mineru_method": settings.MINERU_METHOD,
        "mineru_device": settings.MINERU_DEVICE,
    }
    logger.info("MinerU 解析完成 pages={} chars={}", len(pages), sum(len(p.text) for p in pages))
    return pages, meta, ""


__all__ = ["parse_with_mineru", "pages_from_blocks", "resolve_cmd"]
