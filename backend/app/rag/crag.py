"""CRAG（Corrective RAG）三级降级。

    relevant   → 直接生成
    ambiguous  → Query Rewrite 后重检索（最多 3 次）
    irrelevant → Web Search 兜底

判级为什么要两段
----------------
每次都用 LLM 判级太贵（每轮问答多一次调用），而且 LLM 对"完全跑题"这种粗判断并不可靠。
所以先用**词法覆盖度**做零成本预判：
- 覆盖度极低 → 直接判 irrelevant，省掉一次 LLM 调用；
- 覆盖度很高 → 直接判 relevant；
- 落在中间灰区 → 才交给 LLM 精判（此时才需要区分"部分相关"与"相关"）。

这个顺序让绝大多数请求只花一次生成调用。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.errors import EmptyRetrievalError
from app.core.logging import logger

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{1,}|[\u4e00-\u9fff]")

# 零成本预判的两个阈值（与 CRAG_* 配置独立：那是"判级结论"的阈值，这是"要不要问 LLM"的阈值）
CHEAP_SKIP_LOW = 0.10
CHEAP_SKIP_HIGH = 0.45


class Verdict(StrEnum):
    RELEVANT = "relevant"
    AMBIGUOUS = "ambiguous"
    IRRELEVANT = "irrelevant"


@dataclass(slots=True)
class GradeResult:
    verdict: Verdict
    score: float
    rationale: str = ""
    used_llm: bool = False


@dataclass(slots=True)
class CRAGResult:
    chunks: list  # list[RetrievedChunk]，避免循环导入所以不标类型
    verdict: Verdict
    rewrites: list[str] = field(default_factory=list)
    used_web_fallback: bool = False
    rounds: int = 0
    rationale: str = ""


class _LLMGrade(BaseModel):
    """LLM 精判的输出 schema。"""

    relevance: float = Field(ge=0.0, le=1.0, description="检索内容对回答该问题的有用程度")
    reason: str = Field(default="", description="一句话理由")


def tokenize(text: str) -> set[str]:
    """极简分词：英文按词、中文按字。用于词法覆盖度，不追求语言学正确。"""
    return {t.lower() for t in _WORD_RE.findall(text or "")}


def lexical_coverage(query: str, chunks: Sequence[object], *, max_chunks: int = 5) -> float:
    """query 的词有多大比例出现在检索结果里。

    这是 recall 的下界代理指标 —— 注意它衡量的是"问题里的词有没有落进语料"，
    不是"答案对不对"。所以只用来筛掉明显不相关，不用来判相关。
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return 0.0
    corpus: set[str] = set()
    for c in chunks[:max_chunks]:
        corpus |= tokenize(getattr(c, "content", "") or "")
    if not corpus:
        return 0.0
    return len(q_tokens & corpus) / len(q_tokens)


def verdict_from_score(score: float) -> Verdict:
    if score >= settings.CRAG_RELEVANCE_THRESHOLD:
        return Verdict.RELEVANT
    if score >= settings.CRAG_AMBIGUOUS_LOW:
        return Verdict.AMBIGUOUS
    return Verdict.IRRELEVANT


def cheap_grade(query: str, chunks: Sequence[object]) -> GradeResult | None:
    """灰区返回 None 表示"需要 LLM 精判"。"""
    if not chunks:
        return GradeResult(Verdict.IRRELEVANT, 0.0, "无检索结果")
    score = lexical_coverage(query, chunks)
    if score < CHEAP_SKIP_LOW:
        return GradeResult(Verdict.IRRELEVANT, score, f"问题关键词覆盖率仅 {score:.0%}，判定跑题")
    if score > CHEAP_SKIP_HIGH:
        return GradeResult(Verdict.RELEVANT, score, f"问题关键词覆盖率 {score:.0%}，直接采信")
    return None


