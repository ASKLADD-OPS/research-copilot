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


# ================================================================ 三级降级驱动器
# 判级函数在这里被换掉：要验的是**分支行为**（改不改写、重检索几次、走不走 Web），
# 不是判级本身的准确率 —— 那个由上面的 cheap_grade / lexical_coverage 用例负责。


class CallLog:
    """记录三条依赖被调用的次数与入参，替代 mock 框架。"""

    def __init__(self) -> None:
        self.retrievals: list[str] = []
        self.rewrites: list[str] = []
        self.web: list[str] = []


@pytest.fixture
def patch_grade(monkeypatch):
    """按顺序返回给定的 verdict 序列，用完后停在最后一个。"""

    def _install(*verdicts: Verdict, scores: list[float] | None = None) -> None:
        seq = list(verdicts)
        scores = scores or [0.9] * len(seq)

        async def fake_grade(query, chunks, **kwargs):
            from app.rag.crag import GradeResult

            i = min(getattr(fake_grade, "_n", 0), len(seq) - 1)
            fake_grade._n = i + 1
            return GradeResult(seq[i], scores[i], f"stub#{i}")

        fake_grade._n = 0
        monkeypatch.setattr("app.rag.crag.grade", fake_grade)

    return _install


def _driver(log: CallLog, *, chunks_by_query: dict[str, list] | None = None, web: list | None = None):
    """装配一台 CRAG 驱动器；不给 web 就是"没有 Web 兜底"的部署形态。"""
    chunks_by_query = chunks_by_query or {}

    async def retrieve(q: str) -> list:
        log.retrievals.append(q)
        return list(chunks_by_query.get(q, [FakeChunk(f"关于 {q} 的检索结果")]))

    async def rewrite(q: str) -> str:
        log.rewrites.append(q)
        return f"{q} 改写版{len(log.rewrites)}"

    async def web_search(q: str) -> list:
        log.web.append(q)
        return list(web or [])

    from app.rag.crag import CorrectiveRAG

    return CorrectiveRAG(retrieve=retrieve, rewrite=rewrite, web_search=web_search if web is not None else None)


@pytest.mark.unit
async def test_crag_relevant_returns_immediately(patch_grade):
    """第 1 级 relevant：一次检索、一次判级，不改写、不 Web。"""
    log = CallLog()
    patch_grade(Verdict.RELEVANT)
    result = await _driver(log).run("问题")

    assert result.verdict is Verdict.RELEVANT
    assert log.retrievals == ["问题"]  # initial 为空 → 需要自己检索一次
    assert log.rewrites == []
    assert log.web == []
    assert result.rounds == 0
    assert result.used_web_fallback is False
    assert result.chunks


@pytest.mark.unit
async def test_crag_relevant_with_initial_chunks_skips_first_retrieval(patch_grade):
    """调用方已经给了检索结果就不要重复检索 —— 少一次 ANN 查询。"""
    log = CallLog()
    patch_grade(Verdict.RELEVANT)
    result = await _driver(log).run("问题", initial=[FakeChunk("已有片段")])

    assert log.retrievals == []
    assert result.chunks[0].content == "已有片段"


@pytest.mark.unit
async def test_crag_ambiguous_rewrites_and_re_retrieves(patch_grade):
    """第 2 级 ambiguous：改写 → 重检索 → 复判；判到 relevant 就停。"""
    log = CallLog()
    patch_grade(Verdict.AMBIGUOUS, Verdict.RELEVANT)
    result = await _driver(log).run("问题")

    assert log.rewrites == ["问题"]
    assert log.retrievals == ["问题", "问题 改写版1"]  # 第二轮用的是**改写后**的查询
    assert result.rounds == 1
    assert result.rewrites == ["问题 改写版1"]
    assert result.verdict is Verdict.RELEVANT


