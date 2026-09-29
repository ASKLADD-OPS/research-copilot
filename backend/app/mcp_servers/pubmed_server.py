"""PubMed MCP Server —— 生物医学文献检索（NCBI E-utilities）。

独立运行：`python -m app.mcp_servers.pubmed_server`
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings
from app.mcp_servers._common import get_json

mcp = FastMCP("pubmed")

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _auth() -> dict[str, str]:
    return {"api_key": settings.NCBI_API_KEY} if settings.NCBI_API_KEY else {}


@mcp.tool()
async def pubmed_search(query: str, max_results: int = 10, year_from: int | None = None) -> dict[str, Any]:
    """检索 PubMed 生物医学文献。

    何时使用：医学、生物学、公共卫生方向，或用户明确要求 PubMed。
    何时不要用：计算机/物理方向（用 arxiv_search），或问已入库论文（用 retrieve_papers）。

    Args:
        query: 检索式，支持 PubMed 语法，如 `(CRISPR[Title]) AND 2023:2025[dp]`。
        max_results: 返回条数，1-50。
        year_from: 只保留该年份之后的文献（可选）。

    Returns:
        {"total": int, "results": [{"pmid","title","authors","journal","pubdate","url"}]}
    """
    max_results = max(1, min(int(max_results), 50))
    term = f"({query}) AND {year_from}:3000[dp]" if year_from else query
    common = {"db": "pubmed", "retmode": "json", **_auth()}

    search = await get_json(
        _ESEARCH,
        params={**common, "term": term, "retmax": max_results, "sort": "relevance"},
        timeout=settings.MCP_TIMEOUT,
    )
    ids = search.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return {"total": 0, "query": query, "results": []}

    summary = await get_json(
        _ESUMMARY,
        params={**common, "id": ",".join(ids)},
        timeout=settings.MCP_TIMEOUT,
    )
    payload = summary.get("result", {})

    results: list[dict[str, Any]] = []
    for pmid in ids:
        item = payload.get(pmid) or {}
        results.append(
            {
                "pmid": pmid,
                "title": (item.get("title") or "").strip(),
                "authors": [a.get("name", "") for a in (item.get("authors") or [])][:12],
                "journal": item.get("fulljournalname") or item.get("source") or "",
                "pubdate": item.get("pubdate") or "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )

    return {"total": len(results), "query": query, "results": results}


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
