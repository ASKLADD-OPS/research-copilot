"""混合检索器：Dense + Sparse 两路 → Milvus 原生 RRF → （可选）第三路融合 → CrossEncoder 重排。

调用链
------
query → bge-m3 编码（dense 1024 维 + learned sparse）
      → Milvus `paper_chunks` hybrid_search（`AnnSearchRequest` × 2 + `RRFRanker(k=60)`）
      → **回 PostgreSQL 补正文**（见下）
      → 若调用方提供了额外召回路（引文图谱 / Web 兜底），用 `reciprocal_rank_fusion` 再融合一次
      → CrossEncoder 重排 → top_k

为什么要回表
------------
`paper_chunks` 集合里只存向量与三个 id，不存正文 —— 正文的真源是 `chunks.content`，
在向量库里再存一份就有两个可写副本，upsert 半途失败时无法判断谁对。
代价就是这一步：一次 `WHERE id IN (...)`，不是 N+1。换来的是"读到的正文
一定和 PG 一致"，以及改 chunk 策略后不必重建向量之外的任何东西。

为什么两段式融合而不是一次算完
------------------------------
Milvus 的 `RRFRanker` 只能融合**它自己集合内**的两路。图谱召回（NetworkX 内存图）
和 Web 兜底都不在 Milvus 里，只能在拿到 Milvus 结果后再做一次 RRF。
两次 RRF 的分数不可直接比较，但排序语义一致 —— 我们只消费排序。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db.milvus import VectorHit, ahybrid_search
from app.db.session import session_scope
from app.embeddings.bge_m3 import get_embedder
from app.models import Chunk
from app.rag.fusion import dedupe_by_id, reciprocal_rank_fusion


@dataclass(slots=True)
class RetrievedChunk:
    """检索链路中流动的统一结构。

    `id` 是 RRF 的融合键，必须稳定 —— 用 `chunks.id`（也就是 Milvus 的主键）。
    """

    id: int
    paper_id: int
    content: str = ""
    section: str | None = None
    page: int | None = None
    bbox: Any | None = None
    score: float = 0.0
    rerank_score: float | None = None
    sources: list[str] = field(default_factory=list)

    @classmethod
    def from_vector_hit(cls, hit: VectorHit) -> RetrievedChunk:
        """只听召回结果，正文留空 —— 由 `_hydrate` 统一回表填。"""
        return cls(
            id=hit.chunk_id,
            paper_id=hit.paper_id,
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
            "page": self.page,
            "bbox": self.bbox,
            "sources": self.sources,
        }


def build_paper_filter(paper_ids: Sequence[int] | None) -> str | None:
    """构造 Milvus 过滤表达式。id 是 int64，**不要加引号** ——
    加引号在 Milvus 里会被当成字符串字段比较，静默返回空结果。"""
    if not paper_ids:
        return None
    return f"paper_id in [{', '.join(str(int(p)) for p in paper_ids)}]"


async def hydrate_chunks(chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
    """回 PostgreSQL 补正文与定位信息，保持入参顺序。"""
    ids = [c.id for c in chunks if c.id]
    if not ids:
        return list(chunks)
    async with session_scope() as session:
        rows = (await session.execute(select(Chunk).where(Chunk.id.in_(ids)))).scalars().all()
    by_id = {int(r.id): r for r in rows}

    out: list[RetrievedChunk] = []
    for chunk in chunks:
        row = by_id.get(chunk.id)
        if row is None:
            # 向量在、正文不在 = PG 被清过而 Milvus 没同步。跳过而不是塞空串：
            # 一个正文为空的 chunk 会让生成侧把 [n] 引到空气上。
            logger.warning("Milvus 命中 chunk_id={} 但在 PG 中不存在，已跳过（索引不一致？）", chunk.id)
            continue
        chunk.content = row.content
        chunk.section = row.section
        chunk.page = row.page
        chunk.bbox = row.bbox
        out.append(chunk)
    return out


class HybridRetriever:
    def __init__(self) -> None:
        self._embedder = get_embedder()

    async def retrieve(
        self,
        query: str,
        *,
        paper_ids: Sequence[int] | None = None,
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
            logger.error("嵌入失败，无法检索（embedding 后端可能未就绪）")
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
            merged = dedupe_by_id(
                [
                    RetrievedChunk(
                        id=chunk.id,
                        paper_id=chunk.paper_id,
                        content=chunk.content,
                        section=chunk.section,
                        page=chunk.page,
                        bbox=chunk.bbox,
                        score=score,
                        sources=chunk.sources,
                    )
                    for chunk, score in fused
                ]
            )
        else:
            merged = dedupe_by_id(primary)

        merged = await hydrate_chunks(merged)

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
        head = f"[{i}] paper={c.paper_id} chunk={c.id}"
        if c.section:
            head += f" section={c.section}"
        if c.page:
            head += f" p.{c.page}"
        block = f"{head}\n{c.content.strip()}\n"
        # `used > 0` 这个条件不能省：没有它就退化成"第一块超限时上下文为空"，
        # 生成侧拿不到任何证据，只会照着问题编 —— 宁可超限，不可空手。
        if used and used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n---\n".join(lines)


__all__ = ["HybridRetriever", "RetrievedChunk", "build_paper_filter", "hydrate_chunks", "to_context_block"]
