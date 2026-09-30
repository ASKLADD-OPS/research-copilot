"""Executor 节点 —— Plan-and-Execute 的执行端（内嵌 ReAct 循环）。

按 `current_step` 取计划中的一步，喂给 LLM 做 ReAct，最多
`settings.AGENT_MAX_REACT_ROUNDS` 轮 Thought→Action→Observation。
用性价比模型（Role.EXECUTOR）。
"""

from __future__ import annotations

from typing import Any

from app.agents.prompts import render
from app.agents.state import AgentState
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Message, Role, get_llm
from app.rag.retriever import to_context_block

_ACTION_RE = __import__("re").compile(
    r"Action\s*[:：]\s*(\w+)\s*[\r\n]+\s*Action\s*Input\s*[:：]\s*(\{.*?\})",
    __import__("re").DOTALL,
)


def current_step(state: AgentState) -> dict[str, Any] | None:
    """取当前待执行步骤（越界返回 None）。"""
    plan = state.get("plan") or []
    idx = state.get("current_step", 0)
    return plan[idx] if 0 <= idx < len(plan) else None


def parse_action(text: str) -> tuple[str, dict[str, Any]] | None:
    """从 LLM 输出里抽 Action / Action Input。解析不出返回 None。"""
    import json

    m = _ACTION_RE.search(text or "")
    if not m:
        return None
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    return m.group(1), args if isinstance(args, dict) else {}


def _context_of(state: AgentState, max_chars: int = 12000) -> str:
    """把已检索片段转成带编号的上下文（编号即引用白名单）。"""
    from app.rag.retriever import RetrievedChunk

    docs = state.get("reranked") or state.get("retrieved") or []
    if not docs:
        return "（无检索上下文）"
    chunks = [
        RetrievedChunk(
            id=str(d.get("chunk_id", "")),
            paper_id=str(d.get("paper_id", "")),
            content=str(d.get("text", "")),
            section=d.get("section") or None,
            page_start=d.get("page") or None,
        )
        for d in docs
    ]
    return to_context_block(chunks, max_chars=max_chars)


async def executor_node(state: AgentState) -> dict[str, Any]:
    """执行当前步骤。ReAct 循环内联在本节点（不单独建图边，减少状态搬运）。"""
    step = current_step(state)
    if step is None:
        return {"done": True, "trace": [{"node": "executor", "note": "无待执行步骤"}]}

    thought_log: list[dict[str, Any]] = [{"node": "executor", "step": step.get("idx"), "goal": step.get("goal")}]
    fix_hint = (state.get("reflection") or {}).get("fix_hint", "") if state.get("refine_round") else ""
    messages: list[Message] = [
        {"role": "system", "content": render("executor")},
        {
            "role": "user",
            "content": (
                f"用户需求：{state.get('query')}\n"
                f"本步目标：{step.get('goal')}\n"
                f"建议工具：{step.get('tool')}\n"
                + (f"⚠️ 上一稿被评审打回，必须修正：{fix_hint}\n" if fix_hint else "")
                + f"\n可用上下文：\n{_context_of(state)}"
            ),
        },
    ]

    llm = get_llm()
    answer = ""
    for rnd in range(settings.AGENT_MAX_REACT_ROUNDS):
        try:
            out = await llm.complete(Role.EXECUTOR, messages, temperature=0.2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Executor LLM 调用失败: {}", exc)
            thought_log.append({"node": "executor", "round": rnd, "error": str(exc)})
            break

        messages.append({"role": "assistant", "content": out})
        action = parse_action(out)

        if action is None:  # 没有 Action → 视为最终答复
            answer = out.strip()
            thought_log.append({"node": "executor", "round": rnd, "final": answer[:400]})
            break

        tool_name, tool_args = action
        from app.agents.mcp.registry import call_tool

        try:
            observation = await call_tool(tool_name, tool_args)
            obs_text = str(observation)[:4000]
        except Exception as exc:  # noqa: BLE001 - 工具失败要回喂给模型，不能抛出
            obs_text = f"工具调用失败：{type(exc).__name__}: {exc}"
            logger.warning("工具 {} 失败: {}", tool_name, exc)

        messages.append({"role": "user", "content": f"Observation: {obs_text}"})
        thought_log.append(
            {"node": "executor", "round": rnd, "action": tool_name, "args": tool_args, "obs": obs_text[:600]}
        )
    else:
        # 轮次用尽仍未收敛：把最后一次输出当结论，并记一笔
        answer = messages[-1].get("content", "") if messages else ""
        thought_log.append({"node": "executor", "note": f"ReAct 达上限 {settings.AGENT_MAX_REACT_ROUNDS} 轮"})

    plan = list(state.get("plan") or [])
    idx = state.get("current_step", 0)
    if 0 <= idx < len(plan):
        plan[idx] = {**plan[idx], "status": "done", "result": answer[:1000]}

    return {
        "plan": plan,
        "current_step": idx + 1,
        "thoughts": thought_log,
        "trace": thought_log,
    }


def route_after_execute(state: AgentState) -> str:
    """还有步骤就继续 executor；否则交给 replanner —— 由它决定 re-plan 还是转 reflector 评审。

    注意：这里**不能**直接返回 "reflector"。graph.py 里 executor 的边表只声明了
    `{"executor", "replanner"}`，LangGraph 会把未声明的返回值当成 KeyError 抛出：
        File "langgraph/graph/_branch.py", line 203, in _finish
            r if isinstance(r, Send) else self.ends[r] for r in result
        KeyError: 'reflector'
    改这一行之前，所有走 Planner 的意图（writing_assist / translation / visualization）
    都在第一轮执行完就崩，SSE 只吐得出 error 帧。
    """
    plan = state.get("plan") or []
    return "executor" if state.get("current_step", 0) < len(plan) else "replanner"


__all__ = ["current_step", "executor_node", "parse_action", "route_after_execute"]
