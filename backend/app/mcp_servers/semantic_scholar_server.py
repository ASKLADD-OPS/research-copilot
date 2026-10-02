"""Semantic Scholar MCP Server —— 带引用数的文献检索。

独立运行：`python -m app.mcp_servers.semantic_scholar_server`
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings
from app.mcp_servers._common import get_json

mcp = FastMCP("semantic-scholar")

_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_FIELDS = "title,abstract,year,venue,citationCount,influentialCitationCount,authors,externalIds,url,openAccessPdf"


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.SEMANTIC_SCHOLAR_API_KEY} if settings.SEMANTIC_SCHOLAR_API_KEY else {}


@mcp.tool()
async def semantic_scholar_search(
    query: str,
    max_results: int = 10,
    min_citations: int | None = None,
) -> dict[str, Any]:
    """检索 Semantic Scholar，**带引用数**，适合判断论文影响力。

    何时使用：需要按引用量排序、找某领域的奠基工作、看论文的被引情况。
    何时不要用：只为拿摘要（arxiv_search 更详细），或问已入库论文（用 retrieve_papers）。

    Args:
        query: 自然语言检索式（不支持复杂布尔语法）。
        max_results: 返回条数，1-50。
        min_citations: 只保留引用数不低于该值的论文（可选）。

    Returns:
        {"total": int, "results": [{"paper_id","title","year","venue","citation_count",
        "influential_citation_count","authors","abstract","pdf_url","url"}]}
    """
    max_results = max(1, min(int(max_results), 50))
    data = await get_json(
        f"{_BASE}/search",
        params={"query": query, "limit": max_results, "fields": _FIELDS},
        headers=_headers(),
        timeout=settings.MCP_TIMEOUT,
    )

    results: list[dict[str, Any]] = []
    for item in data.get("data", []):
        if min_citations is not None and (item.get("citationCount") or 0) < min_citations:
            continue
        pdf = item.get("openAccessPdf") or {}
        results.append(
            {
                "paper_id": item.get("paperId", ""),
                "title": (item.get("title") or "").strip(),
                "year": item.get("year"),
                "venue": item.get("venue") or "",
                "citation_count": item.get("citationCount") or 0,
                "influential_citation_count": item.get("influentialCitationCount") or 0,
                "authors": [a.get("name", "") for a in (item.get("authors") or [])][:12],
                "abstract": (item.get("abstract") or "")[:1500],
                "pdf_url": pdf.get("url") or "",
                "url": item.get("url") or "",
            }
        )

    results.sort(key=lambda r: r["citation_count"], reverse=True)
    return {"total": len(results), "query": query, "results": results}


@mcp.tool()
async def semantic_scholar_paper(paper_id: str) -> dict[str, Any]:
    """按 id 取**单篇论文**的完整书目信息（含引用数）。

    何时使用：已知 DOI / arXiv 号，要补全年份、期刊、被引数（引文图的节点属性）。
    何时不要用：按关键词找论文（用 semantic_scholar_search）。

    Args:
        paper_id: `DOI:10.x/y` / `ARXIV:2401.00001` / `CorpusId:123` / 40 位 hash。

    Returns:
        {"found": bool, "paper": {"paper_id","title","year","venue","citation_count",
        "influential_citation_count","authors","abstract","doi","arxiv_id","pdf_url","url"}}
    """
    key = s2_paper_id(paper_id)
    item = await get_json(
        f"{_BASE}/{key}", params={"fields": _FIELDS}, headers=_headers(), timeout=settings.MCP_TIMEOUT
    )
    ext = item.get("externalIds") or {}
    pdf = item.get("openAccessPdf") or {}
    return {
        "found": bool(item.get("title")),
        "paper": {
            "paper_id": item.get("paperId", ""),
            "title": (item.get("title") or "").strip(),
            "year": item.get("year"),
            "venue": item.get("venue") or "",
            "citation_count": item.get("citationCount") or 0,
            "influential_citation_count": item.get("influentialCitationCount") or 0,
            "authors": [a.get("name", "") for a in (item.get("authors") or [])][:12],
            "abstract": (item.get("abstract") or "")[:1500],
            "doi": ext.get("DOI") or "",
            "arxiv_id": ext.get("ArXiv") or "",
            "pdf_url": pdf.get("url") or "",
            "url": item.get("url") or "",
        },
    }


# ------------------------------------------------------------------ 引用网络
# 引文图的补边数据源。字段刻意比 search 轻：这里要的是"谁引了谁"，
# 摘要是 10 倍体积而构图用不上（正文上下文由 `contexts` 提供）。
_NET_FIELDS = "title,year,venue,citationCount,authors,externalIds,url,contexts"


def s2_paper_id(paper_id: str) -> str:
    """校验并归一 S2 认的 paper id：`DOI:x` / `ARXIV:x` / `CorpusId:x` / 40 位 hash。

    **不做标题兜底**：S2 的 Graph API 路径里没有标题解析，传标题只会 404，
    与其让调用方拿到一个含糊的 404，不如在这里明确报错。
    """
    pid = (paper_id or "").strip()
    if pid.startswith(("DOI:", "ARXIV:", "CorpusId:")) and len(pid) > 6:
        return pid
    if len(pid) == 40 and pid.isalnum():  # S2 的 sha1 风格 hash
        return pid
    raise ValueError(
        f"不是合法的 Semantic Scholar paper id：{paper_id!r}（需 DOI:x / ARXIV:x / CorpusId:x / 40 位 hash）"
    )


def _edge_rows(data: list[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    """把 citations / references 的返回折成统一形状。

    两个端点的正文键不同（`citingPaper` vs `citedPaper`），其余结构一致 ——
    折在这里，调用方就不用记住哪个是哪个。
    """
    rows: list[dict[str, Any]] = []
    for item in data:
        paper = item.get(key) or {}
        if not paper.get("title"):
            continue
        ext = paper.get("externalIds") or {}
        rows.append(
            {
                "paper_id": paper.get("paperId", ""),
                "title": (paper.get("title") or "").strip(),
                "year": paper.get("year"),
                "venue": paper.get("venue") or "",
                "citation_count": paper.get("citationCount") or 0,
                "doi": ext.get("DOI") or "",
                "arxiv_id": ext.get("ArXiv") or "",
                "contexts": [c for c in (item.get("contexts") or []) if c][:3],
            }
        )
    return rows


@mcp.tool()
async def semantic_scholar_references(paper_id: str, limit: int = 50) -> dict[str, Any]:
    """取一篇论文的**参考文献**（它引了谁）—— 用于往"更早"的方向补引文边。

    何时使用：构建引文图、追溯某个方法的源头工作。
    何时不要用：问这篇论文自己讲了什么（用 retrieve_papers）。

    Args:
        paper_id: `DOI:10.x/y` / `ARXIV:2401.00001` / `CorpusId:123` / 40 位 hash。
            本地库里的论文用它的 DOI 或 arXiv 号拼前缀即可。
        limit: 返回条数，1-1000。

    Returns:
        {"total": int, "paper_id": str, "references": [{"paper_id","title","year",
        "venue","citation_count","doi","arxiv_id","contexts"}]}
    """
    limit = max(1, min(int(limit), 1000))
    key = s2_paper_id(paper_id)
    data = await get_json(
        f"{_BASE}/{key}/references",
        params={"limit": limit, "fields": _NET_FIELDS},
        headers=_headers(),
        timeout=settings.MCP_TIMEOUT,
    )
    rows = _edge_rows(data.get("data") or [], key="citedPaper")
    return {"total": len(rows), "paper_id": key, "references": rows}


@mcp.tool()
async def semantic_scholar_citations(paper_id: str, limit: int = 50) -> dict[str, Any]:
    """取一篇论文的**被引记录**（谁引了它）—— 用于往"更新"的方向补引文边。

    何时使用：找某工作之后的 follow-up、判断一篇论文是不是已被后续工作取代。
    何时不要用：找参考文献（用 semantic_scholar_references）。

    Args:
        paper_id: 同 `semantic_scholar_references`。
        limit: 返回条数，1-1000。

    Returns:
        {"total": int, "paper_id": str, "citations": [{"paper_id","title","year",
        "venue","citation_count","doi","arxiv_id","contexts"}]}
    """
    limit = max(1, min(int(limit), 1000))
    key = s2_paper_id(paper_id)
    data = await get_json(
        f"{_BASE}/{key}/citations",
        params={"limit": limit, "fields": _NET_FIELDS},
        headers=_headers(),
        timeout=settings.MCP_TIMEOUT,
    )
    rows = _edge_rows(data.get("data") or [], key="citingPaper")
    return {"total": len(rows), "paper_id": key, "citations": rows}


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
