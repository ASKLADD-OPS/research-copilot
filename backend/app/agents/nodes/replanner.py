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


class ReplanResult(BaseModel):
    decision: str = Field(default="continue", description="continue | replan | finish")
    reason: str = ""
    new_steps: list[PlanStep] = Field(default_factory=list)


async def replanner_node(state: AgentState) -> dict[str, Any]:
    plan = list(state.get("plan") or [])
    idx = state.get("current_step", 0)
    done, remaining = plan[:idx], plan[idx:]
    rnd = state.get("plan_round", 0)
    budget_left = rnd < settings.REPLAN_MAX_ROUNDS

    # 预算用尽：不再问 LLM，直接继续（省一次调用，也避免模型硬要 replan）
    if not budget_left or not remaining:
        return {
            "current_step": idx,
            "trace": [
                {
                    "node": "replanner",
                    "decision": "continue",
                    "reason": "replan 预算用尽" if not budget_left else "无剩余步骤",
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
        # 已完成的步骤必须保留：重规划只替换剩余部分
        base = len(done)
        new_plan = done + [
            PlanStep(**{**s, "idx": base + i + 1, "status": "pending"}) for i, s in enumerate(result.new_steps)
        ]
        logger.info("重规划第 {} 轮：{} 步 → {} 步", rnd + 1, len(plan), len(new_plan))
        return {
            "plan": new_plan,
            "plan_round": rnd + 1,
            "current_step": idx,
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


__all__ = ["ReplanResult", "replanner_node", "route_after_replan"]
