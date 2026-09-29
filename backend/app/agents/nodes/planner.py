"""Planner 节点 —— Plan-and-Execute 的规划端，用高性能模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState, PlanStep
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured


class PlanResult(BaseModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=6)
    reasoning: str = ""


def _fallback_plan(intent: str | None) -> list[PlanStep]:
    """LLM 不可用时的最小可用计划 —— 一步检索，好过整图崩掉。"""
    if intent == "chitchat":
        return [{"idx": 1, "goal": "直接回答", "tool": "none", "status": "pending"}]
    return [
        {
            "idx": 1,
            "goal": "从本地论文库检索与问题最相关的片段",
            "tool": "retrieve_papers",
            "status": "pending",
        }
    ]


async def planner_node(state: AgentState) -> dict[str, Any]:
    query = state.get("query", "")
    intent = state.get("intent")
    extra = {k: v for k, v in state.get("slot_filling", {}).items() if k != "clarify_options"}

    try:
        result = await complete_structured(
            PlanResult,
            [
                {"role": "system", "content": render("planner")},
                {
                    "role": "user",
                    "content": (
                        f"用户需求：{query}\n"
                        f"意图：{intent}\n"
                        f"槽位：{extra}\n"
                        f"指定论文：{state.get('target_papers') or '未指定'}"
                    ),
                },
            ],
            role=Role.PLANNER,  # 规划用强模型
        )
        steps = [PlanStep(**{**s, "idx": i + 1, "status": "pending"}) for i, s in enumerate(result.steps)]
        reasoning = result.reasoning
    except Exception as exc:  # noqa: BLE001
        logger.warning("规划失败，退化单步计划: {}", exc)
        steps, reasoning = _fallback_plan(intent), f"规划失败退化为单步：{exc}"

    logger.info("计划 {} 步：{}", len(steps), [s.get("tool") for s in steps])
    return {
        "plan": steps,
        "plan_round": state.get("plan_round", 0),
        "current_step": 0,
        "trace": [{"node": "planner", "steps": steps, "reasoning": reasoning}],
    }


def route_after_planner(state: AgentState) -> str:
    """计划为空就直接收尾，别白跑 executor。"""
    return "executor" if state.get("plan") else "synthesizer"


__all__ = ["PlanResult", "planner_node", "route_after_planner"]
