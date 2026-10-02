"""MultiServerMCPClient 封装 —— 把多个 MCP Server 的工具装载为 LangChain Tool。

设计取舍：
- **进程内直调优先**：同进程 import 工具函数比 stdio 起子进程快两个数量级，
  且没有 JSON-RPC 序列化开销。`MCP_TRANSPORT=inproc` 时走这条（默认）。
- **stdio 兜底**：`MCP_TRANSPORT=stdio` 时用 langchain-mcp-adapters 拉起子进程，
  用于验证"跨进程也能跑"和生产隔离。

两条路径对上层暴露同一个接口（名字 → 可调用），所以 Agent 无感。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.core.config import settings
from app.mcp_servers import SERVER_MODULES

# 哪些 Server 受开关控制（未配置 key 的也一并关掉）
_SERVER_FLAG: dict[str, str] = {
    "arxiv": "MCP_ARXIV_ENABLED",
    "pubmed": "MCP_PUBMED_ENABLED",
    "semantic-scholar": "MCP_SEMANTIC_SCHOLAR_ENABLED",
    "python-exec": "MCP_PYTHON_EXEC_ENABLED",
    "web-search": "MCP_WEB_SEARCH_ENABLED",
}

_BACKOFF_BASE = 0.5  # 指数退避基数（秒）；测试里调到 0 免得白等


def backoff_delay(attempt: int) -> float:
    """第 `attempt`（从 0 起）次重试前该等多久：0.5s → 1s → 2s ..."""
    return _BACKOFF_BASE * (2**attempt)


def enabled_servers() -> list[str]:
    """按配置开关过滤出启用的 Server 名。"""
    return [name for name, flag in _SERVER_FLAG.items() if getattr(settings, flag, True)]


class MCPToolbox:
    """工具注册表 + 调用入口。

    进程内直调：工具函数从对应 server 模块按名取（`@mcp.tool()` 装饰后原函数
    仍可通过闭包拿到 —— 这里改用 FastMCP 的工具管理器读取，避免依赖装饰器行为）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------ 装载
    async def load(self) -> dict[str, Any]:
        """装载工具，返回 {工具名: LangChain BaseTool}。幂等。"""
        async with self._lock:
            if self._loaded:
                return self._tools
            if settings.MCP_TRANSPORT == "stdio":
                self._tools = await self._load_via_stdio()
            else:
                self._tools = await self._load_inproc()
            self._loaded = True
            logger.info("MCP 工具装载完成（{}）：{}", settings.MCP_TRANSPORT, sorted(self._tools))
            return self._tools

    async def _load_inproc(self) -> dict[str, Any]:
        """进程内加载：直接用 FastMCP 的 list_tools 拿 schema，再包成 LangChain Tool。"""
        from langchain_core.tools import StructuredTool

        tools: dict[str, Any] = {}
        for server in enabled_servers():
            module = _import_server(server)
            if module is None:
                continue
            try:
                listed = await module.mcp.list_tools()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP Server {} 列工具失败: {}", server, exc)
                continue
            for spec in listed:
                fn: Callable[..., Any] | None = getattr(module, spec.name, None)
                if fn is None or not callable(fn):
                    logger.warning("工具 {} 在 {} 中找不到同名函数，跳过", spec.name, server)
                    continue
                tools[spec.name] = StructuredTool.from_function(
                    coroutine=fn if inspect.iscoroutinefunction(fn) else None,
                    func=None if inspect.iscoroutinefunction(fn) else fn,
                    name=spec.name,
                    description=(spec.description or "").strip(),
                )
        return tools

    async def _load_via_stdio(self) -> dict[str, Any]:
        """跨进程加载：MultiServerMCPClient 起子进程，按 MCP 协议取工具。"""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        servers = {
            name: {
                "command": settings.MCP_PYTHON,
                "args": ["-m", module],
                "transport": "stdio",
                "env": {"PYTHONIOENCODING": "utf-8", "PYTHONPATH": settings.MCP_PYTHONPATH},
            }
            for name, module in SERVER_MODULES.items()
            if name in enabled_servers()
        }
        if not servers:
            return {}
        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        return {t.name: t for t in tools}

    # ------------------------------------------------------------ 调用
    def get(self, name: str) -> Any | None:
        return self._tools.get(name)

    async def ainvoke(self, name: str, args: dict[str, Any]) -> Any:
        """调用工具：单次墙钟超时 + 指数退避重试。

        重试只针对**抛异常**的调用（网络抖动 / 5xx / 超时）。工具自己"成功返回
        一条错误说明"（如 python_exec 拒绝危险代码）不算失败，不会重试 —— 重试
        一个确定性拒绝只是白等。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具 {name}（已装载：{sorted(self._tools)}）")

        attempts = max(1, int(settings.MCP_TOOL_RETRIES) + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(settings.MCP_TOOL_TIMEOUT):
                    result = tool.ainvoke(args)
                    return await result if inspect.isawaitable(result) else result
            except Exception as exc:  # noqa: BLE001 - 交给末尾统一抛出
                last = exc
                if attempt + 1 >= attempts:
                    break
                delay = backoff_delay(attempt)
                logger.warning("工具 {} 第 {} 次调用失败（{}），{:.1f}s 后重试", name, attempt + 1, exc, delay)
                await asyncio.sleep(delay)
        raise last if last is not None else RuntimeError(f"工具 {name} 调用失败")

    def langchain_tools(self) -> list[Any]:
        return list(self._tools.values())


def _import_server(server: str) -> Any | None:
    import importlib

    module_path = SERVER_MODULES.get(server)
    if not module_path:
        return None
    try:
        return importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - 单个 Server 挂了不该拖垮其它工具
        logger.warning("MCP Server {} 导入失败: {}", server, exc)
        return None


_toolbox: MCPToolbox | None = None


def get_toolbox() -> MCPToolbox:
    global _toolbox
    if _toolbox is None:
        _toolbox = MCPToolbox()
    return _toolbox


async def load_all_mcp_tools() -> list[Any]:
    """装载全部 MCP Server 的工具，返回 LangChain `BaseTool` 列表。

    这就是"工具总入口"：拿到的列表可以直接喂给
    `create_react_agent(model, tools)` 或自定义图的 tool 节点。
    幂等 —— 首次装载后走缓存，不会每个请求重新起一遍 Server。
    """
    toolbox = get_toolbox()
    await toolbox.load()
    return toolbox.langchain_tools()


__all__ = ["MCPToolbox", "backoff_delay", "enabled_servers", "get_toolbox", "load_all_mcp_tools"]
