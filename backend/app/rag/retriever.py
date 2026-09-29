"""混合检索器：Dense + Sparse 两路 → Milvus 原生 RRF → （可选）第三路融合 → CrossEncoder 重排。

调用链
------
query → bge-m3 编码（dense 1024 维 + learned sparse）
      → Milvus `hybrid_search`（`AnnSearchRequest` × 2 + `RRFRanker(k=60)`）
      → 若调用方提供了额外召回路（引文图谱 / Web 兜底），用 `reciprocal_rank_fusion` 再融合一次
      → CrossEncoder 重排 → top_k

为什么两段式融合而不是一次算完
------------------------------
Milvus 的 `RRFRanker` 只能融合**它自己集合内的字段**。图谱召回（NetworkX 内存图）
和 Web 兜底都不在 Milvus 里，只能在拿到 Milvus 结果后再做一次 RRF。
两次 RRF 的结果不可直接比较分数，但排序语义是一致的 —— 我们只消费排序。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.db.milvus import VectorHit, ahybrid_search
from app.embeddings.bge_m3 import get_embedder
from app.rag.fusion import dedupe_by_id, reciprocal_rank_fusion


@dataclass(slots=True)
class RetrievedChunk:
    """检索链路中流动的统一结构。

    `id` 是 RRF 的融合键，必须稳定 —— 用 chunk 的 UUID（与 Milvus 主键一致）。
    """

    id: str
    paper_id: str
    content: str
    chunk_index: int = 0
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    score: float = 0.0
    rerank_score: float | None = None
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_vector_hit(cls, hit: VectorHit) -> RetrievedChunk:
        return cls(
            id=hit.id,
            paper_id=hit.paper_id,
            content=hit.content,
            chunk_index=hit.chunk_index,
            section=hit.section,
            page_start=hit.page_start,
            page_end=hit.page_end,
            score=hit.score,
            sources=[hit.source],
        )

    @property
    def final_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.score

    def to_citation(self) -> dict[str, Any]:
        return {
            "chunk_id": self.id,
            "paper_id": self.paper_id,
            "section": self.section,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "sources": self.sources,
        }


def build_paper_filter(paper_ids: Sequence[str] | None) -> str | None:
    """构造 Milvus 过滤表达式。"""
    if not paper_ids:
        return None
    quoted = ", ".join(f'"{p}"' for p in paper_ids)
    return f"paper_id in [{quoted}]"


class HybridRetriever:
    def __init__(self) -> None:
        self._embedder = get_embedder()

    async def retrieve(
        self,
        query: str,
        *,
        paper_ids: Sequence[str] | None = None,
        top_k: int | None = None,
        recall_k: int | None = None,
        extra_paths: Sequence[Sequence[RetrievedChunk]] | None = None,
        extra_weights: Sequence[float] | None = None,
        rerank: bool | None = None,
    ) -> list[RetrievedChunk]:
        top_k = top_k or settings.RETRIEVAL_TOP_K
        recall_k = recall_k or settings.RETRIEVAL_RECALL_K
        expr = build_paper_filter(paper_ids)

        emb = self._embedder.encode_query(query)
        if not emb.dense:
            logger.error("嵌入失败，无法检索")
            return []

        hits = await ahybrid_search(
            emb.dense[0],
            emb.sparse[0] if emb.sparse else {},
            limit=max(top_k, recall_k),
            recall_k=recall_k,
            expr=expr,
        )
        primary = [RetrievedChunk.from_vector_hit(h) for h in hits]

        ranked_paths: list[list[RetrievedChunk]] = [primary]
        weights: list[float] = [1.0]
        for path in extra_paths or []:
            if path:
                ranked_paths.append(list(path))
        if extra_weights:
            weights = [1.0, *extra_weights]

        if len(ranked_paths) > 1:
            fused = reciprocal_rank_fusion(ranked_paths, k=settings.RRF_K, weights=weights)
            merged: list[RetrievedChunk] = []
            for chunk, score in fused:
                merged.append(
                    RetrievedChunk(
                        id=chunk.id,
                        paper_id=chunk.paper_id,
                        content=chunk.content,
                        chunk_index=chunk.chunk_index,
                        section=chunk.section,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        score=score,
                        sources=chunk.sources,
                    )
                )
            merged = dedupe_by_id(merged)
        else:
            merged = dedupe_by_id(primary)

        if rerank is None:
            rerank = settings.RERANKER_ENABLED
        if rerank and merged:
            from app.rag.reranker import get_reranker

            merged = await get_reranker().arerank(query, merged)

        logger.debug("检索完成 query={!r} 候选={} 返回={}", query[:40], len(merged), min(top_k, len(merged)))
        return merged[:top_k]


def to_context_block(chunks: Sequence[RetrievedChunk], *, max_chars: int = 12000) -> str:
    """把检索结果拼成给 LLM 的上下文块。

    **带编号**：编号就是引用白名单 —— 生成侧只允许引用这里出现过的 `[n]`，
    溯源引擎据此校验，这是"禁止幻觉引用"的第一道闸门。
    """
    lines: list[str] = []
    used = 0
    for i, c in enumerate(chunks, start=1):
        head = f"[{i}] paper={c.paper_id[:8]} chunk={c.chunk_index}"
        if c.section:
            head += f" section={c.section}"
        if c.page_start:
            head += f" p.{c.page_start}" + (f"-{c.page_end}" if c.page_end and c.page_end != c.page_start else "")
        block = f"{head}\n{c.content.strip()}\n"
        # `used > 0` 这个条件不能省：没有它就退化成"第一块超限时上下文为空"，
        # 生成侧拿不到任何证据，只会照着问题编 —— 宁可超限，不可空手。
        if used and used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n---\n".join(lines)
