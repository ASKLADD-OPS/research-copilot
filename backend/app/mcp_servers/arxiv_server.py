"""arXiv MCP Server —— 检索预印本。

独立运行：`python -m app.mcp_servers.arxiv_server`（stdio，供 MultiServerMCPClient 拉起）
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings
from app.mcp_servers._common import clean_text, get_text

mcp = FastMCP("arxiv")

_FEED = "https://export.arxiv.org/api/query"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_FIELD = {
    "id": re.compile(r"<id>(.*?)</id>", re.DOTALL),
    "title": re.compile(r"<title>(.*?)</title>", re.DOTALL),
    "summary": re.compile(r"<summary>(.*?)</summary>", re.DOTALL),
    "published": re.compile(r"<published>(.*?)</published>", re.DOTALL),
}
_AUTHOR = re.compile(r"<name>(.*?)</name>", re.DOTALL)
_CATEGORY = re.compile(r'<category[^>]*term="([^"]+)"')


def _grab(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


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

    return {"total": len(results), "query": query, "results": results}


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
