"""溯源引擎测试 —— 抗幻觉的四道检查各自能拦住什么。

这里的每条用例都对应一个**真实会发生的失败模式**，不是凑覆盖率：
编号越界、数字被改、无据声明、低有据率硬答。
"""

from __future__ import annotations

import pytest

from app.core.errors import GroundingError
from app.rag.source_tracing import (
    SourceTracer,
    content_terms,
    extract_markers,
    lexical_entailment,
    numbers_consistent,
    numbers_in,
    sentence_spans,
    split_sentences,
    strip_markers,
    term_grounding_ratio,
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


@pytest.mark.unit
def test_sentence_spans_align_with_split_sentences():
    """切句与字符区间必须来自同一次切分 —— 前端高亮靠区间，溯源靠文本。"""
    text = "第一句。第二句！第三句？"
    spans = sentence_spans(text)
    assert [s for _, _, s in spans] == split_sentences(text)
    assert [text[a:b] for a, b, _ in spans] == split_sentences(text)
    assert spans[0][0] == 0 and spans[-1][1] <= len(text)


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
    # 有据率按**实词**加权：只有第一句的实词算被支持，所以一定落在 (0,1) 开区间
    assert 0.0 < report.grounding_ratio < 1.0
    assert report.terms_supported > 0
    assert report.terms_supported < report.terms_total
    assert any("消融" in c for c in report.uncited_claims)


@pytest.mark.unit
def test_trace_ignores_short_connective_sentences(tracer, make_chunk):
    """短衔接句不该被记成无据声明，否则无据清单会被它刷屏。

    但它**仍计入分母**（句级的 `sentences_total` 与实词级的 `terms_total` 都是）——
    衔接句也是模型自己写出来的字，把它从分母里摘掉等于给有据率"注水"。
    改用实词加权后，短句的分量小于一整句长声明，所以这里 ratio 会高于句级的 1/2。
    """
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    report = tracer.trace("综上。该方法在三个数据集上平均提升了 12% [1]。", chunks)
    assert report.uncited_claims == []
    assert report.sentences_total == 2
    assert report.grounding_ratio > 0.5
    assert report.grounding_ratio < 1.0
    assert report.terms_supported < report.terms_total


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
    report = SourceTracer(enable_nli=False).trace("该方法有效 [1]。", chunks)
    payload = report.to_dict()
    import json

    json.dumps(payload)  # 不能有不可序列化的对象
    assert set(payload) >= {"grounding_ratio", "phantom_markers", "citations", "passed"}


@pytest.mark.unit
def test_unit_fixture_never_attempts_nli_download(tracer):
    """单测里绝不能去拉 cross-encoder 权重。

    这个断言看着像在测实现细节，其实是在挡一类真实事故：
    `sentence-transformers` 一旦装上，`SourceTracer()` 的懒加载就会真发
    HuggingFace 请求；huggingface_hub 默认**没有下载超时**，网络半通时
    不抛错而是挂住，表现为"单测跑到某个用例就不动了"，且很难和死锁区分。
    """
    assert tracer._nli_checked is True
    assert tracer._nli is None


# ================================================================ 实词级 Grounding Ratio
@pytest.mark.unit
def test_content_terms_drops_stopwords_and_punctuation():
    """实词 = 去掉停用词、标点、数字的 token。数字由 numbers_consistent 单独把关。"""
    assert content_terms("这个方法 in the paper") == {"方", "法", "paper"}
    assert content_terms("12% 的[1]标点。") == {"标", "点"}
    assert content_terms("") == set()


@pytest.mark.unit
def test_term_grounding_ratio_is_ratio_of_supported_terms():
    """分子是"被支持句子的实词数"，分母是"全部实词数"。

    期望值由 `content_terms` 现算再加总，避免把停用词表的变化抄进断言里
    （停用词表本身由 `test_content_terms_drops_stopwords_and_punctuation` 盯着）。
    """
    supported, unsupported = "方法有效效果显著", "作者没做实验"
    good, bad = len(content_terms(supported)), len(content_terms(unsupported))
    assert good > 0 and bad > 0  # 两句都得有实词，否则这条用例验不到加权

    ratio = term_grounding_ratio([(supported, True), (unsupported, False)])
    assert ratio == pytest.approx(good / (good + bad))
    assert ratio == pytest.approx(0.6)  # 6 / (6 + 4)
    assert 0.0 < ratio < 1.0


@pytest.mark.unit
def test_term_grounding_ratio_edges():
    """空输入与"整段都是虚词"都返回 0.0 —— 什么都没说时不能报告"完全有据"。"""
    assert term_grounding_ratio([]) == 0.0
    assert term_grounding_ratio([("的了是在", False)]) == 0.0
    assert term_grounding_ratio([("方法", True)]) == 1.0


@pytest.mark.unit
def test_grounding_ratio_weights_long_sentences_more(tracer, make_chunk):
    """**为什么按实词而不是按句**：一句 40 字的无据长句，比一句 5 字的短句
    更该把有据率拉下来。按句计权时两者是同一份分母，看不出来。"""
    chunks = [make_chunk("c1", "该方法有效。")]

    short_noise = tracer.trace("综上。该方法有效 [1]。", chunks)
    long_noise = tracer.trace(
        "此外作者在附录里声称该结论可以推广到全部语言并且不需要任何额外训练数据。该方法有效 [1]。",
        chunks,
    )
    assert long_noise.grounding_ratio < short_noise.grounding_ratio
    assert long_noise.terms_total > short_noise.terms_total


# ================================================================ 溯源契约（SourceTrace）
@pytest.mark.unit
def test_source_trace_carries_every_contract_field(tracer, make_chunk):
    """规格要求的七个字段必须齐全：answer_span / chunk_id / paper_id / page / bbox /
    confidence / attribution_method —— 少一个前端就有一块面板没数据。"""
    import json

    chunk = make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")
    chunk.bbox = [72.0, 120.0, 480.0, 143.0]
    chunk.page_start = 4
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", [chunk])

    assert len(report.citations) == 1
    trace = report.source_traces()[0]
    assert trace.answer_span.startswith("该方法在三个数据集")
    assert "12%" in trace.answer_span
    assert trace.chunk_id == "c1"
    assert trace.paper_id == "p1"
    assert trace.page == 4
    assert trace.bbox == [72.0, 120.0, 480.0, 143.0]
    assert 0.0 < trace.confidence <= 1.0
    assert trace.attribution_method in {"self_citation", "nli", "hybrid"}

    payload = trace.to_dict()
    assert set(payload) == {
        "answer_span",
        "chunk_id",
        "paper_id",
        "page",
        "bbox",
        "confidence",
        "attribution_method",
    }
    json.dumps(report.to_dict())  # 整个报告必须可 JSON 序列化
    assert report.to_dict()["sources"] == [payload]


@pytest.mark.unit
def test_char_span_points_at_the_citing_sentence(tracer, make_chunk):
    """字符区间要能原样切回那句话 —— 前端靠它给答案高亮。"""
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    answer = "该方法在三个数据集上平均提升了 12% [1]。此外我认为作者可能没做过消融实验。"
    citation = tracer.trace(answer, chunks).citations[0]

    assert citation.char_span is not None
    start, end = citation.char_span
    assert answer[start:end].startswith("该方法在三个数据集")
    assert "[1]" in answer[start:end]
    assert answer[start:end] not in ("", answer)  # 不是整篇、也不是空


@pytest.mark.unit
def test_attribution_method_is_hybrid_without_nli_model(tracer, make_chunk):
    """没有 NLI 模型时，被支持的引用经"白名单 + 词法蕴含 + 数字硬规则"三重认定 → hybrid。"""
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    report = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", chunks)
    assert report.citations[0].supported is True
    assert report.citations[0].attribution_method == "hybrid"


@pytest.mark.unit
def test_attribution_method_is_nli_when_model_is_available(make_chunk):
    """装上真 NLI 模型后，认定方式要升级为 nli（前端据此显示更强的一档）。"""

    class FakeNLI:
        def predict(self, pairs, apply_softmax=True):
            return [[0.02, 0.96, 0.02] for _ in pairs]

    tracer = SourceTracer(enable_nli=False)
    tracer._nli = FakeNLI()  # 注入假模型，绝不真去下权重
    report = tracer.trace(
        "该方法在三个数据集上平均提升了 12% [1]。", [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    )
    assert report.citations[0].supported is True
    assert report.citations[0].attribution_method == "nli"
    assert report.citations[0].confidence == pytest.approx(0.96)


@pytest.mark.unit
def test_attribution_method_is_self_citation_when_claim_unsupported(tracer, make_chunk):
    """编号合法但语义上没被支持 → 只能标 self_citation（前端要把它标灰）。"""
    chunks = [make_chunk("c1", "本文研究的是图像分割。")]
    report = tracer.trace("作者证明了存在外星文明 [1]。", chunks)
    assert report.phantom_markers == []  # 编号没越界
    assert report.citations[0].supported is False
    assert report.citations[0].attribution_method == "self_citation"


# ================================================================ 注入幻觉：必须被检出
@pytest.mark.unit
def test_injected_hallucination_is_detected_and_lowers_grounding(tracer, make_chunk):
    """验收 3：故意注入幻觉 → 被检出 + grounding_ratio 下降。

    注入两处，覆盖两种最典型的幻觉：
    ① 越界引用编号（编造参考文献）；
    ② 无据声明（结论在语料里根本没有依据）。
    """
    chunks = [make_chunk("c1", "该方法在三个数据集上平均提升了 12%。")]
    clean = tracer.trace("该方法在三个数据集上平均提升了 12% [1]。", chunks)

    hallucinated = tracer.trace(
        "该方法在三个数据集上平均提升了 12% [1]。另外我们在 ImageNet 上把准确率提升到了 99.9%，详见 [7]。",
        chunks,
    )

    assert hallucinated.phantom_markers == [7]  # ① 越界编号被检出
    assert not hallucinated.passed
    assert hallucinated.grounding_ratio < clean.grounding_ratio  # 阈值门真的下降
    assert hallucinated.grounding_ratio < 0.8  # 掉到高风险区间
    assert hallucinated.unsupported_claims or hallucinated.uncited_claims  # ② 无据声明被记录

    with pytest.raises(GroundingError, match="幻觉引用编号"):
        tracer.enforce(hallucinated)


@pytest.mark.unit
def test_nli_contradiction_is_not_support(make_chunk):
    """NLI 判定为 contradiction 时算幻觉：entailment 概率低 → 不支持。"""

    class ContradictionNLI:
        def predict(self, pairs, apply_softmax=True):
            return [[0.9, 0.05, 0.05] for _ in pairs]  # [contradiction, entailment, neutral]

    tracer = SourceTracer(enable_nli=False)
    tracer._nli = ContradictionNLI()
    report = tracer.trace("该方法没有任何提升 [1]。", [make_chunk("c1", "该方法平均提升了 12%。")])

    assert report.citations[0].supported is False
    assert report.citations[0].confidence == pytest.approx(0.05)
    assert report.grounding_ratio == 0.0
    assert not report.passed


@pytest.mark.unit
def test_nli_neutral_is_not_support(make_chunk):
    """neutral（无法验证）同样不算支持 —— "无法验证"不等于"有据"。"""

    class NeutralNLI:
        def predict(self, pairs, apply_softmax=True):
            return [[0.1, 0.2, 0.7] for _ in pairs]

    tracer = SourceTracer(enable_nli=False)
    tracer._nli = NeutralNLI()
    report = tracer.trace("该方法在某个数据集上有效 [1]。", [make_chunk("c1", "本文研究图像分割。")])
    assert report.citations[0].supported is False
    assert report.grounding_ratio == 0.0
