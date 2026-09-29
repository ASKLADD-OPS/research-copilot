"""MCP Servers —— 所有外部工具的统一出口。

每个模块都是**可独立运行**的 FastMCP Server（stdio），由
`app/agents/mcp/client.py` 的 MultiServerMCPClient 拉起。
Agent 侧不直接 import 这里的函数，只通过 MCP 协议调用 —— 保证
"换实现不改 Agent"。
"""

from __future__ import annotations

SERVER_MODULES = {
    "arxiv": "app.mcp_servers.arxiv_server",
    "pubmed": "app.mcp_servers.pubmed_server",
    "semantic-scholar": "app.mcp_servers.semantic_scholar_server",
    "python-exec": "app.mcp_servers.python_exec_server",
    "web-search": "app.mcp_servers.web_search_server",
}

__all__ = ["SERVER_MODULES"]
