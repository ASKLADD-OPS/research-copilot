"""Replanner 节点 —— 根据执行结果动态调整剩余计划（max 2 轮）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState, PlanStep
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured

# "未完成"的步骤状态：只有这两种值得再规划。
# "replanned" 是**已被取代**（本模块重规划时打的标），"done" 是已完成。
OPEN_STATUSES = ("pending", "failed")


class ReplanResult(BaseModel):
    decision: str = Field(default="continue", description="continue | replan | finish")
    reason: str = ""
    new_steps: list[PlanStep] = Field(default_factory=list)


async def replanner_node(state: AgentState) -> dict[str, Any]:
    """决定"是否补一步"。

    `remaining` 曾经写作 `plan[current_step:]`，而本节点**只在计划跑完时被进入**
    （`route_after_execute` 只在 `current_step >= len(plan)` 时指过来），
    于是它恒为空列表、"replan" 分支是一段死代码 —— 工具失败后只会一路滑到评审。
    现在按**状态**取未完成步骤（含 failed），失败才有可观测的重规划触发条件。

    重规划采取**尾追**而不是替换剩余段：已完成/已取代的步骤保持原位，
    新步骤追加到队尾，`current_step` 停在原计划长度即指向第一个新步骤。
    """
    plan = list(state.get("plan") or [])
    idx = state.get("current_step", 0)
    done = [s for s in plan if s.get("status") == "done"]
    # 只有 pending / failed 算"未完成"。被重规划取代的步骤状态是 "replanned"，
    # 它既没成功也不该再触发下一轮重规划 —— 漏掉这一条会让每次重规划都再重规划一次。
    remaining = [s for s in plan if s.get("status") in OPEN_STATUSES]
    rnd = state.get("plan_round", 0)
    budget_left = rnd < settings.REPLAN_MAX_ROUNDS

    # 全部完成 或 预算用尽：不问 LLM，直接继续（省一次调用，也避免模型硬要 replan）
    if not budget_left or not remaining:
        return {
            "current_step": max(idx, len(plan)),
            "trace": [
                {
                    "node": "replanner",
                    "decision": "continue",
                    "reason": "replan 预算用尽" if not budget_left else "无未完成步骤",
                    "round": rnd,
                }
            ],
        }

    try:
        result = await complete_structured(
            ReplanResult,
            [
                {"role": "system", "content": render("replanner")},
                {
                    "role": "user",
                    "content": (
                        f"用户需求：{state.get('query')}\n\n"
                        f"已完成步骤与结果：\n{_fmt(done)}\n\n"
                        f"剩余待执行步骤：\n{_fmt(remaining, with_status=False)}\n\n"
                        f"当前 replan 轮次：{rnd}/{settings.REPLAN_MAX_ROUNDS}"
                    ),
                },
            ],
            role=Role.PLANNER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Replanner 失败，按原计划继续: {}", exc)
        return {
            "trace": [{"node": "replanner", "decision": "continue", "reason": f"调用失败：{exc}"}],
        }

    if result.decision == "replan" and result.new_steps:
        # 失败步骤标 "replanned"：它已被新步骤取代，不该在下一轮再次触发重规划。
        base = len(plan)
        new_plan = [{**s, "status": "replanned"} if s.get("status") == "failed" else s for s in plan] + [
            PlanStep(**{**s, "idx": base + i + 1, "status": "pending"}) for i, s in enumerate(result.new_steps)
        ]
        logger.info("重规划第 {} 轮：{} 步 → {} 步", rnd + 1, len(plan), len(new_plan))
        return {
            "plan": new_plan,
            "plan_round": rnd + 1,
            "current_step": base,  # 指向第一个新步骤
            "trace": [
                {
                    "node": "replanner",
                    "decision": "replan",
                    "reason": result.reason,
                    "round": rnd + 1,
                    "new_steps": result.new_steps,
                }
            ],
        }

    return {
        "current_step": len(plan) if result.decision == "finish" else idx,
        "trace": [{"node": "replanner", "decision": result.decision, "reason": result.reason, "round": rnd}],
    }


def _fmt(steps: list[dict[str, Any]], *, with_status: bool = True) -> str:
    if not steps:
        return "（无）"
    lines = []
    for s in steps:
        line = f"{s.get('idx')}. [{s.get('tool')}] {s.get('goal')}"
        if with_status:
            line += f" → {str(s.get('result', ''))[:300]}"
        lines.append(line)
    return "\n".join(lines)


def route_after_replan(state: AgentState) -> str:
    """还有步骤就继续执行，否则去评审。"""
    plan = state.get("plan") or []
    return "executor" if state.get("current_step", 0) < len(plan) else "reflector"


__all__ = ["OPEN_STATUSES", "ReplanResult", "replanner_node", "route_after_replan"]
