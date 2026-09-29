"""溯源引擎测试 —— 抗幻觉的四道检查各自能拦住什么。

这里的每条用例都对应一个**真实会发生的失败模式**，不是凑覆盖率：
编号越界、数字被改、无据声明、低有据率硬答。
"""

from __future__ import annotations

import pytest

from app.core.errors import GroundingError
from app.rag.source_tracing import (
    SourceTracer,
    extract_markers,
    lexical_entailment,
    numbers_consistent,
    numbers_in,
    split_sentences,
    strip_markers,
)


# ---------------------------------------------------------------- 引用编号抽取
@pytest.mark.unit
def test_extract_single_marker():
    assert extract_markers("结论成立 [1]。") == [1]


@pytest.mark.unit
def test_extract_comma_list_and_chinese_brackets():
    assert extract_markers("见 [2,3] 与 【5】") == [2, 3, 5]
    assert extract_markers("见 [2，3]") == [2, 3]  # 中文逗号也要认


@pytest.mark.unit
def test_extract_range_expands():
    assert extract_markers("[5-7]") == [5, 6, 7]
    assert extract_markers("[3–5]") == [3, 4, 5]  # en dash


@pytest.mark.unit
def test_extract_absurd_range_is_ignored():
    """防御离谱区间：LLM 偶尔会吐 [1-9999]，不能因此把内存吃光。"""
    assert extract_markers("[1-9999]") == []


@pytest.mark.unit
def test_extract_preserves_order_and_duplicates():
    assert extract_markers("[3] 然后 [1] 然后 [3]") == [3, 1, 3]


@pytest.mark.unit
def test_extract_no_markers():
    assert extract_markers("这句话没有任何引用") == []
    assert extract_markers("") == []


@pytest.mark.unit
def test_strip_markers_removes_all_forms():
    assert strip_markers("结论 [1,2] 成立【3】") == "结论  成立"
    assert strip_markers("") == ""


# ---------------------------------------------------------------- 切句
@pytest.mark.unit
def test_split_sentences_chinese_and_english():
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    out = split_sentences("First point. Second point.")
    assert len(out) == 2


@pytest.mark.unit
def test_split_sentences_empty():
    assert split_sentences("") == []
    assert split_sentences("   ") == []


# ---------------------------------------------------------------- 数字硬规则
@pytest.mark.unit
def test_numbers_in_extracts_values_and_percent():
    assert numbers_in("提升了 12% 与 3.5 个点") == {"12%", "3.5"}


@pytest.mark.unit
def test_numbers_consistent_rejects_changed_number():
    """这正是数字要单列硬规则的原因：词法覆盖几乎一样，结论却相反。"""
    assert numbers_consistent("提升了 12%", "实验显示提升了 21%") is False
    assert numbers_consistent("提升了 12%", "实验显示提升了 12%") is True


@pytest.mark.unit
def test_numbers_consistent_passes_when_claim_has_no_number():
    assert numbers_consistent("方法有效", "任何证据") is True


# ---------------------------------------------------------------- 词法蕴含
@pytest.mark.unit
def test_lexical_entailment_is_coverage_ratio():
    assert lexical_entailment("该方法有效", "该方法有效") == pytest.approx(1.0)
    assert lexical_entailment("", "任何") == 0.0
    assert lexical_entailment("该方法有效", "") == 0.0


@pytest.mark.unit
def test_lexical_entailment_partial():
    score = lexical_entailment("该方法有效", "该")
    assert 0.0 < score < 1.0


# ---------------------------------------------------------------- 整体溯源
@pytest.mark.unit
def test_trace_without_chunks_flags_everything_uncited(tracer, make_chunk):
    report = tracer.trace("第一句。第二句。", [])
    assert report.sentences_total == 2
    assert report.sentences_supported == 0
    assert len(report.uncited_claims) == 2
    assert report.grounding_ratio == 0.0


@pytest.mark.unit
def test_trace_detects_phantom_marker(tracer, make_chunk):
    """幻觉引用：正文写了 [9]，但上下文只有 1 块 —— 最便宜也最有效的检查。"""
    chunks = [make_chunk("c1", "只有一块证据。")]
    report = tracer.trace("结论由 [9] 支持。", chunks)
    assert report.phantom_markers == [9]
    assert report.passed is False