async def llm_grade(query: str, chunks: Sequence[object]) -> GradeResult:
    from app.llm.client import Role
    from app.llm.structured import complete_structured

    excerpt = "\n---\n".join((getattr(c, "content", "") or "")[:600] for c in chunks[:5])
    prompt = (
        "判断下面【检索内容】对回答【问题】的有用程度，给出 0~1 的分数。\n"
        "打分口径：1.0 = 直接包含答案依据；0.5 = 部分相关、需要推理;0.0 = 完全无关。\n\n"
        f"【问题】\n{query}\n\n【检索内容】\n{excerpt}"
    )
    out = await complete_structured(_LLMGrade, [{"role": "user", "content": prompt}], role=Role.UTILITY)
    return GradeResult(verdict_from_score(out.relevance), out.relevance, out.reason, used_llm=True)


async def grade(query: str, chunks: Sequence[object], *, force_llm: bool = False) -> GradeResult:
    """判级入口：先零成本预判，灰区才走 LLM；LLM 失败则退回词法分。"""
    if not settings.CRAG_ENABLED:
        return GradeResult(Verdict.RELEVANT, 1.0, "CRAG 已关闭")

    if not force_llm:
        cheap = cheap_grade(query, chunks)
        if cheap is not None:
            return cheap

    try:
        return await llm_grade(query, chunks)
    except Exception as exc:  # noqa: BLE001 —— 判级失败不该中断问答
        logger.warning("LLM 判级失败，退回词法分：{}", exc)
        score = lexical_coverage(query, chunks)
        return GradeResult(verdict_from_score(score), score, f"LLM 不可用，词法覆盖度 {score:.0%}")


class CorrectiveRAG:
    """三级降级驱动器。

    依赖注入三个回调，而不是直接 import 检索/改写/搜索 —— 便于单测注入假实现，
    也避免 rag 层与 agents 层互相 import 成环。
    """

    def __init__(
        self,
        *,
        retrieve: Callable[[str], Awaitable[list]],
        rewrite: Callable[[str], Awaitable[str]] | None = None,
        web_search: Callable[[str], Awaitable[list]] | None = None,
    ) -> None:
        self._retrieve = retrieve
        self._rewrite = rewrite
        self._web_search = web_search

    async def run(self, query: str, initial: list | None = None) -> CRAGResult:
        chunks = list(initial or [])
        rewrites: list[str] = []
        rounds = 0

        if not chunks:
            chunks = list(await self._retrieve(query))

        result = await grade(query, chunks)

        # ---- 第 2 级：ambiguous → 查询改写后重检索 ----
        current_query = query
        while result.verdict is Verdict.AMBIGUOUS and self._rewrite and rounds < settings.CRAG_REWRITE_MAX_RETRY:
            rounds += 1
            current_query = await self._rewrite(current_query)
            rewrites.append(current_query)
            logger.info("CRAG 第 {} 轮改写: {}", rounds, current_query[:80])
            new_chunks = list(await self._retrieve(current_query))
            if new_chunks:
                chunks = new_chunks
            result = await grade(current_query, chunks)

        # ---- 第 3 级：irrelevant → Web Search 兜底 ----
        if result.verdict is Verdict.IRRELEVANT and self._web_search:
            logger.info("CRAG 判定跑题，启用 Web Search 兜底")
            web_chunks = list(await self._web_search(current_query))
            if web_chunks:
                return CRAGResult(
                    chunks=web_chunks,
                    verdict=Verdict.AMBIGUOUS,  # 兜底结果只当"参考"，不当"证据"
                    rewrites=rewrites,
                    used_web_fallback=True,
                    rounds=rounds,
                    rationale="本地语料不相关，已回退到 Web 检索，答案需标注外部来源",
                )

        if result.verdict is Verdict.IRRELEVANT:
            raise EmptyRetrievalError("本地语料与问题不相关，且未配置可用的 Web Search 兜底")

        return CRAGResult(
            chunks=chunks,
            verdict=result.verdict,
            rewrites=rewrites,
            rounds=rounds,
            rationale=result.rationale,
        )
