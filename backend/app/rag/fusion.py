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
这里再实现一份纯 Python 版本，用途是融合**第三路及以上的召回源**
（引文图谱召回、Web Search 兜底），因为 Milvus 只认识它自己集合内的那两路。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

DEFAULT_K = 60


class _HasId(Protocol):
    id: str


T = TypeVar("T", bound=_HasId)


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
    from dataclasses import dataclass

    @dataclass
    class Doc:
        id: str
        text: str = ""

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
    print("fusion self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _demo()