@pytest.mark.unit
def test_trace_supported_sentence_passes(tracer, make_chunk):
    evidence = "该方法在三个数据集上平均提升了 12%，超过基线。"
    chunks = [make_chunk("c1", evidence)]
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", chunks)
    assert report.phantom_markers == []
    assert report.sentences_supported == 1
    assert report.grounding_ratio == pytest.approx(1.0)
    assert report.passed is True
    assert report.citations[0].marker == 1
    assert report.citations[0].supported is True
    assert report.citations[0].chunk_id == "c1"


@pytest.mark.unit
def test_trace_number_mismatch_blocks_support(tracer, make_chunk):
    """数字对不上：覆盖度满分也不算有据。"""
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 21%，超过基线。")]
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", chunks)
    assert report.sentences_supported == 0
    assert report.number_mismatches
    assert report.grounding_ratio == 0.0


@pytest.mark.unit
def test_trace_uncited_claim_recorded(tracer, make_chunk):
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    answer = "该方法在三个数据集上平均提升了 12% [1]。此外我认为作者可能没做过消融实验。"
    report = tracer.trace(answer, chunks)
    assert report.sentences_supported == 1
    assert report.sentences_total == 2
    assert report.grounding_ratio == pytest.approx(0.5)
    assert any("消融" in c for c in report.uncited_claims)


@pytest.mark.unit
def test_trace_ignores_short_connective_sentences(tracer, make_chunk):
    """「综上所述。」这种短衔接句不该被记成无据声明，否则无据清单会被它刷屏。

    注意它**仍计入分母**（sentences_total）—— 这是刻意的：衔接句也是模型
    自己写出来的一个字，把它从分母里摘掉等于给有据率"注水"。所以这里
    ratio = 1/2 而不是 1.0。
    """
    chunks = [make_chunk("c1", "该方法有效。")]
    report = tracer.trace("综上所述。该方法有效 [1]。", chunks)
    assert report.uncited_claims == []
    assert report.sentences_total == 2
    assert report.grounding_ratio == pytest.approx(0.5)


@pytest.mark.unit
def test_trace_keeps_one_citation_per_marker(tracer, make_chunk):
    """同一编号被多句引用时，只产出**一条** Citation（取最高分），
    否则前端引用列表会出现一堆重复项。"""
    good = "该方法在三个数据集上平均提升了 12%。"
    chunks = [make_chunk("c1", "完全无关的另一段文字。"), make_chunk("c2", good)]
    report = tracer.trace("第一句 [1]。[2] 第二句也该被支持。", chunks)
    assert [c.marker for c in report.citations] == [1, 2]
    assert len(report.citations) == len({c.marker for c in report.citations})


@pytest.mark.unit
def test_trace_ratio_denominator_is_at_least_one(tracer, make_chunk):
    """空答案不能让 ratio 变成 ZeroDivisionError。"""
    report = tracer.trace("", [make_chunk("c1", "内容")])
    assert report.sentences_total == 1
    assert report.grounding_ratio == 0.0


# ---------------------------------------------------------------- 闸门
@pytest.mark.unit
def test_enforce_raises_on_phantom(tracer, make_chunk):
    report = tracer.trace("结论 [7]。", [make_chunk("c1", "证据")])
    with pytest.raises(GroundingError, match="幻觉引用编号"):
        tracer.enforce(report)


@pytest.mark.unit
def test_enforce_raises_below_threshold(tracer, make_chunk):
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。另外还有一句完全无据的话在这里。", chunks)
    assert report.grounding_ratio < 0.8
    with pytest.raises(GroundingError, match="溯源率"):
        tracer.enforce(report)


@pytest.mark.unit
def test_enforce_passes_when_clean(tracer, make_chunk):
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", chunks)
    tracer.enforce(report)  # 不抛异常即为通过


@pytest.mark.unit
def test_threshold_comes_from_settings(tracer, settings):
    """阈值不硬编码在代码里，改配置就能调松紧。"""
    assert tracer.nli_threshold == 0.6
    assert settings.GROUNDING_MIN_RATIO == 0.8


@pytest.mark.unit
def test_report_to_dict_is_json_safe(make_chunk):
    chunks = [make_chunk("c1", "该方法有效。")]
    report = SourceTracer().trace("该方法有效 [1]。", chunks)
    payload = report.to_dict()
    import json

    json.dumps(payload)  # 不能有不可序列化的对象
    assert set(payload) >= {"grounding_ratio", "phantom_markers", "citations", "passed"}
