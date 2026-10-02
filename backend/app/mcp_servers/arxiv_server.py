"""arXiv MCP Server —— 检索预印本 + 按编号下载 PDF。

独立运行：`python -m app.mcp_servers.arxiv_server`（stdio，供 MultiServerMCPClient 拉起）

两个工具的分工：`arxiv_search` 是"找"，`arxiv_fetch` 是"取"（回读元数据 + 落盘 PDF）。
分开是因为它们的调用时机完全不同 —— 检索在对话轮里，下载在入库流水线里，
而且下载会写磁盘，不该被 Agent 随手触发。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings
from app.mcp_servers._common import clean_text, get_bytes, get_text

mcp = FastMCP("arxiv")

_FEED = "https://export.arxiv.org/api/query"
_PDF = "https://arxiv.org/pdf/{arxiv_id}"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_FIELD = {
    "id": re.compile(r"<id>(.*?)</id>", re.DOTALL),
    "title": re.compile(r"<title>(.*?)</title>", re.DOTALL),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL),
    "published": re.compile(r"<published>(.*?)</published>", re.DOTALL),
}
_AUTHOR = re.compile(r"<name>(.*?)</name>", re.DOTALL)
_CATEGORY = re.compile(r'<category[^>]*term="([^"]+)"')
#: 新式 2401.12345 / 旧式 cs.CL/0701001，都可带 vN
_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)


def _grab(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def parse_arxiv_ref(raw: str) -> tuple[str, str]:
    """从链接 / 裸编号里拆出 `(arxiv_id, version)`。version 缺省为 "v1"。

    arXiv 的编号本身就是标识，所以"没写版本"与"写了 v1"在下载时是同一份文件 ——
    这里统一补 v1，返回的两个值可以直接落 `papers.arxiv_id / version`。

    纯函数，不联网 —— 需要在 API 层先做 400 校验（链接不是 arXiv 就早点拒掉，
    别等下载完才发现拿到的是 HTML 404 页）。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("arXiv 链接/编号为空")
    # `arxiv.org/pdf/2401.12345v7.pdf` 这类要先剥 .pdf，否则 `v7.pdf` 匹配不上
    text = re.sub(r"\.pdf$", "", text, flags=re.I).rstrip("/")
    match = _ID_RE.search(text)
    if match is None:
        raise ValueError(f"认不出 arXiv 编号: {raw!r}")
    return match.group(1), (match.group(2) or "v1").lower()


def _parse_entries(xml: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for block in _ENTRY.findall(xml):
        raw_id = _grab(_FIELD["id"], block)
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1] or raw_id.rsplit("/", 1)[-1]
        results.append(
            {
                "arxiv_id": arxiv_id,
                "title": clean_text(_grab(_FIELD["title"], block), limit=400),
                "authors": [clean_text(a, limit=80) for a in _AUTHOR.findall(block)][:12],
                "abstract": clean_text(_grab(_FIELD["summary"], block), limit=1500),
                "published": _grab(_FIELD["published"], block)[:10],
                "categories": _CATEGORY.findall(block)[:5],
                "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
            }
        )
    return results


@mcp.tool()
async def arxiv_search(
    query: str,
    max_results: int = 10,
    sort_by: str = "relevance",
) -> dict[str, Any]:
    """检索 arXiv 预印本论文。

    何时使用：用户要找**尚未正式发表**的论文、最新预印本，或本地库中检索不到时。
    何时不要用：问的是已入库论文的细节（用 retrieve_papers）。

    Args:
        query: 检索式。支持 arXiv 语法，如 `ti:"mixture of experts" AND abs:routing`。
            自然语言短语会被当作全字段检索。
        max_results: 返回条数，1-50。
        sort_by: `relevance`（相关度）或 `submittedDate`（最新）。

    Returns:
        {"total": int, "results": [{"arxiv_id","title","authors","abstract","published","categories","url"}]}
    """
    max_results = max(1, min(int(max_results), 50))
    sort_map = {"relevance": "relevance", "submittedDate": "submittedDate", "lastUpdatedDate": "lastUpdatedDate"}
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_map.get(sort_by, "relevance"),
        "sortOrder": "descending",
    }
    xml = await get_text(_FEED, params=params, timeout=settings.MCP_TIMEOUT)
    results = _parse_entries(xml)
    return {"total": len(results), "query": query, "results": results}


def _save_pdf(dest_dir: str, name: str, payload: bytes) -> Path:
    """建目录 + 落盘，返回最终路径。阻塞 IO，由调用侧丢进线程跑。"""
    target_dir = Path(dest_dir).expanduser() if dest_dir else settings.upload_path
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_bytes(payload)
    return target


@mcp.tool()
async def arxiv_fetch(arxiv_id: str, dest_dir: str = "") -> dict[str, Any]:
    """按编号/链接把一篇 arXiv 预印本的 PDF 抓到本地磁盘，并回读元数据。

    何时使用：用户给出 arXiv 链接（或编号）要求"收进我的文献库""下载这篇并解析"，
    或入库流水线需要先落地再解析时。
    何时不要用：用户只是想知道这篇论文讲了什么 —— 那用 arxiv_search（它只取摘要，
    不外网下一份几十 MB 的 PDF）。也不要用来找论文：本工具不检索，只按编号取件。

    Args:
        arxiv_id: arXiv 编号或链接，如 `2401.12345v7`、
            `https://arxiv.org/abs/2401.12345v7`。旧式编号 `cs.CL/0701001` 也认。
        dest_dir: PDF 落盘目录。留空则落到 `settings.upload_path`。

    Returns:
        {"arxiv_id","version","title","authors","abstract","published","categories",
         "url","pdf_path","bytes","sha256"}；拿不到 PDF 时抛异常（由调用侧降级）。
    """
    ident, version = parse_arxiv_ref(arxiv_id)
    # 元数据：id_list 精确查，比 search_query 少一次"猜标题"的机会
    xml = await get_text(_FEED, params={"id_list": ident, "max_results": 1}, timeout=settings.MCP_TIMEOUT)
    entries = _parse_entries(xml)
    meta = entries[0] if entries else {}

    limit = settings.ARXIV_MAX_PDF_MB * 1024 * 1024
    payload, content_type = await get_bytes(
        _PDF.format(arxiv_id=f"{ident}{version}"),
        timeout=settings.MCP_TIMEOUT,
        max_bytes=limit,
    )
    # arXiv 对不存在的编号会返回 HTML 页面（200），落成 .pdf 后 PyMuPDF 会报"不是 PDF"，
    # 报错位置离病因很远 —— 在这里就拦掉。
    if not payload.startswith(b"%PDF"):
        raise ValueError(f"arXiv 未返回 PDF（content-type={content_type!r}，{len(payload)} 字节），编号可能不存在")

    target = await asyncio.to_thread(_save_pdf, dest_dir, f"arxiv-{ident.replace('/', '_')}{version}.pdf", payload)

    return {
        **{k: meta.get(k) for k in ("title", "authors", "abstract", "published", "categories", "url")},
        "arxiv_id": ident,
        "version": version,
        "pdf_path": str(target),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
