"""Milvus 集合定义与混合检索访问层。

两个集合，职责不重叠
--------------------
- **paper_chunks**：chunk 级。`dense_embedding`（bge-m3 dense，dim=1024）+
  `sparse_embedding`（bge-m3 learned sparse）双路召回，用 Milvus 原生 `RRFRanker`
  融合。这是问答链路的主召回源。
- **paper_summaries**：论文级。只有 `dense_embedding`，用于**语义去重**
  （新论文进来先查一次，近似重复的直接认领已有 paper_id）和论文级检索。

为什么集合里不放 content
------------------------
规格里 `paper_chunks` 只有 5 个字段，没有正文字段。这是对的：正文的真源是
PostgreSQL 的 `chunks.content`，在 Milvus 再存一份就出现了两个可写副本，
一旦某次 upsert 半途失败，两边不一致时**无从判断哪个才是对的**。
代价是召回后要多一次 `WHERE id IN (...)` 回表 —— 一次查询，不是 N+1。

为什么用同步客户端 + to_thread
-----------------------------
pymilvus 的同步 `MilvusClient` 是最稳定、文档最全的接口，async 客户端在版本间
命名漂移较多。包一层 `asyncio.to_thread` 即可在 FastAPI 里非阻塞使用，
比追 async 客户端的 API 变更省心。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pymilvus import (
    AnnSearchRequest,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    RRFRanker,
)
from pymilvus.milvus_client.index import IndexParams

from app.core.config import settings
from app.core.errors import RetrievalError
from app.core.logging import logger

DENSE_FIELD = "dense_embedding"
SPARSE_FIELD = "sparse_embedding"


# ---------------------------------------------------------------- 集合定义
def build_chunks_schema(dim: int | None = None) -> CollectionSchema:
    """paper_chunks：id / paper_id / chunk_id / dense_embedding / sparse_embedding。"""
    dim = dim or settings.MILVUS_DENSE_DIM
    return CollectionSchema(
        fields=[
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True),
            FieldSchema(name="paper_id", dtype=DataType.INT64),
            FieldSchema(name="chunk_id", dtype=DataType.INT64),
            FieldSchema(name=DENSE_FIELD, dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name=SPARSE_FIELD, dtype=DataType.SPARSE_FLOAT_VECTOR),
        ],
        description="论文 chunk 的 dense(1024) + learned sparse 双向量索引",
    )


def build_chunks_index_params() -> IndexParams:
    """HNSW(COSINE) 打稠密路，SPARSE_INVERTED_INDEX(IP) 打稀疏路。

    稀疏路用 IP 而非 COSINE：learned sparse 的每一维都是非负权重，向量天然非负，
    内积与余弦在排序上等价，而倒排索引只支持 IP。

    **必须是 `IndexParams` 而不是 `list[dict]`**：pymilvus 2.4 收 list[dict]，
    2.5 起 `MilvusClient.create_index` 会 `validate_param(..., IndexParams)`，
    传 list 直接抛 `ParamError: expected type: [IndexParams], got type: [list]`
    —— 而且这是**客户端侧**校验，服务端是不是 Milvus 2.4 都一样会炸。
    """
    params = IndexParams()
    params.add_index(
        DENSE_FIELD,
        index_type=settings.MILVUS_INDEX_TYPE,  # HNSW
        metric_type=settings.MILVUS_METRIC_TYPE,  # COSINE
        params={"M": 16, "efConstruction": 200},
    )
    params.add_index(
        SPARSE_FIELD,
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="IP",
        params={"drop_ratio_build": 0.2},
    )
    return params


def build_summaries_schema(dim: int | None = None) -> CollectionSchema:
    """paper_summaries：id / paper_id / dense_embedding。"""
    dim = dim or settings.MILVUS_DENSE_DIM
    return CollectionSchema(
        fields=[
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True),
            FieldSchema(name="paper_id", dtype=DataType.INT64),
            FieldSchema(name=DENSE_FIELD, dtype=DataType.FLOAT_VECTOR, dim=dim),
        ],
        description="论文级摘要向量，用于语义去重与论文级检索",
    )


def build_summaries_index_params() -> IndexParams:
    params = IndexParams()
    params.add_index(
        DENSE_FIELD,
        index_type=settings.MILVUS_INDEX_TYPE,
        metric_type=settings.MILVUS_METRIC_TYPE,
        params={"M": 16, "efConstruction": 200},
    )
    return params


def _registry() -> dict[str, tuple[Any, Any]]:
    """集合名 → (schema 构造器, index 构造器)。

    读 settings 放在**调用时**而不是 import 时 —— 否则测试里改环境变量不生效，
    而且 `import app.db.milvus` 会隐式把配置固化下来。
    """
    return {
        settings.MILVUS_CHUNKS_COLLECTION: (build_chunks_schema, build_chunks_index_params),
        settings.MILVUS_SUMMARIES_COLLECTION: (build_summaries_schema, build_summaries_index_params),
    }


# ---------------------------------------------------------------- 工具
def sparse_to_dict(sparse: Any) -> dict[int, float]:
    """把 bge-m3 的稀疏输出规范化成 {token_id: weight}。

    兼容三种形态：dict、`{indices, values}` 的类 scipy 对象、list[tuple]。
    bge-m3 官方封装在不同版本里返回过这三种，写死一种会在升级时炸。
    """
    if sparse is None:
        return {}
    if isinstance(sparse, dict):
        return {int(k): float(v) for k, v in sparse.items()}
    if hasattr(sparse, "indices") and hasattr(sparse, "values"):
        return {int(i): float(v) for i, v in zip(sparse.indices, sparse.values, strict=False)}
    if isinstance(sparse, (list, tuple)):
        return {int(i): float(v) for i, v in sparse}
    raise ValueError(f"无法识别的稀疏向量格式: {type(sparse)!r}")


def _dtype_name(raw: Any) -> str:
    """把服务端回读的字段类型码转成可读名字（5 → INT64，101 → FLOAT_VECTOR）。

    回读出来的是数字，`"101"` 这种值在验收报告里没法看，也没法在测试里断言语义。
    """
    try:
        return DataType(int(raw)).name
    except (ValueError, TypeError):
        return str(raw)


@dataclass(slots=True)
class VectorHit:
    """一路召回的结果项。score 的语义随 metric 变化，跨路比较无意义 —— 交给 RRF。

    刻意**不带 content**：正文回 PostgreSQL 取（见模块头）。
    """

    chunk_id: int
    paper_id: int
    score: float
    source: str = "vector"  # vector | dense | summary | graph | web

    @classmethod
    def from_entity(cls, entity: dict[str, Any], score: float, source: str = "vector") -> VectorHit:
        # 优先 chunk_id；只有主键时用 id 兜底 —— 本项目里两者是同一个值
        chunk_id = entity.get("chunk_id") or entity.get("id") or 0
        return cls(
            chunk_id=int(chunk_id),
            paper_id=int(entity.get("paper_id") or 0),
            score=float(score),
            source=source,
        )


# ---------------------------------------------------------------- 访问层
class MilvusStore:
    def __init__(self, uri: str | None = None) -> None:
        self._uri = uri or settings.MILVUS_LITE_PATH or f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
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

    @property
    def chunks(self) -> str:
        return settings.MILVUS_CHUNKS_COLLECTION

    @property
    def summaries(self) -> str:
        return settings.MILVUS_SUMMARIES_COLLECTION

    # ------------------------------------------------------------ 建表
    def ensure_collections(self, *, recreate: bool = False) -> list[str]:
        """幂等建两个集合 + 索引。已存在且非 recreate 就跳过。返回实际创建的集合名。"""
        client = self.client
        created: list[str] = []
        for name, (schema_fn, index_fn) in _registry().items():
            if client.has_collection(name):
                if not recreate:
                    continue
                logger.warning("重建 Milvus 集合 {}（原有向量将被删除）", name)
                client.drop_collection(name)
            client.create_collection(
                collection_name=name,
                schema=schema_fn(),
                index_params=index_fn(),
            )
            logger.info("已创建 Milvus 集合 {} 及其索引", name)
            created.append(name)
        return created

    # ------------------------------------------------------------ 写入
    def upsert_chunks(self, records: list[dict[str, Any]]) -> int:
        """按主键 upsert chunk 向量。

        record 需含：id(int)、paper_id(int)、chunk_id(int)、dense(list[float])、sparse(dict)。
        `id` 与 `chunk_id` 本项目取同一个值 —— 一条 chunk 一个向量，没有一对多的必要；
        拆成两个字段是为了留出"同一 chunk 多向量（多种粒度）"的余地。
        """
        if not records:
            return 0
        payload = [
            {
                "id": int(r["id"]),
                "paper_id": int(r["paper_id"]),
                "chunk_id": int(r["chunk_id"]),
                DENSE_FIELD: [float(x) for x in r["dense"]],
                SPARSE_FIELD: sparse_to_dict(r.get("sparse")),
            }
            for r in records
        ]
        res = self.client.upsert(collection_name=self.chunks, data=payload)
        return int(res.get("upsert_count", len(payload))) if isinstance(res, dict) else len(payload)

    def upsert_summaries(self, records: list[dict[str, Any]]) -> int:
        """论文级摘要向量。record 需含 id / paper_id / dense。"""
        if not records:
            return 0
        payload = [
            {"id": int(r["id"]), "paper_id": int(r["paper_id"]), DENSE_FIELD: [float(x) for x in r["dense"]]}
            for r in records
        ]
        res = self.client.upsert(collection_name=self.summaries, data=payload)
        return int(res.get("upsert_count", len(payload))) if isinstance(res, dict) else len(payload)

    def delete_paper(self, paper_id: int) -> None:
        """删一篇论文在两个集合里的全部向量。"""
        client = self.client
        for name in (self.chunks, self.summaries):
            if client.has_collection(name):
                client.delete(collection_name=name, filter=f"paper_id == {int(paper_id)}")

    def delete_chunks(self, chunk_ids: list[int]) -> None:
        if not chunk_ids or not self.client.has_collection(self.chunks):
            return
        self.client.delete(collection_name=self.chunks, ids=[int(i) for i in chunk_ids])

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
        """Dense + Sparse 两路召回 → Milvus 原生 RRF 融合。

        RRF 基于**排名**而非原始分数合并，所以 cosine 分与 IP 分不可比这件事
        不影响结果，也不需要手工调权重。
        """
        limit = limit or settings.RETRIEVAL_TOP_K
        recall_k = recall_k or settings.RETRIEVAL_RECALL_K
        rrf_k = rrf_k or settings.RRF_K

        reqs = [
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
                collection_name=self.chunks,
                reqs=reqs,
                ranker=RRFRanker(rrf_k),
                limit=limit,
                output_fields=["id", "paper_id", "chunk_id"],
            )
        except Exception as exc:  # noqa: BLE001 —— 统一转业务异常，保留原始信息
            raise RetrievalError(f"Milvus 混合检索失败: {exc}") from exc
        return [
            VectorHit.from_entity(dict(h.get("entity") or {}), h.get("distance", 0.0))
            for group in (raw or [])
            for h in group
        ]

    def dense_search(self, dense: list[float], *, limit: int = 10, expr: str | None = None) -> list[VectorHit]:
        """仅稠密路。用于对照实现与稀疏不可用时的降级。"""
        raw = self.client.search(
            collection_name=self.chunks,
            data=[list(dense)],
            anns_field=DENSE_FIELD,
            search_params={"metric_type": settings.MILVUS_METRIC_TYPE, "params": {"ef": 64}},
            limit=limit,
            output_fields=["id", "paper_id", "chunk_id"],
            filter=expr or "",
        )
        return [
            VectorHit.from_entity(dict(h.get("entity") or {}), h.get("distance", 0.0), source="dense")
            for group in (raw or [])
            for h in group
        ]

    def search_summaries(self, dense: list[float], *, limit: int = 10) -> list[VectorHit]:
        """论文级检索 / 语义去重：返回 (paper_id, 相似度)。"""
        raw = self.client.search(
            collection_name=self.summaries,
            data=[list(dense)],
            anns_field=DENSE_FIELD,
            search_params={"metric_type": settings.MILVUS_METRIC_TYPE, "params": {"ef": 64}},
            limit=limit,
            output_fields=["id", "paper_id"],
        )
        return [
            VectorHit(
                chunk_id=int((h.get("entity") or {}).get("id") or 0),
                paper_id=int((h.get("entity") or {}).get("paper_id") or 0),
                score=float(h.get("distance", 0.0)),
                source="summary",
            )
            for group in (raw or [])
            for h in group
        ]

    # ------------------------------------------------------------ 自检
    def describe(self) -> dict[str, Any]:
        """把两个集合的字段与索引参数读回来。

        验收要证明的是「索引真的建上了」，而不是「我们调过创建接口」——
        这两件事在接口层面看起来一模一样，只有回读才能区分。
        """
        client = self.client
        out: dict[str, Any] = {}
        for name, (_schema_fn, index_fn) in _registry().items():
            if not client.has_collection(name):
                out[name] = {"exists": False}
                continue
            info = client.describe_collection(name)
            fields = [
                {
                    "name": f.get("name"),
                    "type": _dtype_name(f.get("type")),
                    "dim": (f.get("params") or {}).get("dim"),
                }
                for f in info.get("fields", [])
            ]
            indexes = []
            for idx_name in client.list_indexes(name):
                desc = client.describe_index(name, idx_name)
                indexes.append(
                    {
                        "field": desc.get("field_name"),
                        "type": desc.get("index_type"),
                        "metric": desc.get("metric_type"),
                        "params": desc.get("params"),
                    }
                )
            out[name] = {
                "exists": True,
                "fields": fields,
                "indexes": indexes,
                "expected_indexes": [p.to_dict() for p in index_fn()],
            }
        return out

    def health(self) -> tuple[bool, str]:
        try:
            client = self.client
            parts: list[str] = []
            ok = True
            for name in _registry():
                if not client.has_collection(name):
                    ok = False
                    parts.append(f"{name}=未创建")
                    continue
                stats = client.get_collection_stats(name)
                parts.append(f"{name} 行数={stats.get('row_count', '?')}")
            return ok, "；".join(parts)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@lru_cache
def get_store() -> MilvusStore:
    return MilvusStore()


# ---------------------------------------------------------------- async 包装
# pymilvus 同步调用放进线程池，避免阻塞事件循环。FastAPI 与 Agent 节点统一用这一层。


async def aensure_collections(*, recreate: bool = False) -> list[str]:
    return await asyncio.to_thread(get_store().ensure_collections, recreate=recreate)


async def aupsert_chunks(records: list[dict[str, Any]]) -> int:
    return await asyncio.to_thread(get_store().upsert_chunks, records)


async def aupsert_summaries(records: list[dict[str, Any]]) -> int:
    return await asyncio.to_thread(get_store().upsert_summaries, records)


async def ahybrid_search(dense: list[float], sparse: Any, **kwargs: Any) -> list[VectorHit]:
    return await asyncio.to_thread(lambda: get_store().hybrid_search(dense, sparse, **kwargs))


async def adense_search(dense: list[float], **kwargs: Any) -> list[VectorHit]:
    return await asyncio.to_thread(lambda: get_store().dense_search(dense, **kwargs))


async def asearch_summaries(dense: list[float], **kwargs: Any) -> list[VectorHit]:
    return await asyncio.to_thread(lambda: get_store().search_summaries(dense, **kwargs))


async def adelete_paper(paper_id: int) -> None:
    await asyncio.to_thread(get_store().delete_paper, paper_id)


async def ahealth() -> tuple[bool, str]:
    return await asyncio.to_thread(get_store().health)


def describe_collections() -> dict[str, Any]:
    return get_store().describe()


__all__ = [
    "DENSE_FIELD",
    "SPARSE_FIELD",
    "MilvusStore",
    "VectorHit",
    "adelete_paper",
    "adense_search",
    "aensure_collections",
    "ahealth",
    "ahybrid_search",
    "asearch_summaries",
    "aupsert_chunks",
    "aupsert_summaries",
    "build_chunks_index_params",
    "build_chunks_schema",
    "build_summaries_index_params",
    "build_summaries_schema",
    "describe_collections",
    "get_store",
    "sparse_to_dict",
]
