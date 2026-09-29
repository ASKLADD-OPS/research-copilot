"""Clarify 节点 —— 低置信度时追问一句，图到此暂停等用户回答。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured


class ClarifyResult(BaseModel):
    question: str = Field(description="一句话，必须给出 2-4 个具体候选")
    options: list[str] = Field(default_factory=list, max_length=4)


async def clarify_node(state: AgentState) -> dict[str, Any]:
    """生成澄清问题。LLM 挂了也要给用户一个能用的兜底问句。"""
    query = state.get("query", "")
    try:
        result = await complete_structured(
            ClarifyResult,
            [
                {"role": "system", "content": render("clarify")},
                {
                    "role": "user",
                    "content": (
                        f"用户原话：{query}\n初步意图：{state.get('intent')}（置信度 {state.get('confidence', 0):.2f}）"
                    ),
                },
            ],
            role=Role.UTILITY,
        )
        question, options = result.question, result.options
    except Exception as exc:  # noqa: BLE001
        logger.warning("澄清生成失败，用默认问句: {}", exc)
        question, options = f"能再说清楚一点吗？你想问的是：{query[:40]}", []

    return {
        "clarify_question": question,
        "answer": question + ("\n\n" + " / ".join(options) if options else ""),
        "done": True,
        "slot_filling": {**state.get("slot_filling", {}), "clarify_options": options},
        "trace": [{"node": "clarify", "question": question, "options": options}],
    }


__all__ = ["ClarifyResult", "clarify_node"]
