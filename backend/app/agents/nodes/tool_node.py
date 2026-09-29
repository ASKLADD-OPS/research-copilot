"""Tool 节点 —— 独立于 Executor 的工具调用节点。

Executor 内嵌 ReAct 已能调工具；本节点供"图层面直连工具"的场景使用
（例如计划里只有一步检索、无需 LLM 参与决策），避免为一个确定性调用
再绕一圈 ReAct。
"""

from __future__ import annotations

from typing import Any

from app.agents.mcp.registry import call_tool
from app.agents.state import AgentState
from app.core.logging import logger


async def tool_node(state: AgentState) -> dict[str, Any]:
    """执行计划当前步骤指定的工具，结果写进 plan[i].result。"""
    plan = list(state.get("plan") or [])
    idx = state.get("current_step", 0)
    if not (0 <= idx < len(plan)):
        return {"done": True, "trace": [{"node": "tool", "note": "无待执行步骤"}]}

    step = plan[idx]
    name = str(step.get("tool") or "")
    args = {"query": state.get("rewritten_query") or state.get("query", "")}

    try:
        result = await call_tool(name, args)
        text = str(result)[:4000]
        status = "done"
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具节点 {} 失败: {}", name, exc)
        text, status = f"工具不可用：{type(exc).__name__}: {exc}", "failed"

    plan[idx] = {**step, "status": status, "result": text}
    return {
        "plan": plan,
        "current_step": idx + 1,
        "thoughts": [{"node": "tool", "tool": name, "args": args, "result": text[:600]}],
        "trace": [{"node": "tool", "tool": name, "status": status}],
    }


def route_after_tool(state: AgentState) -> str:
    plan = state.get("plan") or []
    return "tool" if state.get("current_step", 0) < len(plan) else "reflector"


__all__ = ["route_after_tool", "tool_node"]
