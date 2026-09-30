"""Milvus 两个 collection + 索引的**真实**验证 —— 覆盖验收标准 2。

不用 Docker：`milvus_lite` 起的是一个真正的 Milvus 引擎（Rust 实现，本地文件落盘），
支持本次用到的全部特性 —— INT64 主键、FLOAT_VECTOR(dim=1024)、
SPARSE_FLOAT_VECTOR、HNSW、SPARSE_INVERTED_INDEX、以及原生 RRFRanker。

为什么必须真连一次而不是只测 schema 构造器：
    `build_chunks_schema()` 返回的对象长什么样，和"服务端是否接受并真的建上索引"
    是两件事。本项目踩过的正是这个 —— 索引参数在 pymilvus 2.5 起从 `list[dict]`
    改成了必须传 `IndexParams`，构造出来的东西看着没错，一 create 就
    `ParamError: expected type: [IndexParams], got type: [list]`。
    只有真调一次 + `describe()` 回读，才能把"我们调过接口"和"索引真的存在"分开。

需要 `milvus-lite`：`pip install "pymilvus[milvus_lite]>=2.5,<3"`。没装则整文件跳过。
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

pytest.importorskip("milvus_lite", reason="Milvus 集成验证需要 milvus-lite")

from app.db.milvus import (  # noqa: E402
    DENSE_FIELD,
    SPARSE_FIELD,
    MilvusStore,
    sparse_to_dict,
)

pytestmark = pytest.mark.integration

DIM = 1024


def unit_dense(seed: int, dim: int = DIM) -> list[float]:
    """造一个确定性的单位向量。用种子而不是随机数，失败时可复现。"""
    rnd = random.Random(seed)
    vec = [rnd.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


@pytest.fixture
def store(tmp_path) -> Iterator[MilvusStore]:
    """每个用例一个独立的 milvus-lite 文件，互不干扰。"""
    s = MilvusStore(uri=str(tmp_path / "milvus.db"))
    yield s
    s.close()


# ==================================================================== 建表
def test_both_collections_and_indexes_exist_after_create(store):
    created = store.ensure_collections()

    assert sorted(created) == ["paper_chunks", "paper_summaries"]

    detail = store.describe()
    assert detail["paper_chunks"]["exists"] is True
    assert detail["paper_summaries"]["exists"] is True


def test_chunks_schema_matches_spec(store):
    """规格：id / paper_id / chunk_id / dense(1024) / sparse。字段与类型都要对。"""
    store.ensure_collections()

    fields = {f["name"]: f for f in store.describe()["paper_chunks"]["fields"]}

    assert set(fields) == {"id", "paper_id", "chunk_id", DENSE_FIELD, SPARSE_FIELD}
    assert fields["id"]["type"] == "INT64"  # 对齐 Milvus INT64，不是 UUID 字符串
    assert fields["paper_id"]["type"] == "INT64"
    assert fields["chunk_id"]["type"] == "INT64"
    assert fields[DENSE_FIELD]["type"] == "FLOAT_VECTOR"
    assert fields[DENSE_FIELD]["dim"] == DIM
    assert fields[SPARSE_FIELD]["type"] == "SPARSE_FLOAT_VECTOR"


def test_chunks_indexes_are_actually_built(store):
    """回读索引，而不是"我们调过创建接口"。"""
    store.ensure_collections()

    indexes = {i["field"]: i for i in store.describe()["paper_chunks"]["indexes"]}

    assert indexes[DENSE_FIELD]["type"] == "HNSW"
    assert indexes[DENSE_FIELD]["metric"] == "COSINE"
    assert indexes[SPARSE_FIELD]["type"] == "SPARSE_INVERTED_INDEX"
    assert indexes[SPARSE_FIELD]["metric"] == "IP"


def test_summaries_schema_and_index(store):
    store.ensure_collections()

    detail = store.describe()["paper_summaries"]
    fields = {f["name"]: f for f in detail["fields"]}

    assert set(fields) == {"id", "paper_id", DENSE_FIELD}
    assert fields[DENSE_FIELD]["type"] == "FLOAT_VECTOR"
    assert fields[DENSE_FIELD]["dim"] == DIM
    assert [i["type"] for i in detail["indexes"]] == ["HNSW"]


def test_ensure_collections_is_idempotent(store):
    assert len(store.ensure_collections()) == 2
    # 第二次必须什么都不建，否则每次启动都会重建集合、清空全部向量
    assert store.ensure_collections() == []


def test_recreate_drops_and_rebuilds(store):
    store.ensure_collections()
    store.upsert_chunks([{"id": 1, "paper_id": 1, "chunk_id": 1, "dense": unit_dense(1), "sparse": {5: 0.5}}])
    assert store.client.get_collection_stats(store.chunks)["row_count"] == 1

    assert sorted(store.ensure_collections(recreate=True)) == ["paper_chunks", "paper_summaries"]
    assert store.client.get_collection_stats(store.chunks)["row_count"] == 0


# ==================================================================== 写入
def test_upsert_chunks_round_trip(store):
    store.ensure_collections()

    written = store.upsert_chunks(
        [
            {"id": 1, "paper_id": 10, "chunk_id": 1, "dense": unit_dense(1), "sparse": {11: 0.9, 12: 0.4}},
            {"id": 2, "paper_id": 10, "chunk_id": 2, "dense": unit_dense(2), "sparse": {11: 0.1, 13: 0.8}},
        ]
    )

    assert written == 2
    assert store.client.get_collection_stats(store.chunks)["row_count"] == 2


def test_upsert_is_keyed_by_primary_key(store):
    """同一 id 再写一次是覆盖而不是新增 —— 重建索引时靠这条保证不出现重影。"""
    store.ensure_collections()
    row = {"id": 7, "paper_id": 1, "chunk_id": 7, "dense": unit_dense(1), "sparse": {5: 0.5}}

    store.upsert_chunks([row])
    store.upsert_chunks([{**row, "dense": unit_dense(2)}])

    assert store.client.get_collection_stats(store.chunks)["row_count"] == 1


def test_upsert_empty_is_a_noop(store):
    store.ensure_collections()
    assert store.upsert_chunks([]) == 0
    assert store.upsert_summaries([]) == 0


# ==================================================================== 检索
def test_hybrid_search_fuses_two_paths_with_rrf(store):
    """RRF 融合的判据：分数必须等于 Σ 1/(k+rank)。

    这条断言比"返回了 3 条"强得多 —— 它证明走的是 Milvus 原生 RRFRanker 且
    k 真的是 60，而不是哪一路单跑被误当成融合结果。
    """
    store.ensure_collections()
    store.upsert_chunks(
        [
            {"id": 1, "paper_id": 10, "chunk_id": 1, "dense": unit_dense(1), "sparse": {11: 0.9, 12: 0.4}},
            {"id": 2, "paper_id": 10, "chunk_id": 2, "dense": unit_dense(2), "sparse": {11: 0.1, 13: 0.8}},
            {"id": 3, "paper_id": 11, "chunk_id": 3, "dense": unit_dense(3), "sparse": {14: 0.7}},
        ]
    )

    hits = store.hybrid_search(unit_dense(1), {11: 1.0, 12: 0.5}, limit=3, recall_k=3)

    assert [h.chunk_id for h in hits] == [1, 2, 3]
    k = 60  # settings.RRF_K
    assert hits[0].score == pytest.approx(1 / (k + 1) + 1 / (k + 1), rel=1e-4)  # 两路都排第一
    assert hits[1].score == pytest.approx(1 / (k + 2) + 1 / (k + 2), rel=1e-4)
    assert hits[2].score == pytest.approx(1 / (k + 3), rel=1e-4)  # 只在稠密路出现


def test_hybrid_search_returns_int_ids_only(store):
    """集合里不存正文，所以返回项只能是 id —— 正文必须回 PostgreSQL 取。"""
    store.ensure_collections()
    store.upsert_chunks([{"id": 1, "paper_id": 10, "chunk_id": 1, "dense": unit_dense(1), "sparse": {11: 0.9}}])

    hit = store.hybrid_search(unit_dense(1), {11: 1.0}, limit=1, recall_k=1)[0]

    assert (hit.chunk_id, hit.paper_id) == (1, 10)
    assert isinstance(hit.chunk_id, int) and isinstance(hit.paper_id, int)


def test_dense_search_respects_expr_filter(store):
    store.ensure_collections()
    store.upsert_chunks(
        [
            {"id": 1, "paper_id": 10, "chunk_id": 1, "dense": unit_dense(1), "sparse": {11: 0.9}},
            {"id": 3, "paper_id": 11, "chunk_id": 3, "dense": unit_dense(3), "sparse": {14: 0.7}},
        ]
    )

    hits = store.dense_search(unit_dense(1), limit=5, expr="paper_id == 11")

    assert [h.paper_id for h in hits] == [11]
    assert hits[0].source == "dense"


def test_summaries_search_returns_paper_level_hits(store):
    """语义去重要用的就是这条路：给定新论文摘要，找出最像的已有 paper_id。"""
    store.ensure_collections()
    store.upsert_summaries(
        [
            {"id": 1, "paper_id": 10, "dense": unit_dense(1)},
            {"id": 2, "paper_id": 11, "dense": unit_dense(2)},
        ]
    )

    hits = store.search_summaries(unit_dense(2), limit=2)

    assert [h.paper_id for h in hits] == [11, 10]
    assert hits[0].source == "summary"
    assert hits[0].score > hits[1].score


def test_delete_paper_removes_vectors_from_both_collections(store):
    store.ensure_collections()
    store.upsert_chunks(
        [
            {"id": 1, "paper_id": 10, "chunk_id": 1, "dense": unit_dense(1), "sparse": {11: 0.9}},
            {"id": 3, "paper_id": 11, "chunk_id": 3, "dense": unit_dense(3), "sparse": {14: 0.7}},
        ]
    )
    store.upsert_summaries([{"id": 1, "paper_id": 11, "dense": unit_dense(3)}])

    store.delete_paper(11)

    assert store.client.get_collection_stats(store.chunks)["row_count"] == 1
    assert store.client.get_collection_stats(store.summaries)["row_count"] == 0


def test_health_reports_row_counts(store):
    store.ensure_collections()
    store.upsert_chunks([{"id": 1, "paper_id": 1, "chunk_id": 1, "dense": unit_dense(1), "sparse": {5: 0.5}}])

    ok, detail = store.health()

    assert ok is True
    assert "paper_chunks 行数=1" in detail
    assert "paper_summaries 行数=0" in detail


# ==================================================================== 纯函数
@pytest.mark.unit
def test_sparse_to_dict_normalizes_three_shapes():
    """bge-m3 封装在不同版本里返回过三种形态，写死一种会在升级时炸。"""

    class ScipyLike:
        indices = [1, 2]
        values = [0.5, 0.25]

    assert sparse_to_dict(None) == {}
    assert sparse_to_dict({1: 0.5}) == {1: 0.5}
    assert sparse_to_dict({"3": 0.5}) == {3: 0.5}  # JSON 往返后 key 会变成字符串
    assert sparse_to_dict(ScipyLike()) == {1: 0.5, 2: 0.25}
    assert sparse_to_dict([(1, 0.5), (2, 0.25)]) == {1: 0.5, 2: 0.25}
    with pytest.raises(ValueError, match="无法识别的稀疏向量格式"):
        sparse_to_dict(42)
