"""Reciprocal Rank Fusion。

RRF 用**排名**而不是原始分数做融合：

    score(d) = Σ_r  1 / (k + rank_r(d))        k = 60（论文默认值）

为什么必须用它，而不是加权求和原始分数
----------------------------------------
1. 各路分数**不同量纲**：Milvus 的 COSINE 分在 [-1, 1]，IP 分无上界，图召回的
   相似度又是另一个尺度。加权求和需要人工调权重，且权重随语料漂移。
2. RRF 只看名次，天然免疫量纲问题，也无需归一化。
3. 对"某一路上排名靠前"的文档给足奖励，避免单个召回源的偏差主导结果。

Milvus 的 `RRFRanker` 已经实现了 dense+sparse 两路的 RRF（见 app.db.milvus）。
这里再实现一份纯 Python 版本，两个用途：
1. `rrf_fuse(dense, sparse, k=60)` —— 两路融合**并标注来源**（dense / sparse / both），
   原生 ranker 不返回这个归属；
2. `reciprocal_rank_fusion(rankings)` —— 融合**第三路及以上**的召回源
   （引文图谱召回、Web Search 兜底），因为 Milvus 只认识它自己集合内的那两路。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

DEFAULT_K = 60

# 来源标记。值是给前端直接显示的，所以用英文小写单词而不是枚举。
DENSE_SOURCE = "dense"
SPARSE_SOURCE = "sparse"


class _HasId(Protocol):
    id: str


class _HasSources(_HasId, Protocol):
    """融合结果要能带上"来自哪一路"，没有这个属性就没法标来源。"""

    sources: list[str]


T = TypeVar("T", bound=_HasId)
F = TypeVar("F", bound=_HasSources)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[T]],
    *,
    k: int = DEFAULT_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[T, float]]:
    """融合多路有序结果。

    参数
    ----
    rankings : 每路一个**已按相关性降序排列**的列表
    k        : RRF 常数，默认 60。k 越大，排名差异被压得越平（越"民主"）。
    weights  : 每路权重，默认全 1。用于表达"某路更可信"（如 arXiv 元数据 vs Web 兜底）。

    返回
    ----
    [(item, rrf_score)] 按分数降序。**同一 id 只在首次出现时保留实例**，
    避免不同路返回的对象被重复计数。
    """
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(f"weights 长度 {len(weights)} 与 rankings 路数 {len(rankings)} 不一致")

    scores: dict[str, float] = {}
    first_seen: dict[str, T] = {}

    for path_idx, ranked in enumerate(rankings):
        weight = 1.0 if weights is None else weights[path_idx]
        for rank, item in enumerate(ranked, start=1):
            key = item.id
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
            first_seen.setdefault(key, item)

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(first_seen[key], score) for key, score in ordered]


def rrf_fuse(
    dense_results: Sequence[F],
    sparse_results: Sequence[F],
    *,
    k: int = DEFAULT_K,
) -> list[tuple[F, float]]:
    """Dense + Sparse 两路 RRF 融合，并在每个结果上标注**来源**。

    与 Milvus 原生 `RRFRanker(k)` 的关系
    ------------------------------------
    公式完全一致（都是 Σ 1/(k+rank)），差别只有一个：原生 `hybrid_search` 只回
    融合后的名次，不回"这一条来自哪一路"。溯源面板要显示 dense / sparse / both，
    所以这里显式跑两路再融合。多一次 ANN 查询换来源归属，值。

    返回
    ----
    [(item, rrf_score)] 按分数降序。每个 item 的 `sources` 被**改写**为
    `["dense"]` / `["sparse"]` / `["dense", "sparse"]`（后者即两路都召回的 "both"）。
    改写而不是新建对象：融合结果继续往下走整条链路，拿到的必须是同一个实例。

    边界
    ----
    任一路为空 → 只由另一路贡献分数，`sources` 也只有那一个标记；
    两路都空 → 返回空列表（不抛错，检索链路对空结果有统一的降级处理）。
    """
    dense, sparse = list(dense_results), list(sparse_results)
    fused = reciprocal_rank_fusion([dense, sparse], k=k)

    in_dense = {item.id for item in dense}
    in_sparse = {item.id for item in sparse}
    for item, _ in fused:
        tags: list[str] = []
        if item.id in in_dense:
            tags.append(DENSE_SOURCE)
        if item.id in in_sparse:
            tags.append(SPARSE_SOURCE)
        item.sources = tags
    return fused


def dedupe_by_id(items: Sequence[T]) -> list[T]:
    """保持原顺序去重。检索链路里多处需要（同 chunk 被两路召回）。"""
    seen: set[str] = set()
    out: list[T] = []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        out.append(it)
    return out


def min_max_normalize(scores: Sequence[float]) -> list[float]:
    """把分数压到 [0,1]。仅用于**展示**（如前端引用条长度），不参与 RRF 计算。"""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-12:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def _demo() -> None:
    """自检：RRF 的核心性质。任何一条不成立就说明融合逻辑坏了。"""
    from dataclasses import dataclass, field

    @dataclass
    class Doc:
        id: str
        text: str = ""
        sources: list[str] = field(default_factory=list)

    a, b, c, d = Doc("a"), Doc("b"), Doc("c"), Doc("d")

    # 1) 两路都排第一的，必须总分最高
    fused = reciprocal_rank_fusion([[a, b], [a, c]])
    assert [x.id for x, _ in fused][0] == "a", fused

    # 2) 被两路都召回的，胜过只被一路召回的（这是 RRF 的核心价值）
    fused = reciprocal_rank_fusion([[a, b, c], [d, b, a]])
    by_id = {x.id: s for x, s in fused}
    assert by_id["b"] > by_id["d"], by_id

    # 3) k=60 时第一名贡献 = 1/61，且权重线性可加
    fused = reciprocal_rank_fusion([[a]])
    assert abs(fused[0][1] - 1 / 61) < 1e-12, fused
    fused = reciprocal_rank_fusion([[a]], weights=[2.0])
    assert abs(fused[0][1] - 2 / 61) < 1e-12, fused

    # 4) 去重：同一个 id 在两路出现，实例只保留一个
    fused = reciprocal_rank_fusion([[a, b], [Doc("a"), c]])
    assert sum(1 for x, _ in fused if x.id == "a") == 1, fused

    # 5) 空输入不炸
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []

    # 6) 权重长度不匹配要报错，而不是静默算错
    try:
        reciprocal_rank_fusion([[a], [b]], weights=[1.0])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("weights 长度不匹配时应抛 ValueError")

    assert dedupe_by_id([a, Doc("a"), b]).__len__() == 2
    assert min_max_normalize([5.0, 5.0]) == [1.0, 1.0]

    # 7) rrf_fuse：来源标记必须是 dense / sparse / 两者都有
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = rrf_fuse([a, b], [b, c])
    tags = {x.id: x.sources for x, _ in fused}
    assert tags["a"] == ["dense"], tags
    assert tags["c"] == ["sparse"], tags
    assert tags["b"] == ["dense", "sparse"], tags
    # 8) 任一路为空不能炸，来源标记也要跟着变
    assert [x.id for x, _ in rrf_fuse([], [])] == []
    assert [x.sources for x, _ in rrf_fuse([], [c])] == [["sparse"]]
    assert [x.sources for x, _ in rrf_fuse([a], [])] == [["dense"]]
    # 9) 两路完全重叠时分数翻倍（2/(k+rank)），顺序不变
    fused = rrf_fuse([a, b], [a, b])
    assert [x.id for x, _ in fused] == ["a", "b"]
    assert abs(fused[0][1] - 2 / 61) < 1e-12, fused

    print("fusion self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _demo()
