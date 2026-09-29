"""Intent 节点 —— 意图识别入口。

8 类意图 + 置信度 + 槽位。置信度 < 0.6 时由条件边送去 clarify，
本节点不自己决定走哪条路（路由逻辑集中在 graph.py，便于单测）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState, Intent
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured

CONFIDENCE_FLOOR = settings.INTENT_CONFIDENCE_THRESHOLD


class IntentResult(BaseModel):
    intent: Intent = Field(description="8 类意图之一")
    confidence: float = Field(ge=0.0, le=1.0, description="真实犹豫程度，不要一律给 0.95")
    target_papers: list[str] = Field(default_factory=list)
    slot_filling: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


async def intent_node(state: AgentState) -> dict[str, Any]:
    """识别意图。失败时退化为 single_paper_qa + 低置信度（走澄清，而不是乱答）。"""
    query = state.get("query", "").strip()
    if not query:
        return {
            "intent": "chitchat",
            "confidence": 1.0,
            "clarify_question": "请描述你想问的问题。",
            "trace": [{"node": "intent", "note": "空 query"}],
        }

    try:
        result = await complete_structured(
            IntentResult,
            [
                {"role": "system", "content": render("intent")},
                {"role": "user", "content": query},
            ],
            role=Role.UTILITY,
        )
    except Exception as exc:  # noqa: BLE001 - 分类失败不能拖垮整图
        logger.warning("意图识别失败，退化为澄清: {}", exc)
        return {
            "intent": "single_paper_qa",
            "confidence": 0.0,
            "target_papers": [],
            "slot_filling": {},
            "trace": [{"node": "intent", "error": str(exc)}],
        }

    logger.info("意图={} 置信度={:.2f} 槽位={}", result.intent, result.confidence, result.slot_filling)
    return {
        "intent": result.intent,
        "confidence": result.confidence,
        "target_papers": result.target_papers,
        "slot_filling": result.slot_filling,
        "trace": [
            {
                "node": "intent",
                "intent": result.intent,
                "confidence": result.confidence,
                "reason": result.reason,
            }
        ],
    }


def route_after_intent(state: AgentState) -> str:
    """条件边：低置信度走澄清，chitchat 直接答，其余进规划。"""
    if state.get("confidence", 0.0) < CONFIDENCE_FLOOR:
        return "clarify"
    intent = state.get("intent", "chitchat")
    if intent == "chitchat":
        return "synthesizer"
    return "planner"


__all__ = ["CONFIDENCE_FLOOR", "IntentResult", "intent_node", "route_after_intent"]
