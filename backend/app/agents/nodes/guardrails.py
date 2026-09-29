"""Guardrails 节点 —— 四层防护的落地。

热路径只做正则与集合运算，**不调 LLM**（除了生成侧的越界引用判定，
那也先用白名单算）。四层各自的检查函数独立可测。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.state import AgentState
from app.core.config import settings
from app.core.logging import logger
from app.rag.source_tracing import extract_markers

# ---- 输入侧：注入模式（只匹配典型句式，不做语义判断）----
INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore\s+(all\s+)?(previous|above)\s+instructions?",
        r"忽略(以上|上述|之前)的?(所有)?(指令|要求|提示)",
        r"you\s+are\s+now\s+(a|an)\s+",
        r"你现在(是|扮演)",
        r"(打印|输出|重复)(你的)?\s*(system\s*prompt|系统提示|初始指令)",
        r"reveal\s+your\s+(system\s+)?prompt",
    )
]

# 输入侧剥离：命中后把该句切掉，而不是拒绝整条消息
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+")

HTML_DANGER = re.compile(r"<\s*(script|iframe|object|embed|link|style)\b", re.I)


def check_input(state: AgentState) -> tuple[list[str], dict[str, Any]]:
    """输入侧。返回 (flags, patch)。"""
    flags: list[str] = []
    patch: dict[str, Any] = {}
    query = state.get("query", "") or ""

    if not query.strip():
        return ["empty_query"], {}

    if any(p.search(query) for p in INJECTION_PATTERNS):
        flags.append("injection_suspected")
        kept = [s for s in _SENT_SPLIT.split(query) if s and not any(p.search(s) for p in INJECTION_PATTERNS)]
        cleaned = " ".join(s.strip() for s in kept).strip()
        # 全被剥掉说明整条都是注入 → 留原文交给下游拒绝，避免变成空 query
        patch["query"] = cleaned or query

    if len(query) > settings.GUARDRAIL_MAX_INPUT_CHARS:
        flags.append("truncated")
        patch["query"] = query[: settings.GUARDRAIL_MAX_INPUT_CHARS]

    return flags, patch


def check_retrieval(state: AgentState) -> list[str]:
    """检索侧。"""
    flags: list[str] = []
    docs = state.get("retrieved") or []
    if not docs:
        return ["no_context"]

    if all(float(d.get("score", 0.0)) < settings.RETRIEVAL_MIN_SCORE for d in docs):
        flags.append("low_quality_context")

    # 单篇独占：比较类问题只命中一篇，说明检索偏了
    if state.get("intent") == "cross_paper_reasoning":
        papers = {str(d.get("paper_id")) for d in docs}
        if len(papers) == 1 and len(docs) >= 3:
            flags.append("single_source")

    return flags


def check_generation(state: AgentState, answer: str) -> tuple[list[str], str]:
    """生成侧。返回 (flags, 修订后的 answer)。"""
    flags: list[str] = []
    if not answer:
        return ["empty_answer"], answer

    n_docs = len(state.get("retrieved") or [])
    markers = extract_markers(answer)
    if n_docs and any(m > n_docs for m in markers):
        flags.append("bad_citation_ref")
        answer = re.sub(
            r"[\[【]\s*(\d+)\s*[\]】]",
            lambda m: "" if int(m.group(1)) > n_docs else m.group(0),
            answer,
        )

    if "system prompt" in answer.lower() or "系统提示" in answer:
        flags.append("prompt_leak")
        answer = "我无法提供系统提示的内容。请继续问你的研究问题。"

    return flags, answer


def check_output(state: AgentState, answer: str) -> tuple[list[str], str]:
    """输出侧。"""
    flags: list[str] = []
    ratio = float(state.get("grounding_ratio", 0.0))
    if state.get("retrieved") and ratio < settings.GROUNDING_MIN_RATIO:
        flags.append("low_grounding")

    if HTML_DANGER.search(answer):
        flags.append("html_escaped")
        answer = HTML_DANGER.sub("&lt;", answer)

    if len(answer) > settings.GUARDRAIL_MAX_OUTPUT_CHARS:
        flags.append("output_truncated")
        answer = answer[: settings.GUARDRAIL_MAX_OUTPUT_CHARS] + "\n\n…（输出过长已截断）"

    return flags, answer


def decide(input_flags: list[str], other_flags: list[str]) -> str:
    """汇总动作：能放行就放行，只在注入/越权时阻断。"""
    if "injection_suspected" in input_flags and "empty_query" not in input_flags:
        return "strip"
    if "prompt_leak" in other_flags:
        return "block"
    if "low_grounding" in other_flags or "bad_citation_ref" in other_flags:
        return "refine"
    return "pass"


async def guardrails_node(state: AgentState) -> dict[str, Any]:
    """图上的防护节点：输入 + 检索 + 生成 + 输出一次过完，产出最终 answer。"""
    in_flags, patch = check_input(state)
    ret_flags = check_retrieval(state)

    working: AgentState = {**state, **patch}  # type: ignore[assignment]
    answer = working.get("answer", "")

    gen_flags, answer = check_generation(working, answer)
    out_flags, answer = check_output(working, answer)

    all_flags = [*in_flags, *ret_flags, *gen_flags, *out_flags]
    action = decide(in_flags, [*ret_flags, *gen_flags, *out_flags])
    logger.info("Guardrails action={} flags={}", action, all_flags)

    result: dict[str, Any] = {
        "answer": answer,
        "guardrail_flags": all_flags,
        "done": True,
        "trace": [{"node": "guardrails", "action": action, "flags": all_flags}],
    }
    if "query" in patch:
        result["query"] = patch["query"]
    return result


__all__ = [
    "INJECTION_PATTERNS",
    "check_generation",
    "check_input",
    "check_output",
    "check_retrieval",
    "decide",
    "guardrails_node",
]
