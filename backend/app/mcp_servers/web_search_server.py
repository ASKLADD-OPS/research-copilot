"""Web Search MCP Server —— CRAG 的 irrelevant 兜底。

独立运行：`python -m app.mcp_servers.web_search_server`
需要 TAVILY_API_KEY；未配置时工具会明确报错而不是返回空结果（让 Agent 知道
是"没配"而不是"没搜到"，避免它硬编答案）。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings

mcp = FastMCP("web-search")

_TAVILY = "https://api.tavily.com/search"


@mcp.tool()
async def web_search(query: str, max_results: int = 5, topic: str = "general") -> dict[str, Any]:
    """联网检索网页，**仅作检索兜底**。

    何时使用：本地论文库与学术数据源都检索不到、或问题本身是时效性信息
    （最新进展、某个库的当前版本）。CRAG 判定为 irrelevant 时由系统自动调用。
    何时不要用：能靠本地库回答的问题（用 retrieve_papers）—— 网页内容不是
    同行评议证据，引用它会降低答案可信度。

    注意：返回结果附带 `source="web"` 标记，引用时必须标注为外部来源，
    不得与论文引用混为一谈。

    Args:
        query: 检索词。
        max_results: 返回条数，1-10。
        topic: `general` 或 `news`。

    Returns:
        {"total": int, "results": [{"title","url","snippet","score"}], "note": str}
    """
    if not settings.TAVILY_API_KEY:
        return {
            "total": 0,
            "results": [],
            "note": "未配置 TAVILY_API_KEY，联网兜底不可用。请在 .env 中填写后重试。",
        }

    max_results = max(1, min(int(max_results), 10))
    data = await _post(query, max_results, topic)

    results = [
        {
            "title": (it.get("title") or "").strip(),
            "url": it.get("url") or "",
            "snippet": (it.get("content") or "").strip()[:800],
            "score": it.get("score") or 0.0,
        }
        for it in data.get("results", [])
    ]
    return {
        "total": len(results),
        "results": results,
        "note": "网页结果仅作参考，引用时需标注为外部来源。",
    }


async def _post(query: str, max_results: int, topic: str) -> dict[str, Any]:
    import httpx

    from app.mcp_servers._common import UA

    payload = {
        "api_key": settings.TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "topic": topic if topic in ("general", "news") else "general",
        "search_depth": "basic",
    }
    async with httpx.AsyncClient(timeout=settings.MCP_TIMEOUT, trust_env=True) as client:
        resp = await client.post(_TAVILY, json=payload, headers={"User-Agent": UA})
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
