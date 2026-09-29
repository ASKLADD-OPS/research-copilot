"""MCP 集成层 —— Agent 侧如何拿到工具。

- `client.py`：装载 MCP Server 的工具（进程内 / stdio 两条路径）
- `registry.py`：统一的 `call_tool(name, args)` 分发入口（remote + local）
- `local_tools.py`：进程内原生工具（库内检索、图分析、出图、写作、翻译）
"""

from app.agents.mcp.client import MCPToolbox, get_toolbox
from app.agents.mcp.registry import call_tool, tool_names

__all__ = ["MCPToolbox", "call_tool", "get_toolbox", "tool_names"]
