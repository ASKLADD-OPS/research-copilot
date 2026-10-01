"""RRF 融合的纯逻辑测试。

RRF 是整条检索链路的合流点：它错了，后面重排、CRAG、溯源全部建立在错的名次上。
所以这里把论文里定义 RRF 的几条性质逐条钉死，而不是"跑一遍看着对"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.rag.fusion import (
    DEFAULT_K,
    DENSE_SOURCE,
    SPARSE_SOURCE,
    dedupe_by_id,
    min_max_normalize,
    reciprocal_rank_fusion,
    rrf_fuse,
)


@dataclass
class Doc:
    id: str
    text: str = ""
    # rrf_fuse 靠改写这个字段来标注来源，所以测试替身必须有它
    sources: list[str] = field(default_factory=list)


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


# ================================================================ rrf_fuse（两路 + 来源标记）
@pytest.mark.unit
def test_rrf_fuse_default_k_matches_paper_value():
    """k 默认值与规格一致（60），不允许悄悄改成别的常数。"""
    a = Doc("a")
    assert rrf_fuse([a], [])[0][1] == pytest.approx(1 / (60 + 1))


@pytest.mark.unit
def test_rrf_fuse_tags_source_of_each_result():
    """来源标记三态：dense / sparse / both（两路都召回）。"""
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = rrf_fuse([a, b], [b, c])
    tags = {doc.id: doc.sources for doc, _ in fused}
    assert tags["a"] == [DENSE_SOURCE]
    assert tags["c"] == [SPARSE_SOURCE]
    assert tags["b"] == [DENSE_SOURCE, SPARSE_SOURCE]


@pytest.mark.unit
def test_rrf_fuse_source_tags_are_written_onto_the_item():
    """标记写在 item 本身上（原地改写），因为融合结果要继续走完整条链路。"""
    a = Doc("a")
    fused = rrf_fuse([a], [a])
    assert fused[0][0] is a
    assert a.sources == [DENSE_SOURCE, SPARSE_SOURCE]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dense", "sparse", "expected_ids"),
    [
        ([], [], []),  # 两路都空
        ([], ["s1", "s2"], ["s1", "s2"]),  # dense 空 → 只由 sparse 贡献
        (["d1", "d2"], [], ["d1", "d2"]),  # sparse 空 → 只由 dense 贡献
    ],
)
def test_rrf_fuse_handles_empty_paths(dense, sparse, expected_ids):
    """任一路为空都不能抛异常 —— 稀疏通道缺失是**会真实发生**的降级场景。"""
    docs = {i: Doc(i) for i in {*dense, *sparse}}
    fused = rrf_fuse([docs[i] for i in dense], [docs[i] for i in sparse])
    assert [doc.id for doc, _ in fused] == expected_ids
    for doc, _ in fused:
        assert len(doc.sources) == 1  # 只有一路有结果，来源标记就只能是那一个


@pytest.mark.unit
def test_rrf_fuse_empty_result_does_not_raise():
    assert rrf_fuse([], []) == []


@pytest.mark.unit
def test_rrf_fuse_fully_overlapping_paths():
    """完全重叠（同一批文档、同一顺序）：分数翻倍，顺序不变，全部标 both。"""
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = rrf_fuse([a, b, c], [a, b, c])
    assert [doc.id for doc, _ in fused] == ["a", "b", "c"]
    scores = [s for _, s in fused]
    assert scores[0] == pytest.approx(2 / 61)
    assert scores[1] == pytest.approx(2 / 62)
    assert scores[2] == pytest.approx(2 / 63)
    assert all(doc.sources == [DENSE_SOURCE, SPARSE_SOURCE] for doc, _ in fused)


@pytest.mark.unit
def test_rrf_fuse_fully_disjoint_paths_interleaves_by_rank():
    """完全不重叠：没有两条结果共享排名贡献，顺序由名次交替决定。"""
    fused = rrf_fuse([Doc("d1"), Doc("d2"), Doc("d3")], [Doc("s1"), Doc("s2"), Doc("s3")])
    assert [doc.id for doc, _ in fused] == ["d1", "s1", "d2", "s2", "d3", "s3"]
    scores = {doc.id: s for doc, s in fused}
    assert scores["d1"] == pytest.approx(1 / 61)
    assert scores["s1"] == pytest.approx(1 / 61)
    assert scores["d3"] == pytest.approx(1 / 63)


@pytest.mark.unit
def test_rrf_fuse_partially_overlapping_only_overlap_gets_both():
    """部分重叠：只有交集被标 both，并且它的分数严格高于两侧独有项。"""
    fused = rrf_fuse([Doc("d1"), Doc("x")], [Doc("s1"), Doc("x")])
    scores = {doc.id: s for doc, s in fused}
    tags = {doc.id: doc.sources for doc, _ in fused}
    assert tags["x"] == [DENSE_SOURCE, SPARSE_SOURCE]
    assert tags["d1"] == [DENSE_SOURCE]
    assert tags["s1"] == [SPARSE_SOURCE]
    assert scores["x"] == pytest.approx(2 / 62)
    assert scores["x"] > scores["d1"] == scores["s1"]


@pytest.mark.unit
def test_rrf_fuse_agrees_with_generic_fusion():
    """rrf_fuse 只是 reciprocal_rank_fusion 的两路封装 —— 分数必须逐项一致，
    否则"带来源标记"这个便利会变成两套算法的漂移源。"""
    a, b, c = Doc("a"), Doc("b"), Doc("c")
    fused = rrf_fuse([a, b], [b, c])
    generic = reciprocal_rank_fusion([[a, b], [b, c]])
    assert [(d.id, s) for d, s in fused] == [(d.id, s) for d, s in generic]


@pytest.mark.unit
def test_rrf_fuse_same_object_in_both_paths_is_deduped():
    """同一实例在两路出现只保留一份 —— 否则下游会把同一个 chunk 当两条证据。"""
    a = Doc("a")
    fused = rrf_fuse([a], [a])
    assert len(fused) == 1
    assert fused[0][1] == pytest.approx(2 / 61)
