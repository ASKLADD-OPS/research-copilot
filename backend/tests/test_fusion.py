"""RRF 融合的纯逻辑测试。

RRF 是整条检索链路的合流点：它错了，后面重排、CRAG、溯源全部建立在错的名次上。
所以这里把论文里定义 RRF 的几条性质逐条钉死，而不是"跑一遍看着对"。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.rag.fusion import (
    DEFAULT_K,
    dedupe_by_id,
    min_max_normalize,
    reciprocal_rank_fusion,
)


@dataclass
class Doc:
    id: str
    text: str = ""


@pytest.mark.unit
def test_default_k_is_paper_value():
    assert DEFAULT_K == 60


@pytest.mark.unit
def test_top_of_both_paths_wins():
    """两路都排第一 → 总分最高。"""
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = reciprocal_rank_fusion([[a, b], [a, c]])
    assert [d.id for d, _ in fused][0] == "a"


@pytest.mark.unit
def test_recalled_by_both_beats_recalled_by_one():
    """RRF 的核心价值：被多路同时召回的文档胜过只被一路召回的。"""
    a, b, c, d = Doc("a"), Doc("b"), Doc("c"), Doc("d")
    scores = {x.id: s for x, s in reciprocal_rank_fusion([[a, b, c], [d, b, a]])}
    # b 被两路都召回，d 只被一路召回 → b 胜
    assert scores["b"] > scores["d"]
    # 顺带钉住公式：a 是 (1/61 + 1/63)，略高于 b 的 (1/62 + 1/62)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["b"] == pytest.approx(2 / 62)


@pytest.mark.unit
def test_score_formula_matches_1_over_k_plus_rank():
    """严格按公式：Σ 1/(k+rank)，rank 从 1 开始。"""
    a = Doc("a")
    assert reciprocal_rank_fusion([[a]])[0][1] == pytest.approx(1 / 61)

    # 权重是线性乘子，不是加法
    assert reciprocal_rank_fusion([[a]], weights=[2.0])[0][1] == pytest.approx(2 / 61)


@pytest.mark.unit
def test_larger_k_flattens_ranking_gap():
    """k 越大，名次差异被压得越平 —— 这是 k 唯一的作用。"""
    a, b = Doc("a"), Doc("b")
    small = {x.id: s for x, s in reciprocal_rank_fusion([[a, b]], k=1)}
    large = {x.id: s for x, s in reciprocal_rank_fusion([[a, b]], k=1000)}
    assert small["a"] / small["b"] > large["a"] / large["b"]


@pytest.mark.unit
def test_same_id_in_two_paths_counted_once_but_scored_twice():
    """去重的是实例，不是分数：两路都排第 1 的文档拿到 2/61。"""
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = reciprocal_rank_fusion([[a, b], [Doc("a"), c]])
    assert sum(1 for x, _ in fused if x.id == "a") == 1
    scores = {x.id: s for x, s in fused}
    assert scores["a"] == pytest.approx(2 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["c"] == pytest.approx(1 / 62)
    assert scores["a"] > scores["b"] + scores["c"] - 1e-9


@pytest.mark.unit
def test_first_seen_instance_is_kept():
    """同一个 id 在两路返回不同对象时，保留先出现的那个实例。"""
    first = Doc("x", text="from-dense")
    second = Doc("x", text="from-sparse")
    fused = reciprocal_rank_fusion([[first], [second]])
    assert fused[0][0].text == "from-dense"


@pytest.mark.unit
def test_empty_and_single_path_inputs():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
    only = reciprocal_rank_fusion([[Doc("a"), Doc("b")]])
    assert len(only) == 2


@pytest.mark.unit
def test_weights_length_mismatch_raises():
    """长度不匹配必须报错 —— 静默降级会让权重悄悄失效。"""
    with pytest.raises(ValueError, match="weights"):
        reciprocal_rank_fusion([[Doc("a")], [Doc("b")]], weights=[1.0])


@pytest.mark.unit
def test_zero_and_negative_weights_are_allowed():
    """权重 0 等于屏蔽某一路（检索里常用来临时关掉稀疏路）。"""
    a, b = Doc("a"), Doc("b")
    fused = {x.id: s for x, s in reciprocal_rank_fusion([[a], [b]], weights=[1.0, 0.0])}
    assert fused["b"] == 0.0
    assert fused["a"] > 0.0


@pytest.mark.unit
def test_output_sorted_descending():
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = reciprocal_rank_fusion([[a, b, c]])
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_dedupe_by_id_keeps_first_order():
    a, b = Doc("a"), Doc("b")
    out = dedupe_by_id([a, Doc("a"), b, Doc("b"), Doc("a")])
    assert [d.id for d in out] == ["a", "b"]


@pytest.mark.unit
def test_min_max_normalize_is_for_display_only():
    assert min_max_normalize([]) == []
    assert min_max_normalize([5.0, 5.0]) == [1.0, 1.0]
    assert min_max_normalize([0.0, 10.0]) == [0.0, 1.0]
    mid = min_max_normalize([1.0, 2.0, 3.0])
    assert mid[1] == pytest.approx(0.5)
