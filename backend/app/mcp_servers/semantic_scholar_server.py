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


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
