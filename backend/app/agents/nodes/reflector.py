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

# 两个**独立硬门限**：任一维度掉到这个线下就直接 refine，不看加权总分。
# 加权总分是个平均值 —— 它会把"faithfulness 0.3 但 completeness 1.0"平均成 0.72，
# 而一句编造的话不该被另外三项的高分赎回来。
DIM_FLOORS = {"faithfulness": 0.7, "relevance": 0.6}
# 本项目的 faithfulness 门限比 DIM_FLOORS 更严（一票否决项），取两者上界生效。
PASS_FAITHFULNESS = 0.80
PASS_OVERALL = 0.75


class ReflectionResult(BaseModel):
    scores: dict[str, float] = Field(default_factory=dict)
    critique: str = ""
    fix_hint: str = ""


def score_of(scores: dict[str, float]) -> tuple[float, str]:
    """算加权总分并判定 pass / refine。纯函数，便于单测。

    判 pass 需要同时满足：总分 ≥ PASS_OVERALL、faithfulness ≥ PASS_FAITHFULNESS、
    且没有任何维度低于 DIM_FLOORS。
    """
    known = {k: float(scores.get(k, 0.0)) for k in WEIGHTS}
    overall = sum(WEIGHTS[k] * v for k, v in known.items())
    floors_ok = all(known[dim] >= floor for dim, floor in DIM_FLOORS.items())
    ok = overall >= PASS_OVERALL and known["faithfulness"] >= PASS_FAITHFULNESS and floors_ok
    return round(overall, 4), ("pass" if ok else "refine")


def _floor_breaches(scores: dict[str, float]) -> list[str]:
    return [
        f"{dim}={float(scores.get(dim, 0.0)):.2f} < {floor}"
        for dim, floor in DIM_FLOORS.items()
        if float(scores.get(dim, 0.0)) < floor
    ]


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

    # 命中硬门限时把 breach 原文缀在 critique 上：模型给的 critique 未必点出是哪一项掉线。
    breaches = _floor_breaches(scores)
    if breaches:
        critique = f"{critique}（硬门限：{'、'.join(breaches)}）"

    overall, verdict = score_of(scores)
    rnd = state.get("refine_round", 0)  # 已执行的 refine 次数

    # 预算判断放在**节点内**，与 replanner 一致。
    # 放在路由函数里会差一轮：路由看到的 refine_round 已经 +1，再和上限比较，
    # 于是 "max 2 次" 实际只执行 1 次 —— 第 2 次判定 refine 时就被判"轮次用尽"。
    budget_exhausted = verdict == "refine" and rnd >= settings.REFLECTION_MAX_REFINE
    if budget_exhausted:
        verdict = "pass"
        critique = f"{critique}（refine 预算已用尽 {rnd}/{settings.REFLECTION_MAX_REFINE}，按当前稿收尾）"

    logger.info("评审 overall={:.2f} verdict={} round={} breaches={}", overall, verdict, rnd, breaches)

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
                "floor_breaches": breaches,
            }
        ],
    }

    # 要 refine：指针拨回第 0 步、已执行轮次 +1，并把计划里已完成的步骤重新打开。
    # 最后这一步不能省：executor 按"依赖已满足且状态是 pending"挑活，只把指针拨回 0
    # 而步骤还留着 done，它会认为无可执行步骤而直接跳回评审 —— refine 循环变成空转。
    if verdict == "refine":
        patch["refine_round"] = rnd + 1
        patch["current_step"] = 0
        patch["plan"] = [{**s, "status": "pending"} for s in (state.get("plan") or [])]

    return patch


def route_after_reflect(state: AgentState) -> str:
    """refine 条件边：verdict=refine 就回 executor 重写。预算已由 reflector 判定。"""
    refl = state.get("reflection") or {}
    return "refine" if refl.get("verdict") == "refine" else "synthesizer"


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
    "DIM_FLOORS",
    "PASS_FAITHFULNESS",
    "PASS_OVERALL",
    "WEIGHTS",
    "ReflectionResult",
    "reflector_node",
    "route_after_reflect",
    "score_of",
]
