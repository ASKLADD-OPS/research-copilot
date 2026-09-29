"""Reflector 节点 —— Reflection 范式。4 维评分，低分触发 refine（上限 2 次）。

Reflexion 机制的落点：每轮评审的分数、批评、以及触发 refine 的原因都写进
state["trace"]，形成可追踪日志。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured

# 权重与门限集中在此，改策略只动这一处
WEIGHTS = {"faithfulness": 0.40, "relevance": 0.25, "coherence": 0.20, "completeness": 0.15}
PASS_OVERALL = 0.75
PASS_FAITHFULNESS = 0.80  # 一票否决项


class ReflectionResult(BaseModel):
    scores: dict[str, float] = Field(default_factory=dict)
    critique: str = ""
    fix_hint: str = ""


def score_of(scores: dict[str, float]) -> tuple[float, str]:
    """算加权总分并判定 pass / refine。纯函数，便于单测。"""
    known = {k: float(scores.get(k, 0.0)) for k in WEIGHTS}
    overall = sum(WEIGHTS[k] * v for k, v in known.items())
    ok = overall >= PASS_OVERALL and known["faithfulness"] >= PASS_FAITHFULNESS
    return round(overall, 4), ("pass" if ok else "refine")


async def reflector_node(state: AgentState) -> dict[str, Any]:
    draft = state.get("draft") or _draft_from_plan(state)
    if not draft.strip():
        return {
            "reflection": {
                "scores": {},
                "overall": 0.0,
                "verdict": "refine",
                "critique": "草稿为空",
                "round": state.get("refine_round", 0),
            },
            "trace": [{"node": "reflector", "verdict": "refine", "critique": "草稿为空"}],
        }

    context = _context_of(state)
    try:
        result = await complete_structured(
            ReflectionResult,
            [
                {"role": "system", "content": render("reflector")},
                {
                    "role": "user",
                    "content": (f"用户问题：{state.get('query')}\n\n检索上下文：\n{context}\n\n待评审草稿：\n{draft}"),
                },
            ],
            role=Role.REVIEWER,  # 评审用稳定模型
        )
        scores, critique, hint = result.scores, result.critique, result.fix_hint
    except Exception as exc:  # noqa: BLE001
        # 评审本身失败不能阻断主链 —— 放行，但记一笔
        logger.warning("Reflector 失败，放行草稿: {}", exc)
        scores, critique, hint = dict.fromkeys(WEIGHTS, 1.0), f"评审器不可用：{exc}", ""

    overall, verdict = score_of(scores)
    rnd = state.get("refine_round", 0)
    logger.info("评审 overall={:.2f} verdict={} round={}", overall, verdict, rnd)

    patch: dict[str, Any] = {
        "reflection": {
            "scores": scores,
            "overall": overall,
            "verdict": verdict,
            "critique": critique,
            "fix_hint": hint,
            "round": rnd,
        },
        "trace": [
            {
                "node": "reflector",
                "round": rnd,
                "scores": scores,
                "overall": overall,
                "verdict": verdict,
                "critique": critique,
            }
        ],
    }

    # 要 refine：指针拨回第 0 步、轮次 +1。上限判断在 route_after_reflect 里做。
    if verdict == "refine":
        patch["refine_round"] = rnd + 1
        patch["current_step"] = 0

    return patch


def route_after_reflect(state: AgentState) -> str:
    """refine 条件边：verdict=refine 且未超轮次 → 回 executor 重写。"""
    refl = state.get("reflection") or {}
    rounds_left = state.get("refine_round", 0) < settings.REFLECTION_MAX_REFINE
    return "refine" if refl.get("verdict") == "refine" and rounds_left else "synthesizer"


def _draft_from_plan(state: AgentState) -> str:
    """没有独立 draft 时（executor 未生成草稿），用各步 result 拼一个。"""
    return "\n\n".join(str(s.get("result", "")) for s in (state.get("plan") or []) if s.get("result"))


def _context_of(state: AgentState) -> str:
    from app.rag.retriever import to_context_block

    docs = state.get("reranked") or state.get("retrieved") or []
    if not docs:
        return "（无检索上下文）"
    from app.rag.retriever import RetrievedChunk

    chunks = [
        RetrievedChunk(
            id=str(d.get("chunk_id", "")),
            paper_id=str(d.get("paper_id", "")),
            content=str(d.get("text", "")),
            section=d.get("section") or None,
        )
        for d in docs
    ]
    return to_context_block(chunks, max_chars=8000)


__all__ = [
    "PASS_FAITHFULNESS",
    "PASS_OVERALL",
    "WEIGHTS",
    "ReflectionResult",
    "reflector_node",
    "route_after_reflect",
    "score_of",
]
