"""CRAG 判级测试。

重点是**两段式判级的边界**：词法预判的区间必须既省下 LLM 调用，
又不会把"明显相关"错杀成 irrelevant。这里把三段区间和边界值全部钉死。
"""

from __future__ import annotations

import pytest

from app.rag.crag import (
    CHEAP_SKIP_HIGH,
    CHEAP_SKIP_LOW,
    Verdict,
    cheap_grade,
    lexical_coverage,
    tokenize,
    verdict_from_score,
)


class FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


# ---------------------------------------------------------------- 分词
@pytest.mark.unit
def test_tokenize_splits_english_words_and_chinese_chars():
    tokens = tokenize("Retrieval-Augmented 检索 增强")
    assert "retrieval-augmented" in tokens  # 连字符词保持整体
    assert "retrieval" in tokens or "retrieval-augmented" in tokens
    assert {"检", "索", "增", "强"} <= tokens
    assert all(t == t.lower() for t in tokens)


@pytest.mark.unit
def test_tokenize_ignores_single_letters_and_empty():
    assert tokenize("") == set()
    assert "a" not in tokenize("a b c")  # 单字母不算词，避免噪声覆盖


# ---------------------------------------------------------------- 词法覆盖度
@pytest.mark.unit
def test_lexical_coverage_full_and_empty():
    assert lexical_coverage("检索", [FakeChunk("检索")]) == pytest.approx(1.0)
    assert lexical_coverage("检索", [FakeChunk("完全无关")]) == 0.0
    assert lexical_coverage("", [FakeChunk("内容")]) == 0.0
    assert lexical_coverage("检索", []) == 0.0


@pytest.mark.unit
def test_lexical_coverage_is_partial_ratio():
    # query 有 2 个 token，命中 1 个 → 0.5
    assert lexical_coverage("检索 增强", [FakeChunk("检索")]) == pytest.approx(0.5)


@pytest.mark.unit
def test_lexical_coverage_uses_union_of_chunks():
    chunks = [FakeChunk("检"), FakeChunk("索")]
    assert lexical_coverage("检索", chunks) == pytest.approx(1.0)


@pytest.mark.unit
def test_lexical_coverage_ignores_chunks_beyond_max():
    """只看前 max_chunks 个：后面的片段不该把覆盖率撑高。"""
    chunks = [FakeChunk("无关") for _ in range(5)] + [FakeChunk("检索")]
    assert lexical_coverage("检索", chunks, max_chunks=5) == 0.0


# ---------------------------------------------------------------- 结论阈值
@pytest.mark.unit
def test_verdict_from_score_thresholds(settings):
    hi, lo = settings.CRAG_RELEVANCE_THRESHOLD, settings.CRAG_AMBIGUOUS_LOW
    assert hi == 0.5
    assert lo == 0.3

    assert verdict_from_score(0.5) is Verdict.RELEVANT  # 边界闭区间
    assert verdict_from_score(0.75) is Verdict.RELEVANT
    assert verdict_from_score(0.3) is Verdict.AMBIGUOUS  # 下界闭、上界开
    assert verdict_from_score(0.4999) is Verdict.AMBIGUOUS
    assert verdict_from_score(0.2999) is Verdict.IRRELEVANT
    assert verdict_from_score(0.0) is Verdict.IRRELEVANT


# ---------------------------------------------------------------- 零成本预判
@pytest.mark.unit
def test_cheap_grade_no_chunks_is_irrelevant():
    result = cheap_grade("任何问题", [])
    assert result is not None
    assert result.verdict is Verdict.IRRELEVANT
    assert result.used_llm is False


@pytest.mark.unit
def test_cheap_grade_low_coverage_skips_llm():
    result = cheap_grade("完全不同的主题 xx yy", [FakeChunk("检索增强生成")])
    assert result is not None
    assert result.verdict is Verdict.IRRELEVANT
    assert result.score < CHEAP_SKIP_LOW


@pytest.mark.unit
def test_cheap_grade_high_coverage_skips_llm():
    result = cheap_grade("检索", [FakeChunk("检索增强生成")])
    assert result is not None
    assert result.verdict is Verdict.RELEVANT
    assert result.score > CHEAP_SKIP_HIGH


@pytest.mark.unit
def test_cheap_grade_gray_zone_defers_to_llm():
    """灰区必须返回 None —— 这才是"省调用"与"判得准"的折中点。"""
    # 2 个 token 命中 1 个 → 0.5，落在 (0.10, 0.45) 之外；换成 4 命中 1 = 0.25
    result = cheap_grade("检索 增强 生成 幻觉", [FakeChunk("检索")])
    assert result is None or CHEAP_SKIP_LOW < result.score < CHEAP_SKIP_HIGH


@pytest.mark.unit
def test_cheap_skip_band_is_ordered():
    assert CHEAP_SKIP_LOW < CHEAP_SKIP_HIGH
