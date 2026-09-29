"""问答：走完整 Agent 图（意图 → 规划 → 检索/工具 → 评审 → 合成 → 防护）。

另有两条只读辅助接口：
- `/qa/retrieve`：只跑检索链路，用于调参与排障（不烧生成 token）；
- `/qa/trace`：对给定文本做溯源校验，用于前端"引用可信度"面板。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from loguru import logger
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.errors import EmptyRetrievalError
from app.llm.client import get_llm
from app.models import Paper, PaperChunk
from app.schemas import (
    ApiResponse,
    AskRequest,
    AskResult,
    CitationOut,
    RetrievalDebug,
    RetrievedChunkOut,
    TraceRequest,
    TraceResult,
)

router = APIRouter(prefix="/qa", tags=["问答"])


def _chunk_out(chunk: Any) -> RetrievedChunkOut:
    return RetrievedChunkOut(
        chunk_id=getattr(chunk, "id", ""),
        paper_id=getattr(chunk, "paper_id", ""),
        section=getattr(chunk, "section", None),
        page_start=getattr(chunk, "page_start", None),
        page_end=getattr(chunk, "page_end", None),
        score=round(float(getattr(chunk, "score", 0.0)), 6),
        rerank_score=getattr(chunk, "rerank_score", None),
        sources=list(getattr(chunk, "sources", []) or []),
        preview=(getattr(chunk, "content", "") or "")[:300],
    )


async def _attach_titles(session: Any, rows: list[RetrievedChunkOut]) -> None:
    """补论文标题。一次查询搞定，别在循环里查库。"""
    ids = {r.paper_id for r in rows if r.paper_id}
    if not ids:
        return
    titles = dict(
        (await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(ids)))).all()  # type: ignore[arg-type]
    )
    for row in rows:
        row.title = str(titles.get(row.paper_id, "") or "")


@router.post("/ask", response_model=ApiResponse[AskResult], summary="提问（完整 Agent 链路）")
async def ask(payload: AskRequest, session: SessionDep) -> ApiResponse[AskResult]:
    from app.agents.graph import get_graph

    started = time.perf_counter()
    initial: dict[str, Any] = {
        "query": payload.query,
        "target_papers": payload.paper_ids,
        "top_k": payload.top_k,
    }
    if payload.intent:
        initial["intent"] = payload.intent

    graph = get_graph()
    state: dict[str, Any] = await graph.ainvoke(initial, config={"configurable": {"thread_id": "rest"}})

    retrieved = [_chunk_out(c) for c in (state.get("reranked") or state.get("retrieved") or [])]
    await _attach_titles(session, retrieved)

    grounding = float(state.get("grounding_ratio") or 0.0)
    citations = [
        CitationOut(
            chunk_id=str(c.get("chunk_id", "")),
            paper_id=str(c.get("paper_id", "")),
            title=str(c.get("title", "")),
            page_start=c.get("page"),
            quote=str(c.get("quote", ""))[:280],
            verified=bool(c.get("verified", False)),
        )
        for c in (state.get("citations") or [])
    ]
    for cite in citations:
        if not cite.title:
            cite.title = next((r.title for r in retrieved if r.chunk_id == cite.chunk_id), "")

    usage = get_llm().usage
    latency = int((time.perf_counter() - started) * 1000)
    logger.info("问答完成 intent={} grounding={:.2f} latency={}ms", state.get("intent"), grounding, latency)

    return ApiResponse.ok(
        AskResult(
            answer=state.get("answer") or "",
            intent=str(state.get("intent") or ""),
            intent_confidence=float(state.get("confidence") or 0.0),
            citations=citations,
            grounding_ratio=grounding,
            passed_grounding=state.get("guardrail_passed", grounding >= 0.8),
            unsupported_claims=state.get("unsupported_claims") or [],
            guardrail_flags=list(state.get("guardrail_flags") or []),
            plan=list(state.get("plan") or []),
            reflections=[state["reflection"]] if state.get("reflection") else [],
            retrieved=retrieved,
            debug=RetrievalDebug(
                crag_level=state.get("crag_level"),
                rewrite_round=int(state.get("rewrite_round") or 0),
                rewritten_query=str(state.get("rewritten_query") or ""),
                rerank_applied=bool(state.get("reranked")),
                top_k=payload.top_k or 0,
                trace=list(state.get("trace") or []),
            )
            if payload.debug
            else None,
            usage={
                "calls": usage.calls,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total": usage.total_tokens,
            },
            latency_ms=latency,
        )
    )


@router.post("/retrieve", response_model=ApiResponse[list[RetrievedChunkOut]], summary="只检索（调参用）")
async def retrieve(payload: AskRequest, session: SessionDep) -> ApiResponse[list[RetrievedChunkOut]]:
    from app.rag.retriever import HybridRetriever

    chunks = await HybridRetriever().retrieve(
        payload.query,
        paper_ids=payload.paper_ids or None,
        top_k=payload.top_k,
    )
    if not chunks:
        raise EmptyRetrievalError("没有检索到任何片段：库可能是空的，或问题与已入库论文无关")

    rows = [_chunk_out(c) for c in chunks]
    await _attach_titles(session, rows)
    return ApiResponse.ok(rows)


@router.post("/trace", response_model=ApiResponse[TraceResult], summary="溯源校验（不生成）")
async def trace(payload: TraceRequest, session: SessionDep) -> ApiResponse[TraceResult]:
    from app.rag.source_tracing import get_tracer

    stmt = select(PaperChunk)
    # 没给白名单就取前几块，至少能验出"纯幻觉引用"
    stmt = stmt.where(PaperChunk.id.in_(payload.chunk_ids)) if payload.chunk_ids else stmt.limit(8)
    chunks = (await session.execute(stmt)).scalars().all()

    report = get_tracer().trace(payload.answer, list(chunks))
    return ApiResponse.ok(
        TraceResult(
            grounding_ratio=report.grounding_ratio,
            sentences_total=report.sentences_total,
            sentences_supported=report.sentences_supported,
            passed=report.passed,
            phantom_markers=report.phantom_markers,
            unsupported_claims=report.unsupported_claims,
            uncited_claims=report.uncited_claims,
            number_mismatches=report.number_mismatches,
            citations=[
                CitationOut(
                    marker=c.marker,
                    chunk_id=c.chunk_id,
                    paper_id=c.paper_id,
                    section=c.section,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    quote=c.quote,
                    nli_score=round(c.nli_score, 4),
                    verified=c.supported,
                )
                for c in report.citations
            ],
        )
    )
