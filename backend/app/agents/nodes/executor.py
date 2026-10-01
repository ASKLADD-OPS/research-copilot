"""Executor 节点 —— Plan-and-Execute 的执行端（内嵌 ReAct 循环）。

按计划里的**就绪步骤**取活：依赖（`dependencies`）全部 done 的 pending 步骤才算就绪；
同一个 `parallel_group` 的就绪步骤**并发**跑（`asyncio.gather`），其余逐个跑。
每一步内部最多 `settings.AGENT_MAX_REACT_ROUNDS` 轮 Thought→Action→Observation。
用性价比模型（Role.EXECUTOR）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.agents.prompts import render
from app.agents.state import AgentState, PlanStep
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Message, Role, get_llm
from app.rag.retriever import to_context_block

_ACTION_RE = re.compile(
    r"Action\s*[:：]\s*(\w+)\s*[\r\n]+\s*Action\s*Input\s*[:：]\s*(\{.*?\})",
    re.DOTALL,
)


# ---------------------------------------------------------------- 计划 → 可执行批次
def _status_of(step: PlanStep) -> str:
    """缺 status 一律当 pending。

    重规划尾追的新步骤、HITL 里用户手改的计划都可能不带 status，
    把它们当成"已完成"会让整段计划被静默跳过。
    """
    return str(step.get("status") or "pending")


def _done_idx(plan: list[PlanStep]) -> set[int]:
    out: set[int] = set()
    for s in plan:
        if s.get("status") == "done" and s.get("idx") is not None:
            try:
                out.add(int(s["idx"]))
            except (TypeError, ValueError):
                continue
    return out


def ready_indices(plan: list[PlanStep]) -> list[int]:
    """**按下标**返回"依赖已满足且尚未执行"的步骤，升序。纯函数。"""
    done = _done_idx(plan)
    ready: list[int] = []
    for i, s in enumerate(plan):
        if _status_of(s) != "pending":
            continue
        deps = s.get("dependencies") or []
        ok = True
        for d in deps:
            try:
                if int(d) not in done:
                    ok = False
                    break
            except (TypeError, ValueError):
                ok = False
                break
        if ok:
            ready.append(i)
    return ready


def next_batch(plan: list[PlanStep]) -> list[int]:
    """本批要执行的步骤下标：锚点 + 与它同并行组的就绪成员。

    锚点 = 就绪集合里下标最小的一步（保持计划的书写顺序）。无 `parallel_group`
    时本批只有锚点一步 —— 串行计划的行为与改造前完全一致。
    """
    ready = ready_indices(plan)
    if not ready:
        return []
    anchor = ready[0]
    group = plan[anchor].get("parallel_group")
    if not group:
        return [anchor]
    return [i for i in ready if plan[i].get("parallel_group") == group]


def current_step(state: AgentState) -> PlanStep | None:
    """取当前指针所指的步骤（越界返回 None）。保留给外部/测试用。"""
    plan = state.get("plan") or []
    idx = state.get("current_step", 0)
    return plan[idx] if 0 <= idx < len(plan) else None


def parse_action(text: str) -> tuple[str, dict[str, Any]] | None:
    """从 LLM 输出里抽 Action / Action Input。解析不出返回 None —— 即"这轮是直接回答"。"""
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
            page=d.get("page") or None,
        )
        for d in docs
    ]
    return to_context_block(chunks, max_chars=max_chars)


async def _run_step(state: AgentState, step: PlanStep, fix_hint: str) -> tuple[str, str, list[dict[str, Any]]]:
    """跑完一步。返回 (status, answer, thought_log)。

    `status` 只在真的拿到结论时才是 "done" —— 没结论标 "failed"，
    这是 replanner 决定要不要补一步的唯一触发条件（见 replanner 模块头）。
    """
    thought_log: list[dict[str, Any]] = [{"node": "executor", "step": step.get("idx"), "goal": step.get("goal")}]
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
    concluded = False
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
            concluded = True
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

    return ("done" if concluded else "failed"), answer, thought_log


async def executor_node(state: AgentState) -> dict[str, Any]:
    """执行一批就绪步骤。同 `parallel_group` 的成员并发跑。"""
    plan = list(state.get("plan") or [])
    if not plan:
        return {"done": True, "trace": [{"node": "executor", "note": "计划为空"}]}

    batch = next_batch(plan)
    if not batch:
        # 没有就绪步骤 = 全跑完了，或者被 failed 的依赖卡住。
        # 指针推到队尾交给 replanner —— 留在原地会让 route_after_execute 反复指回 executor 空转。
        return {
            "current_step": len(plan),
            "trace": [{"node": "executor", "note": "无就绪步骤（已完成或依赖未满足）"}],
        }

    fix_hint = (state.get("reflection") or {}).get("fix_hint", "") if state.get("refine_round") else ""
    results = await asyncio.gather(*(_run_step(state, plan[i], fix_hint) for i in batch))

    thought_log: list[dict[str, Any]] = []
    for i, (status, answer, log) in zip(batch, results, strict=True):
        plan[i] = {**plan[i], "status": status, "result": answer[:1000]}
        thought_log.extend(log)

    remaining = ready_indices(plan)
    return {
        "plan": plan,
        "current_step": min(remaining) if remaining else len(plan),
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


__all__ = [
    "current_step",
    "executor_node",
    "next_batch",
    "parse_action",
    "ready_indices",
    "route_after_execute",
]
