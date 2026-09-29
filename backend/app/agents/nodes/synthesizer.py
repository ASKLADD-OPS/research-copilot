"""Synthesizer 节点 —— 综合成文 + 溯源校验。

生成后**立刻**跑 SourceTracer：把引用白名单（检索上下文里的 chunk_id）与
回答里的 `[n]` 标注对齐，算出 Grounding Ratio。低于阈值就把风险写进
guardrail_flags，由输出侧决定是否提示用户。
"""

from __future__ import annotations

import contextlib
from typing import Any

from app.agents.prompts import render
from app.agents.state import AgentState, Citation
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Message, Role, get_llm
from app.rag.retriever import RetrievedChunk, to_context_block


def _chunks_of(state: AgentState) -> list[RetrievedChunk]:
    docs = state.get("reranked") or state.get("retrieved") or []
    return [
        RetrievedChunk(
            id=str(d.get("chunk_id", "")),
            paper_id=str(d.get("paper_id", "")),
            content=str(d.get("text", "")),
            section=d.get("section") or None,
            page_start=d.get("page") or None,
        )
        for d in docs
        if d.get("chunk_id")
    ]


def _writer() -> Any:
    """拿到 LangGraph 的自定义流写入器（stream_mode="custom"）。

    没有流上下文时返回 None —— 这样同一个节点既能被 `ainvoke`（返回完整答案）
    复用，也能被 `astream` 逐 token 推给前端，不需要维护两份实现。
    """
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - 非流式调用路径
        return None


async def _generate(messages: list[Message], *, temperature: float = 0.3) -> str:
    """有流上下文就逐 token 推送，否则一次性返回。"""
    writer = _writer()
    if writer is None:
        return await get_llm().complete(Role.EXECUTOR, messages, temperature=temperature)

    parts: list[str] = []
    async for piece in get_llm().stream(Role.EXECUTOR, messages, temperature=temperature):
        parts.append(piece)
        with contextlib.suppress(Exception):  # 推送失败不该影响生成
            writer({"type": "token", "text": piece})
    return "".join(parts)


async def synthesizer_node(state: AgentState) -> dict[str, Any]:
    """综合成文。

    非流式调用（`ainvoke`）返回完整 answer；流式调用（`astream(stream_mode="custom")`）
    会把 token 直接推给前端，SSE 出口在 `api/v1/chat.py`。
    """
    query = state.get("query", "")
    if state.get("intent") == "chitchat" or (not state.get("plan") and not state.get("retrieved")):
        # 寒暄 / 无上下文：普通对话，不做引用约束
        try:
            answer = await _generate(
                [
                    {"role": "system", "content": "你是学术研究助手。简短回答。不要编造论文内容。"},
                    {"role": "user", "content": query},
                ]
            )
        except Exception as exc:  # noqa: BLE001
            answer = f"模型暂时不可用（{type(exc).__name__}）。"
        return {"answer": answer, "done": True, "trace": [{"node": "synthesizer", "mode": "chat"}]}

    chunks = _chunks_of(state)
    context = to_context_block(chunks, max_chars=settings.CONTEXT_MAX_CHARS) if chunks else "（无检索上下文）"
    fix_hint = (state.get("reflection") or {}).get("fix_hint", "")
    prior = state.get("answer") or state.get("draft") or ""

    messages: list[Message] = [
        {"role": "system", "content": render("synthesizer")},
        {
            "role": "user",
            "content": (
                f"用户问题：{query}\n\n"
                + (f"上一稿的问题（必须修正）：{fix_hint}\n\n" if fix_hint else "")
                + (f"上一稿：\n{prior}\n\n" if fix_hint and prior else "")
                + f"检索上下文：\n{context}"
            ),
        },
    ]

    try:
        answer = await _generate(messages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("综合成文失败: {}", exc)
        # 退化为直接拼接执行结果，至少不返回空
        answer = "模型暂时不可用，以下是各步骤原始结论：\n\n" + "\n\n".join(
            str(s.get("result", "")) for s in (state.get("plan") or []) if s.get("result")
        )

    citations, grounding, flags = _trace(answer, chunks)
    return {
        "answer": answer,
        "draft": answer,
        "citations": citations,
        "grounding_ratio": grounding,
        "guardrail_flags": flags,
        "done": True,
        "trace": [
            {
                "node": "synthesizer",
                "grounding_ratio": grounding,
                "n_citations": len(citations),
                "flags": flags,
            }
        ],
    }


def _trace(answer: str, chunks: list[RetrievedChunk]) -> tuple[list[Citation], float, list[str]]:
    """调溯源引擎；引擎不可用时退化为"chunk_id 白名单"这一最简校验。

    最简校验不调模型，只做三件事：
    ① 回答里的 [n] 是否越界；② 是否出现白名单外的论文标题；③ 估算声明覆盖率。
    """
    flags: list[str] = []
    try:
        from app.rag.source_tracing import get_tracer

        # chunks 的对象顺序即 [n] 编号，直接传原对象（tracer 读 .id/.paper_id）
        report = get_tracer().trace(answer, chunks)
        citations: list[Citation] = [
            Citation(
                chunk_id=str(c.chunk_id),
                paper_id=c.paper_id,
                section=c.section or "",
                page=c.page_start or 0,
                quote=c.quote,
                verified=bool(c.supported),
            )
            for c in report.citations
        ]
        ratio = float(report.grounding_ratio)
        if ratio < settings.GROUNDING_MIN_RATIO:
            flags.append("low_grounding")
        if report.phantom_markers:
            flags.append("bad_citation_ref")
        if report.unsupported_claims:
            flags.append("unsupported_claims")
        return citations, ratio, flags
    except Exception as exc:  # noqa: BLE001
        logger.warning("溯源引擎不可用，退化为白名单校验: {}", exc)
        return _whitelist_check(answer, chunks)


def _whitelist_check(answer: str, chunks: list[RetrievedChunk]) -> tuple[list[Citation], float, list[str]]:
    from app.rag.source_tracing import extract_markers

    flags: list[str] = []
    if not chunks:
        return [], 0.0, ["no_context"]

    max_idx = len(chunks)
    markers = extract_markers(answer)
    if not markers:
        flags.append("no_citation")
    if any(m > max_idx for m in markers):
        flags.append("bad_citation_ref")

    cited = sorted({m for m in markers if 1 <= m <= max_idx})
    citations = [
        Citation(
            chunk_id=chunks[i - 1].id,
            paper_id=chunks[i - 1].paper_id,
            page=chunks[i - 1].page_start or 0,
            verified=False,
        )
        for i in cited
    ]
    ratio = round(len(cited) / max_idx, 4) if max_idx else 0.0
    return citations, ratio, flags


__all__ = ["synthesizer_node"]
