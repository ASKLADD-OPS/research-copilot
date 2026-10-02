"""MCP 工具注册表 —— Agent 侧调工具的唯一入口。

分两类：
- **remote**：来自 MCP Server（arXiv / PubMed / S2 / python-exec / web-search），
  由 `app/agents/mcp/client.py` 装载。
- **local**：本进程内的原生能力（库内检索、图分析、出图、写作、翻译）。
  它们不是"外部工具"，封成 MCP Server 只增加一次序列化开销，所以留在这里 ——
  但**对 Agent 暴露的接口与 remote 完全一致**（同名 dispatch）。

`call_tool` 做三件事：找工具 → 调 → 统一错误包装（工具失败降级为字符串回喂给
LLM，而不是抛异常打断整图）。
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.mcp.client import get_toolbox

LocalTool = Callable[..., Awaitable[Any]]

# ---------------------------------------------------------------- 本地工具
# 名 → (模块路径, 函数名)。懒加载，避免 import 期拉起 Milvus/NetworkX。
_LOCAL: dict[str, tuple[str, str]] = {
    "retrieve_papers": ("app.agents.mcp.local_tools", "retrieve_papers"),
    "graph_analyze": ("app.agents.mcp.local_tools", "graph_analyze"),
    "make_chart": ("app.agents.mcp.local_tools", "make_chart"),
    "write_section": ("app.agents.mcp.local_tools", "write_section"),
    "translate_text": ("app.agents.mcp.local_tools", "translate_text"),
}


def _resolve_local(name: str) -> LocalTool | None:
    spec = _LOCAL.get(name)
    if not spec:
        return None
    module = importlib.import_module(spec[0])
    fn = getattr(module, spec[1], None)
    return fn if callable(fn) else None


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    """调用工具。remote 优先，其次是本地工具。

    工具不存在 → 返回可读字符串（回喂给 LLM 让它换工具），不抛异常。
    """
    args = args or {}

    toolbox = get_toolbox()
    if not toolbox._loaded:  # noqa: SLF001 - 首次调用时惰性装载
        await toolbox.load()
    if toolbox.get(name) is not None:
        try:
            return await toolbox.ainvoke(name, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP 工具 {} 调用失败: {}", name, exc)
            return f"工具 {name} 调用失败：{type(exc).__name__}: {exc}"

    local = _resolve_local(name)
    if local is not None:
        try:
            return await local(**args)
        except TypeError as exc:
            return f"工具 {name} 参数不匹配：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地工具 {} 调用失败: {}", name, exc)
            return f"工具 {name} 调用失败：{type(exc).__name__}: {exc}"

    known = sorted(set(_LOCAL) | set(toolbox._tools))  # noqa: SLF001
    return f"未知工具 `{name}`。可用工具：{', '.join(known)}"


def tool_names() -> list[str]:
    """**本地**工具名（不含 MCP Server 的工具 —— 那批要 await 装载）。"""
    return sorted(set(_LOCAL))


# ---------------------------------------------------------------- 意图 → 工具
# 8 类 intent 各自该看到哪些工具。工具全量塞进 prompt 会稀释模型的注意力，
# 而且"该用 arxiv_search 的场合给了 translate_text"是选错工具的常见原因。
# key 必须与 intent 节点的输出一致（app/agents/nodes/intent.py）。
_INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    "single_paper_qa": ("retrieve_papers",),
    "cross_paper_reasoning": ("retrieve_papers", "graph_analyze"),
    "literature_search": ("arxiv_search", "pubmed_search", "semantic_scholar_search", "web_search"),
    "graph_analysis": ("graph_analyze", "retrieve_papers"),
    "writing_assist": ("write_section", "retrieve_papers"),
    "visualization": ("make_chart", "python_exec"),
    "translation": ("translate_text",),
    "chitchat": (),  # 闲聊不该调任何工具，给空集才是对的
}


async def get_tools_for_intent(intent: str | None) -> list[str]:
    """该意图下应暴露给 LLM 的工具名（升序）。

    只返回**当前确实可用**的名字：白名单里配了但 Server 被关掉（缺 API key）
    的工具会被 intersection 掉，避免 prompt 里出现一个调不通的工具。
    未登记的意图（含 None / 空串）返回全量 —— 宁可多给，也别让一个新意图
    拿到空工具集导致整步无工具可用。
    """
    toolbox = get_toolbox()
    if not toolbox._loaded:  # noqa: SLF001 - 与 call_tool 同一套惰性装载
        await toolbox.load()

    available = set(_LOCAL) | set(toolbox._tools)  # noqa: SLF001
    allowed = _INTENT_TOOLS.get((intent or "").strip())
    if allowed is None:
        return sorted(available)
    return sorted(available & set(allowed))


__all__ = ["call_tool", "get_tools_for_intent", "tool_names"]
