"""Retriever 节点 —— 混合检索 + CRAG 三级降级。

这里只负责"拿到足够好的上下文"，不做生成。降级决策全部交给
`CorrectiveRAG`（依赖注入回调，便于单测）。
"""

from __future__ import annotations

from typing import Any

from app.agents.state import AgentState, RetrievedDoc
from app.core.errors import EmptyRetrievalError
from app.core.logging import logger
from app.rag.crag import CorrectiveRAG
from app.rag.retriever import HybridRetriever, RetrievedChunk


def _to_docs(chunks: list[RetrievedChunk]) -> list[RetrievedDoc]:
    """转成 state 里可序列化的 dict（LangGraph checkpointer 要能 pickle）。

    `bbox` 必须一起带过去：它是 PDF.js 框选命中段落的坐标，只在检索层存着
    就等于前端拿不到精确定位（只剩页码）。
    """
    docs: list[RetrievedDoc] = []
    for c in chunks:
        bbox = c.bbox
        docs.append(
            RetrievedDoc(
                chunk_id=c.id,
                paper_id=c.paper_id,
                section=c.section or "",
                page=c.page or 0,
                bbox=list(bbox) if isinstance(bbox, (list, tuple)) else bbox,
                text=c.content,
                score=c.final_score,
                source=",".join(c.sources) or "dense",
            )
        )
    return docs


def _level_of(verdict: str) -> str:
    return {"relevant": "relevant", "ambiguous": "ambiguous", "irrelevant": "irrelevant"}.get(str(verdict), "ambiguous")


async def retriever_node(state: AgentState) -> dict[str, Any]:
    query = state.get("rewritten_query") or state.get("query", "")
    paper_ids = state.get("target_papers") or None
    retriever = HybridRetriever()

    # 本节点替代计划里 `retrieve_papers` 那一步，所以执行完要把该步标 done 并前移指针
    plan = list(state.get("plan") or [])
    idx = state.get("current_step", 0)

    async def _retrieve(q: str) -> list[RetrievedChunk]:
        return await retriever.retrieve(q, paper_ids=paper_ids)

    async def _rewrite(q: str) -> str:
        from app.rag.query_rewrite import rewrite_query

        return await rewrite_query(q)

    async def _web(q: str) -> list[RetrievedChunk]:
        """Web 兜底：走 MCP 的 web_search，结果包成 RetrievedChunk 并标 source=web。"""
        from app.agents.mcp.registry import call_tool

        raw = await call_tool("web_search", {"query": q, "max_results": 5})
        items = raw.get("results", []) if isinstance(raw, dict) else []
        return [
            RetrievedChunk(
                id=f"web-{i}",
                paper_id="web",
                content=str(it.get("snippet") or it.get("content") or ""),
                section="web",
                score=0.0,
                sources=["web"],
            )
            for i, it in enumerate(items)
            if isinstance(it, dict)
        ]

    crag = CorrectiveRAG(retrieve=_retrieve, rewrite=_rewrite, web_search=_web)
    try:
        result = await crag.run(query)
    except EmptyRetrievalError as exc:
        logger.warning("CRAG 无可用上下文: {}", exc)
        if 0 <= idx < len(plan):
            plan[idx] = {**plan[idx], "status": "failed", "result": str(exc)}
        return {
            "plan": plan,
            "current_step": idx + 1,
            "retrieved": [],
            "crag_level": "irrelevant",
            "reranked": [],
            "guardrail_flags": ["no_context"],
            "trace": [{"node": "retriever", "level": "irrelevant", "reason": str(exc)}],
        }

    docs = _to_docs(list(result.chunks))
    level = _level_of(result.verdict)
    flags = ["web_fallback"] if result.used_web_fallback else []
    final_query = result.rewrites[-1] if result.rewrites else query
    logger.info("检索完成 level={} docs={} rewrites={}", level, len(docs), result.rounds)

    if 0 <= idx < len(plan):
        plan[idx] = {**plan[idx], "status": "done", "result": f"检索到 {len(docs)} 个片段（{level}）"}

    return {
        "plan": plan,
        "current_step": idx + 1,
        "retrieved": docs,
        "reranked": docs,
        "crag_level": level,
        "rewrite_round": result.rounds,
        "rewritten_query": final_query,
        "guardrail_flags": flags,
        "trace": [
            {
                "node": "retriever",
                "level": level,
                "n_docs": len(docs),
                "rewrites": result.rounds,
                "rationale": result.rationale,
            }
        ],
    }


def route_after_retrieve(state: AgentState) -> str:
    """还有步骤就交给 executor；没有则直接收尾。

    注意这里**不经过 reflector** —— 反思评审需要一份草稿，而 retriever 只产上下文。
    对"只检索一步"的计划，跳过评审是刻意的：没有草稿的评审是空转。
    """
    return "executor" if state.get("current_step", 0) < len(state.get("plan") or []) else "synthesizer"


__all__ = ["retriever_node", "route_after_retrieve"]
