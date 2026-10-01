"""混合检索主链路的**条数阶梯**与**来源标记**。

这条链路有三处截断：每路召回 20 → RRF 融合取 10 → 重排取 5。它是最容易
"实现时少做一级"的地方 —— 调错一个数字不会报错，只会让生成侧看到不一样的
上下文，端到端测试照样全绿。所以这里把三级各自钉死，并把"来源标记真的落在
结果对象上"一起验掉（前端溯源面板要靠它区分 dense / sparse / both）。

Milvus 与 PG 在这里全被替换掉：本文件只关心**编排**是否正确，
真实 ANN 检索由 `test_milvus_collections.py`（需要真库）负责。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.milvus import VectorHit
from app.embeddings.bge_m3 import EmbeddingResult
from app.rag.retriever import HybridRetriever


class FakeEmbedder:
    """返回固定的双路向量 —— 不加载 bge-m3（那要 2GB 权重）。"""

    def __init__(self, *, sparse: bool = True) -> None:
        self.sparse_available = sparse
        self.calls: list[str] = []

    def encode_query(self, query: str, **kwargs: Any) -> EmbeddingResult:
        self.calls.append(query)
        return EmbeddingResult(
            dense=[[0.1, 0.2, 0.3]],
            sparse=[{11: 0.9, 12: 0.4}] if self.sparse_available else [],
        )


def _hit(chunk_id: int, *, paper_id: int = 1, score: float = 0.9, source: str = "dense") -> VectorHit:
    return VectorHit(chunk_id=chunk_id, paper_id=paper_id, score=score, source=source)


class FakeStore:
    """假的 ANN 层：记录被调用的 limit/expr，返回调用方指定的命中。"""

    def __init__(self, dense_hits: list[VectorHit], sparse_hits: list[VectorHit]) -> None:
        self.dense_hits, self.sparse_hits = dense_hits, sparse_hits
        self.dense_calls: list[dict[str, Any]] = []
        self.sparse_calls: list[dict[str, Any]] = []

    async def dense(self, vector: list[float], **kwargs: Any) -> list[VectorHit]:
        self.dense_calls.append(kwargs)
        return list(self.dense_hits)

    async def sparse(self, vector: Any, **kwargs: Any) -> list[VectorHit]:
        self.sparse_calls.append(kwargs)
        return list(self.sparse_hits)


class FakeReranker:
    def __init__(self) -> None:
        self.received: list[int] = []
        self.top_k: int | None = None
        self.score: float | None = None  # 给了就写 rerank_score，用来验分数覆盖

    async def arerank(self, query: str, chunks: list, *, top_k: int | None = None) -> list:
        self.received.append(len(chunks))
        self.top_k = top_k
        if self.score is not None:
            for c in chunks:
                c.rerank_score = self.score
        return chunks[:top_k] if top_k else chunks


@pytest.fixture
def wired(monkeypatch):
    """装配一台检索器：ANN、回表、重排全部换成假的，返回 (retriever, store, reranker)。"""

    def _install(
        dense_hits: list[VectorHit] | None = None,
        sparse_hits: list[VectorHit] | None = None,
        *,
        sparse_available: bool = True,
    ):
        store = FakeStore(dense_hits if dense_hits is not None else [_hit(i) for i in range(1, 21)], sparse_hits or [])
        reranker = FakeReranker()
        embedder = FakeEmbedder(sparse=sparse_available)

        async def fake_hydrate(chunks):
            for c in chunks:
                c.content = f"片段正文 {c.id}"
            return list(chunks)

        monkeypatch.setattr("app.rag.retriever.adense_search", store.dense)
        monkeypatch.setattr("app.rag.retriever.asparse_search", store.sparse)
        monkeypatch.setattr("app.rag.retriever.hydrate_chunks", fake_hydrate)
        monkeypatch.setattr("app.rag.reranker.get_reranker", lambda: reranker)

        retriever = HybridRetriever()
        retriever._embedder = embedder  # 绕开真模型
        return retriever, store, reranker

    return _install


# ---------------------------------------------------------------- 三级条数
@pytest.mark.unit
async def test_each_path_recalls_configured_top_k(wired, settings):
    """Dense 与 Sparse 各自按 RETRIEVAL_RECALL_K 召回（规格：各 top_k=20）。"""
    retriever, store, _ = wired()
    await retriever.retrieve("问题")

    assert settings.RETRIEVAL_RECALL_K == 20
    assert [c["limit"] for c in store.dense_calls] == [20]
    assert [c["limit"] for c in store.sparse_calls] == [20]


@pytest.mark.unit
async def test_fusion_truncates_to_rrf_top_k(wired, settings):
    """融合后截断到 RRF_TOP_K=10（不带重排时这是最终条数）。"""
    retriever, _, _ = wired(
        dense_hits=[_hit(i) for i in range(1, 21)],
        sparse_hits=[_hit(i, source="sparse") for i in range(1, 21)],
    )
    out = await retriever.retrieve("问题", rerank=False)

    assert settings.RRF_TOP_K == 10
    assert len(out) == 10
    assert [c.id for c in out] == list(range(1, 11))  # 两路同序 → 名次保持不变


@pytest.mark.unit
async def test_rerank_sees_rrf_candidates_and_returns_rerank_top_k(wired, settings):
    """重排吃到的是 RRF 的 10 条，吐出的是 RERANK_TOP_K=5 条。"""
    retriever, _, reranker = wired(
        dense_hits=[_hit(i) for i in range(1, 21)],
        sparse_hits=[_hit(i, source="sparse") for i in range(1, 21)],
    )
    out = await retriever.retrieve("问题")

    assert settings.RERANK_TOP_K == 5
    assert reranker.received == [10]  # 重排的输入是融合后的 10 条
    assert reranker.top_k == 5
    assert len(out) == 5


@pytest.mark.unit
async def test_explicit_top_k_caps_the_final_result(wired):
    """调用方显式给 top_k 时以它为准（`/qa/retrieve` 的调参入口就靠这个）。"""
    retriever, _, _ = wired(
        dense_hits=[_hit(i) for i in range(1, 21)],
        sparse_hits=[_hit(i, source="sparse") for i in range(1, 21)],
    )
    assert len(await retriever.retrieve("问题", top_k=3)) == 3


# ---------------------------------------------------------------- 来源标记
@pytest.mark.unit
async def test_results_carry_which_path_recalled_them(wired):
    """来源标记必须落到返回的对象上 —— 溯源面板显示 dense / sparse / both。"""
    retriever, _, _ = wired(
        dense_hits=[_hit(1), _hit(2)],
        sparse_hits=[_hit(2, source="sparse"), _hit(3, source="sparse")],
    )
    out = await retriever.retrieve("问题", rerank=False)
    by_id = {c.id: c.sources for c in out}

    assert by_id[1] == ["dense"]
    assert by_id[3] == ["sparse"]
    assert by_id[2] == ["dense", "sparse"]


@pytest.mark.unit
async def test_score_is_the_fused_rank_score_not_the_raw_ann_score(wired):
    """分数换成 RRF 分：cosine 与 IP 不同量纲，混在一起下游无法比较。

    两路同序召回的 chunk 拿 2/(60+rank)，只被一路召回的拿 1/(60+rank)。
    """
    retriever, _, _ = wired(
        dense_hits=[_hit(1, score=0.99)],
        sparse_hits=[_hit(1, score=42.0, source="sparse"), _hit(2, score=7.0, source="sparse")],
    )
    out = await retriever.retrieve("问题", rerank=False)
    scores = {c.id: c.score for c in out}

    assert scores[1] == pytest.approx(2 / 61)  # 两路都排第 1
    assert scores[2] == pytest.approx(1 / 62)  # 只在 sparse 排第 2
    assert 0.99 not in scores.values() and 42.0 not in scores.values()


@pytest.mark.unit
async def test_rerank_overwrites_score_but_keeps_source_tags(wired):
    """重排会写 rerank_score（`final_score` 优先取它），但来源标记不能被冲掉。"""
    retriever, _, reranker = wired(dense_hits=[_hit(i) for i in range(1, 4)])
    reranker.score = 7.5
    out = await retriever.retrieve("问题")

    assert out and out[0].rerank_score == 7.5
    assert out[0].final_score == 7.5
    assert out[0].sources == ["dense"]


# ---------------------------------------------------------------- 降级与过滤
@pytest.mark.unit
async def test_missing_sparse_channel_degrades_to_dense_only(wired):
    """bge-m3 没给出稀疏向量时只跑稠密路，来源标记诚实地只剩 dense。"""
    retriever, store, _ = wired(dense_hits=[_hit(1), _hit(2)], sparse_available=False)
    out = await retriever.retrieve("问题", rerank=False)

    assert store.sparse_calls == []  # 不拿空向量去问 Milvus
    assert [c.sources for c in out] == [["dense"], ["dense"]]


@pytest.mark.unit
async def test_both_paths_empty_returns_empty(wired):
    retriever, _, _ = wired(dense_hits=[], sparse_hits=[])
    assert await retriever.retrieve("问题") == []


@pytest.mark.unit
async def test_embedding_failure_returns_empty_without_touching_milvus(wired):
    """嵌入失败时直接返回空，不去打 Milvus（那只会拿到一堆无关结果）。"""
    retriever, store, _ = wired()

    class _Broken:
        def encode_query(self, query, **kwargs):
            return EmbeddingResult(dense=[], sparse=[])

    retriever._embedder = _Broken()
    assert await retriever.retrieve("问题") == []
    assert store.dense_calls == []


@pytest.mark.unit
async def test_paper_filter_is_pushed_down_to_both_paths(wired):
    """限定论文范围要用 Milvus 过滤表达式下发，而不是取回来再筛。"""
    retriever, store, _ = wired(dense_hits=[_hit(1)], sparse_hits=[_hit(1, source="sparse")])
    await retriever.retrieve("问题", paper_ids=[7, 9], rerank=False)

    assert store.dense_calls[0]["expr"] == "paper_id in [7, 9]"
    assert store.sparse_calls[0]["expr"] == "paper_id in [7, 9]"


@pytest.mark.unit
async def test_no_paper_filter_means_no_expr(wired):
    retriever, store, _ = wired(dense_hits=[_hit(1)], sparse_hits=[_hit(1, source="sparse")])
    await retriever.retrieve("问题", rerank=False)
    assert store.dense_calls[0]["expr"] is None


@pytest.mark.unit
async def test_extra_recall_path_is_fused_into_the_result(wired, settings):
    """第三路（引文图谱 / Web 兜底）要能被并进结果，且不挤掉主路的高排位项。"""
    from app.rag.retriever import RetrievedChunk

    retriever, _, _ = wired(
        dense_hits=[_hit(i) for i in range(1, 4)],
        sparse_hits=[_hit(i, source="sparse") for i in range(1, 4)],
    )
    graph_path = [
        RetrievedChunk(id=900, paper_id=1, content="图谱召回", sources=["graph"]),
        RetrievedChunk(id=901, paper_id=1, content="图谱召回 2", sources=["graph"]),
    ]
    out = await retriever.retrieve("问题", rerank=False, extra_paths=[graph_path], extra_weights=[0.5])
    ids = [c.id for c in out]

    assert {900, 901} <= set(ids)
    assert ids[0] == 1  # 双路都排第一的主路结果不该被权重 0.5 的图谱路顶掉