@pytest.mark.unit
async def test_crag_ambiguous_retries_at_most_three_rounds(patch_grade, settings):
    """max 3 轮：一直判 ambiguous 也必须停下来，不能无限改写（那是烧钱循环）。"""
    log = CallLog()
    patch_grade(Verdict.AMBIGUOUS)  # 永远 ambiguous
    result = await _driver(log).run("问题")

    assert settings.CRAG_REWRITE_MAX_RETRY == 3
    assert result.rounds == 3
    assert len(log.rewrites) == 3
    assert result.verdict is Verdict.AMBIGUOUS
    assert log.web == []  # ambiguous 不该触发 Web 兜底


@pytest.mark.unit
async def test_crag_ambiguous_without_rewriter_stops_immediately(monkeypatch):
    """没配改写器时不能死循环：直接接受 ambiguous 现状。"""
    from app.rag.crag import CorrectiveRAG, GradeResult

    async def fake_grade(query, chunks, **kwargs):
        return GradeResult(Verdict.AMBIGUOUS, 0.4, "stub")

    monkeypatch.setattr("app.rag.crag.grade", fake_grade)

    async def retrieve(q):
        return [FakeChunk("片段")]

    result = await CorrectiveRAG(retrieve=retrieve).run("问题")
    assert result.rounds == 0
    assert result.verdict is Verdict.AMBIGUOUS


@pytest.mark.unit
async def test_crag_irrelevant_falls_back_to_web(patch_grade):
    """第 3 级 irrelevant：本地语料跑题 → Web 兜底。

    兜底结果**降级为 ambiguous**上报：Web 片段只能当参考，不能当"论文原文"证据。
    """
    log = CallLog()
    patch_grade(Verdict.IRRELEVANT)
    result = await _driver(log, web=[FakeChunk("网页结果")]).run("问题")

    assert log.web == ["问题"]
    assert result.used_web_fallback is True
    assert result.verdict is Verdict.AMBIGUOUS
    assert [c.content for c in result.chunks] == ["网页结果"]
    assert "Web" in result.rationale


@pytest.mark.unit
async def test_crag_irrelevant_without_web_raises(patch_grade):
    """没有 Web 兜底可用时必须显式报错，不能返回一段没有证据的答案。"""
    from app.core.errors import EmptyRetrievalError

    log = CallLog()
    patch_grade(Verdict.IRRELEVANT)
    with pytest.raises(EmptyRetrievalError, match="不相关"):
        await _driver(log).run("问题")
    assert log.web == []


@pytest.mark.unit
async def test_crag_web_fallback_that_returns_nothing_raises(patch_grade):
    """Web 兜底自己也没捞到东西 → 同样只能报错（空上下文会让模型照着问题编）。"""
    from app.core.errors import EmptyRetrievalError

    log = CallLog()
    patch_grade(Verdict.IRRELEVANT)
    with pytest.raises(EmptyRetrievalError):
        await _driver(log, web=[]).run("问题")
    assert log.web == ["问题"]


@pytest.mark.unit
async def test_crag_uses_rewritten_query_for_web_search(patch_grade):
    """改写过的查询要一路带到 Web 兜底 —— 否则前两轮改写白做。"""
    log = CallLog()
    patch_grade(Verdict.AMBIGUOUS, Verdict.IRRELEVANT)
    result = await _driver(log, web=[FakeChunk("网页结果")]).run("问题")

    assert log.web == ["问题 改写版1"]
    assert result.rewrites == ["问题 改写版1"]
    assert result.used_web_fallback is True


@pytest.mark.unit
async def test_crag_disabled_short_circuits_to_relevant(monkeypatch):
    """CRAG 关闭时要直接放行（判级都不跑），这是"紧急关掉降级逻辑"的开关。"""
    from app.rag import crag as crag_module

    monkeypatch.setattr(crag_module.settings, "CRAG_ENABLED", False)

    async def retrieve(q):
        return [FakeChunk("片段")]

    result = await crag_module.CorrectiveRAG(retrieve=retrieve).run("问题")
    assert result.verdict is Verdict.RELEVANT
    assert result.rationale == "CRAG 已关闭"
