"""Milvus 集合定义与混合检索访问层。

设计要点
--------
1. **双向量字段**：`dense`（bge-m3 Dense，dim=1024）+ `sparse`（bge-m3 Sparse 稀疏向量）。
   稀疏字段用 `SPARSE_FLOAT_VECTOR` + `SPARSE_INVERTED_INDEX`，metric 用 IP。
2. **RRF 融合交给 Milvus 原生 `RRFRanker`**：两路 `AnnSearchRequest` →
   `hybrid_search(reqs=[...], ranker=RRFRanker(k))`。RRF 基于**排名**而非原始分数，
   不需要手工调权重，也回避了 cosine 分与 IP 分不可比的问题。
3. **content 同时存在 Milvus**：召回后不必为每个 chunk 回 PostgreSQL 取正文，
   少一轮 N+1；同时该字段可直接挂 Milvus 的 BM25 Function 作为备用稀疏来源。
   PostgreSQL 仍是唯一真源，Milvus 里的 content 是检索副本。
4. 用同步 `MilvusClient` + `asyncio.to_thread`：pymilvus 的同步客户端是最稳定、
   文档最全的接口，async 客户端版本间命名漂移较多。包一层即可在 FastAPI 里非阻塞使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pymilvus import AnnSearchRequest, CollectionSchema, DataType, FieldSchema, MilvusClient, RRFRanker

from app.core.config import settings
from app.core.errors import RetrievalError
from app.core.logging import logger

DENSE_FIELD = "dense"
SPARSE_FIELD = "sparse"
OUTPUT_FIELDS = ["id", "paper_id", "chunk_index", "section", "page_start", "page_end", "content"]


@dataclass(slots=True)
class VectorHit:
    """一路召回的结果项。distance 的语义随 metric 变化，跨路比较无意义 —— 交给 RRF。"""

    id: str
    paper_id: str
    chunk_index: int
    section: str | None
    page_start: int | None
    page_end: int | None
    content: str
    score: float
    source: str = "vector"  # vector | graph | web

    @classmethod
    def from_entity(cls, entity: dict[str, Any], score: float, source: str = "vector") -> VectorHit:
        return cls(
            id=str(entity.get("id", "")),
            paper_id=str(entity.get("paper_id", "")),
            chunk_index=int(entity.get("chunk_index") or 0),
            section=entity.get("section"),
            page_start=entity.get("page_start"),
            page_end=entity.get("page_end"),
            content=entity.get("content") or "",
            score=float(score),
            source=source,
        )


def build_schema(dim: int | None = None) -> CollectionSchema:
    dim = dim or settings.MILVUS_DENSE_DIM
    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
        FieldSchema(name="paper_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="chunk_index", dtype=DataType.INT64),
        FieldSchema(name="section", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="page_start", dtype=DataType.INT64),
        FieldSchema(name="page_end", dtype=DataType.INT64),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name=DENSE_FIELD, dtype=DataType.FLOAT_VECTOR, dim=dim),
        FieldSchema(name=SPARSE_FIELD, dtype=DataType.SPARSE_FLOAT_VECTOR),
    ]
    return CollectionSchema(fields=fields, description="论文 chunk 的 dense + sparse 双向量索引")


def build_index_params() -> list[dict[str, Any]]:
    return [
        {
            "field_name": DENSE_FIELD,
            "index_type": settings.MILVUS_INDEX_TYPE,  # HNSW
            "metric_type": settings.MILVUS_METRIC_TYPE,  # COSINE
            "params": {"M": 16, "efConstruction": 200},
        },
        {
            "field_name": SPARSE_FIELD,
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "IP",
            "params": {"drop_ratio_build": 0.2},
        },
    ]


def sparse_to_dict(sparse: Any) -> dict[int, float]:
    """把 bge-m3 的稀疏输出规范化成 {token_id: weight}。

    兼容三种形态：dict、{indices, values} 的类 scipy 对象、list[tuple]。
    """
    if isinstance(sparse, dict):
        return {int(k): float(v) for k, v in sparse.items()}
    if hasattr(sparse, "indices") and hasattr(sparse, "values"):
        return {int(i): float(v) for i, v in zip(sparse.indices, sparse.values, strict=False)}
    if isinstance(sparse, (list, tuple)):
        return {int(i): float(v) for i, v in sparse}
    raise ValueError(f"无法识别的稀疏向量格式: {type(sparse)!r}")


class MilvusStore:
    def __init__(self, uri: str | None = None, collection: str | None = None) -> None:
        self.collection = collection or settings.MILVUS_COLLECTION
        self._uri = uri or f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
        self._client: MilvusClient | None = None

    # ------------------------------------------------------------ 连接
    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            self._client = MilvusClient(uri=self._uri)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------ 建表
    def ensure_collection(self, *, recreate: bool = False) -> None:
        """幂等建表：已存在则直接返回（除非 recreate=True）。"""
        client = self.client
        if client.has_collection(self.collection):
            if not recreate:
                return
            logger.warning("重建 Milvus 集合 {}（原有向量将被删除）", self.collection)
            client.drop_collection(self.collection)
        client.create_collection(
            collection_name=self.collection,
            schema=build_schema(),
            index_params=build_index_params(),
        )
        logger.info("已创建 Milvus 集合 {}（dense dim={} + sparse）", self.collection, settings.MILVUS_DENSE_DIM)

    # ------------------------------------------------------------ 写入
    def upsert(self, records: list[dict[str, Any]]) -> int:
        """按主键 upsert。record 需含 id/paper_id/chunk_index/content/dense/sparse。"""
        if not records:
            return 0
        payload = []
        for r in records:
            payload.append(
                {
                    "id": str(r["id"]),
                    "paper_id": str(r["paper_id"]),
                    "chunk_index": int(r.get("chunk_index", 0)),
                    "section": (r.get("section") or "")[:512],
                    "page_start": int(r.get("page_start") or 0),
                    "page_end": int(r.get("page_end") or 0),
                    "content": (r.get("content") or "")[:65535],
                    DENSE_FIELD: list(r["dense"]),
                    SPARSE_FIELD: sparse_to_dict(r["sparse"]),
                }
            )
        res = self.client.upsert(collection_name=self.collection, data=payload)
        return len(res.get("upsert_count", payload)) if isinstance(res, dict) else len(payload)

    def delete_by_paper(self, paper_id: str) -> None:
        self.client.delete(collection_name=self.collection, filter=f'paper_id == "{paper_id}"')

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        quoted = ", ".join(f'"{i}"' for i in ids)
        self.client.delete(collection_name=self.collection, filter=f"id in [{quoted}]")

    # ------------------------------------------------------------ 检索
    def hybrid_search(
        self,
        dense: list[float],
        sparse: Any,
        *,
        limit: int | None = None,
        recall_k: int | None = None,
        expr: str | None = None,
        rrf_k: int | None = None,
    ) -> list[VectorHit]:
        """Dense + Sparse 两路召回，Milvus 原生 RRF 融合。"""
        limit = limit or settings.RETRIEVAL_TOP_K
        recall_k = recall_k or settings.RETRIEVAL_RECALL_K
        rrf_k = rrf_k or settings.RRF_K

        requests = [
            AnnSearchRequest(
                data=[list(dense)],
                anns_field=DENSE_FIELD,
                param={"metric_type": settings.MILVUS_METRIC_TYPE, "params": {"ef": max(recall_k, 64)}},
                limit=recall_k,
                expr=expr,
            ),
            AnnSearchRequest(
                data=[sparse_to_dict(sparse)],
                anns_field=SPARSE_FIELD,
                param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
                limit=recall_k,
                expr=expr,
            ),
        ]
        try:
            raw = self.client.hybrid_search(
                collection_name=self.collection,
                reqs=requests,
                ranker=RRFRanker(rrf_k),
                limit=limit,
                output_fields=OUTPUT_FIELDS,
            )
        except Exception as exc:  # noqa: BLE001 —— 统一转成业务异常，保留原始信息
            raise RetrievalError(f"Milvus 混合检索失败: {exc}") from exc

        hits: list[VectorHit] = []
        for group in raw or []:
            for h in group:
                entity = dict(h.get("entity") or {})
                hits.append(VectorHit.from_entity(entity, h.get("distance", 0.0)))
        return hits

    def dense_search(self, dense: list[float], *, limit: int = 10, expr: str | None = None) -> list[VectorHit]:
        """仅稠密检索。用于自研 RRF 的对照实现与降级路径。"""
        raw = self.client.search(
            collection_name=self.collection,
            data=[list(dense)],
            anns_field=DENSE_FIELD,
            search_params={"metric_type": settings.MILVUS_METRIC_TYPE, "params": {"ef": 64}},
            limit=limit,
            output_fields=OUTPUT_FIELDS,
            filter=expr or "",
        )
        return [
            VectorHit.from_entity(dict(h.get("entity") or {}), h.get("distance", 0.0))
            for group in (raw or [])
            for h in group
        ]

    # ------------------------------------------------------------ 健康检查
    def health(self) -> tuple[bool, str]:
        try:
            client = self.client
            if not client.has_collection(self.collection):
                return True, f"已连接，集合 {self.collection} 尚未创建"
            stats = client.get_collection_stats(self.collection)
            return True, f"集合 {self.collection} 行数={stats.get('row_count', '?')}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@lru_cache
def get_store() -> MilvusStore:
    return MilvusStore()


# ---------------------------------------------------------------- async 包装
# pymilvus 同步调用放到线程池，避免阻塞事件循环。FastAPI 与 Agent 节点统一用这一层。


async def aensure_collection(*, recreate: bool = False) -> None:
    await asyncio.to_thread(get_store().ensure_collection, recreate=recreate)


async def aupsert(records: list[dict[str, Any]]) -> int:
    return await asyncio.to_thread(get_store().upsert, records)


async def ahybrid_search(dense: list[float], sparse: Any, **kwargs: Any) -> list[VectorHit]:
    return await asyncio.to_thread(lambda: get_store().hybrid_search(dense, sparse, **kwargs))


async def adelete_by_paper(paper_id: str) -> None:
    await asyncio.to_thread(get_store().delete_by_paper, paper_id)


async def ahealth() -> tuple[bool, str]:
    return await asyncio.to_thread(get_store().health)
