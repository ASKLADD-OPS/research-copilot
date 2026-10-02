"""论文框架生成 + 段落扩写。

Idea → 大纲 → 逐节有据草稿，链路是固定的四步：

1. **Planner**（`Role.PLANNER`）把 Idea 拆成章节结构（Abstract / Introduction /
   Related Work / Method / Experiment / Conclusion，也接受自定义章节）。
2. **每节检索**：拿「Idea + 本节标题 + 本节要写什么」去**本地论文库**做混合检索。
   只检索库内论文这一点就是"引用约束"的第一层 —— 检索器只认 `papers` / `chunks`，
   它没有"从网上编一篇出来"的能力。
3. **Self-Citation 起草**：把检索块按 `[n]` 编号交给模型（`to_context_block` 的编号
   就是白名单），要求它只用这些材料、并**在句末标注来源编号**。
4. **全局重编号**：各节是分别起草的，`[1]` 在 Method 里和 Introduction 里指的不是同一篇。
   所以起草完必须把"局部编号 → 全篇编号"重排一次，否则拼起来的稿子引用全乱套。
   同一块证据在多节被引用时共用同一个编号 —— 这正是参考文献表该有的样子。

还有一条闸门在 `_phantoms`：模型编出来的编号（不在检索上下文里的）**在返回前就被摘掉**，
不是等前端去标灰。规格要求"所有引用必须来自已上传文献或检索结果"，那就不该有任何一个
越界编号活着走出这个模块。

为什么不用 `write_section` 那个现成的起草工具
---------------------------------------------
它的提示词里写着"不要编造引用编号"，但**没有要求标注来源**。直接复用它产出的是一段
没有 `[n]` 的漂亮散文 —— 恰好绕过了整个抗幻觉机制（验收第 2 条要的是"正文生成含真实引用"）。
所以这里自带一份提示词，把"标引用"写成硬要求。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role, get_llm, usage_delta, usage_snapshot
from app.llm.structured import complete_structured
from app.rag.retriever import HybridRetriever, RetrievedChunk, to_context_block
from app.rag.source_tracing import TraceReport, extract_markers, get_tracer
from app.schemas import (
    CitationOut,
    ExpandRequest,
    ExpandResult,
    OutlineRequest,
    OutlineResult,
    OutlineSection,
)
from app.writing.references import (
    drop_markers,
    load_paper_meta,
    references_for_chunks,
    remap_markers,
)

#: 规格钉死的六节。顺序即论文顺序。
DEFAULT_SECTIONS: tuple[str, ...] = (
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiment",
    "conclusion",
)

#: 兜底标题（模型没给 title 时用）。键是章节 key，值是 (中文, English)。
SECTION_TITLES: dict[str, tuple[str, str]] = {
    "abstract": ("摘要", "Abstract"),
    "introduction": ("引言", "Introduction"),
    "related_work": ("相关工作", "Related Work"),
    "method": ("方法", "Method"),
    "experiment": ("实验", "Experiment"),
    "conclusion": ("结论", "Conclusion"),
}

#: 各节的默认写作意图。模型规划失败时用它兜底，保证"就算模型全挂也能给出六节框架"。
SECTION_BRIEFS: dict[str, tuple[str, str]] = {
    "abstract": ("用四句话概括：问题、方法、关键结果、意义。", "Problem, method, key result, significance."),
    "introduction": (
        "从领域背景漏斗到具体问题，最后列出本文贡献。",
        "Funnel from background to the problem; list contributions.",
    ),
    "related_work": (
        "按方法脉络组织已有工作，点明与本文的差异。",
        "Organize prior work by method lineage; state the gap.",
    ),
    "method": (
        "逐步说明方法设计，给出必要的公式与模块划分。",
        "Describe the method step by step with modules and formulas.",
    ),
    "experiment": (
        "交代数据集、基线、指标与主要结果，附消融。",
        "Datasets, baselines, metrics, main results, ablations.",
    ),
    "conclusion": ("回收贡献，说明局限与后续方向。", "Recap contributions; limitations and future work."),
}


# ==================================================================== 规划
class SectionPlan(BaseModel):
    key: str = Field(description="章节标识，小写下划线，如 related_work / method")
    title: str = Field(default="", max_length=80, description="章节标题")
    brief: str = Field(default="", max_length=400, description="这一节该写什么")
    points: list[str] = Field(default_factory=list, max_length=5, description="要点，每条一句话")


class OutlinePlan(BaseModel):
    title: str = Field(default="", max_length=200, description="建议的论文标题")
    rationale: str = Field(default="", max_length=600, description="为什么这样分节")
    sections: list[SectionPlan] = Field(default_factory=list, max_length=10)


def _lang(language: str) -> str:
    return "中文" if language.startswith("zh") else "English"


_PLAN_SYSTEM = """你是资深论文作者，负责把一个研究 Idea 拆成可写的章节结构。

硬规则：
1. 章节按论文的标准骨架给：摘要 → 引言 → 相关工作 → 方法 → 实验 → 结论。
   除非用户明确指定了别的章节，否则不要把这几节合并或改名。
2. `brief` 写"这一节要交代哪些内容"，不要写"本节介绍…"这种空话。
3. `points` 给 2~4 条**该节必须回答的问题**，每条一句话，要具体到能直接照它写。
4. `rationale` 说明这套结构为什么适合这个 Idea（一句话）。
5. 只输出 JSON，不要解释。"""


def _fallback_plan(idea: str, wanted: list[str], language: str) -> OutlinePlan:
    """规划模型不可用时的兜底：给出标准六节。

    为什么值得写这个兜底：框架生成是整条链路的入口，入口挂了后面全白搭。而"标准六节"
    本身是领域常识、不依赖模型判断 —— 用它兜底，用户至少能拿到结构去逐节起草。
    """
    keys = wanted or list(DEFAULT_SECTIONS)
    zh = language.startswith("zh")
    return OutlinePlan(
        title=idea[:80],
        rationale="规划模型不可用，退化为论文标准骨架。",
        sections=[
            SectionPlan(
                key=k,
                title=SECTION_TITLES.get(k, (k, k))[0 if zh else 1],
                brief=SECTION_BRIEFS.get(k, ("", ""))[0 if zh else 1],
                points=[],
            )
            for k in keys
        ],
    )


def _normalize(sections: list[SectionPlan], wanted: list[str], *, language: str, max_n: int) -> list[SectionPlan]:
    """去重、保序、限量；`wanted` 非空时以它为准（用户点了哪些章节就必须有哪几节）。"""
    zh = language.startswith("zh")
    by_key: dict[str, SectionPlan] = {}
    order: list[str] = []
    for sec in sections:
        key = (sec.key or "").strip().lower().replace(" ", "_")
        if not key or key in by_key:
            continue
        sec.key = key
        by_key[key] = sec
        order.append(key)

    if wanted:
        merged: list[SectionPlan] = []
        for key in wanted:
            key = key.strip().lower().replace(" ", "_")
            hit = by_key.get(key)
            if hit is not None:
                merged.append(hit)
            else:  # 模型漏了用户点的那一节 —— 补一节占位，而不是悄悄吞掉
                merged.append(
                    SectionPlan(
                        key=key,
                        title=SECTION_TITLES.get(key, (key, key))[0 if zh else 1],
                        brief=SECTION_BRIEFS.get(key, ("", ""))[0 if zh else 1],
                    )
                )
        return merged[:max_n]

    if not order:
        return _fallback_plan("", [], language).sections[:max_n]
    return [by_key[k] for k in order][:max_n]


async def plan_outline(
    idea: str,
    *,
    language: str = "zh",
    sections: list[str] | None = None,
) -> OutlinePlan:
    wanted = [s for s in (sections or []) if s and s.strip()]
    ask = f"必须且只覆盖这些章节（按此顺序，key 用给定值）：{wanted}" if wanted else "按论文标准骨架给出六个章节。"
    try:
        plan = await complete_structured(
            OutlinePlan,
            [
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": f"研究 Idea：{idea}\n输出语言：{_lang(language)}\n{ask}"},
            ],
            role=Role.PLANNER,
        )
    except Exception as exc:  # noqa: BLE001 - 规划失败不该让框架端点整体失败
        logger.warning("论文框架规划失败，退化为标准骨架：{}", exc)
        return _fallback_plan(idea, wanted, language)

    plan.sections = _normalize(plan.sections, wanted, language=language, max_n=settings.WRITING_OUTLINE_MAX_SECTIONS)
    return plan


# ==================================================================== 起草
_DRAFT_SYSTEM = """你是学术写作助手，按给定的材料写论文的一节。

**引用规则（最重要）**：
1. 「可用材料」里每一块都以 `[n]` 开头，`n` 就是它的编号。
2. 凡引用事实、数据、结论，必须在**句末**标出来源，写成 `[n]`（多个写成 `[1,3]`）。
3. 编号只能用「可用材料」里出现过的。**绝不允许**写材料之外的编号或编造论文名 ——
   越界的编号会被程序直接摘掉，那一段就白写了。
4. 材料里没有依据的话就不要写；确实需要但无依据的，写「该点需补充文献」。
5. 不要写空话套话（"具有重要意义"这类），每句都要落到具体对象上。

直接输出这一节的正文，不要重复章节标题，不要解释你的写法。"""


@dataclass(slots=True)
class SectionDraft:
    """一节的起草结果。`report` 留着是为了全局重编号和加权有据率。"""

    plan: SectionPlan
    text: str = ""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    report: TraceReport = field(default_factory=TraceReport)
    dropped: list[int] = field(default_factory=list)


def phantoms_of(text: str, chunks: list[RetrievedChunk], report: TraceReport) -> list[int]:
    """采出正文里的越界编号。

    `trace()` 在**证据为空**时会提前返回、不填 `phantom_markers`（它把"没有上下文"归到
    `uncited_claims` 那一支）。但那恰恰是最该拦的情形：没有检索到任何证据，正文里的每个
    `[n]` 都是凭空编的。所以这里补上这一支。
    """
    if chunks:
        return sorted(set(report.phantom_markers))
    return sorted(set(extract_markers(text)))


def _evidence_query(idea: str, plan: SectionPlan) -> str:
    """检索式 = Idea + 本节标题 + 本节要写什么。

    只用一个不行：单用 Idea 会让六节检索到同一批证据（各节没有区分度），
    单用本节标题又会丢掉 Idea 的领域信息。
    """
    parts = [idea.strip(), (plan.title or plan.key).strip(), plan.brief.strip()]
    return " ".join(p for p in parts if p)[:400]


async def draft_section(
    idea: str,
    plan: SectionPlan,
    *,
    language: str = "zh",
    top_k: int = 5,
    paper_ids: list[int] | None = None,
) -> SectionDraft:
    """检索 → 起草 → 溯源 → 摘掉越界编号。绝不抛异常：单节失败要留下一个空节占位。"""
    draft = SectionDraft(plan=plan)
    try:
        draft.chunks = await HybridRetriever().retrieve(
            _evidence_query(idea, plan),
            paper_ids=paper_ids or None,
            top_k=max(1, top_k),
        )
        context = to_context_block(draft.chunks) if draft.chunks else ""
        instruction = (
            f"论文 Idea：{idea}\n"
            f"本节：{plan.title or plan.key}\n"
            f"本节要交代：{plan.brief}\n"
            + (f"必须回答的问题：{'；'.join(plan.points)}\n" if plan.points else "")
            + f"输出语言：{_lang(language)}"
        )
        text = await get_llm().complete(
            Role.EXECUTOR,
            [
                {"role": "system", "content": _DRAFT_SYSTEM},
                {
                    "role": "user",
                    "content": f"{instruction}\n\n可用材料：\n{context or '（本轮没有检索到任何材料 —— 请只写该点需补充文献类说明，不要编造引用）'}",
                },
            ],
            temperature=0.4,
        )
        draft.text = str(text or "")
        draft.report = get_tracer().trace(draft.text, draft.chunks)
        draft.dropped = phantoms_of(draft.text, draft.chunks, draft.report)
        draft.text = drop_markers(draft.text, draft.dropped)
    except Exception as exc:  # noqa: BLE001 - 一节失败不该拖垮整篇
        logger.warning("章节起草失败 key={}：{}", plan.key, exc)
        draft.text = ""
    return draft


# ==================================================================== 组装
def _citation_out(citation: Any, chunk: Any, paper: Any, marker: int) -> CitationOut:
    """`source_tracing.Citation` → 对外契约。`title` 只能从元数据补（trace 不认识论文表）。"""
    return CitationOut(
        marker=marker,
        chunk_id=int(citation.chunk_id) if str(citation.chunk_id or "").strip() else None,
        paper_id=int(citation.paper_id) if str(citation.paper_id or "").strip() else None,
        title=(getattr(paper, "title", "") or "").strip(),
        section=getattr(chunk, "section", None) or citation.section,
        page=citation.page_start or getattr(chunk, "page", None),
        bbox=citation.bbox,
        quote=citation.quote,
        answer_span=citation.answer_span,
        nli_score=round(float(citation.nli_score), 4),
        confidence=round(float(citation.confidence), 4),
        verified=bool(citation.supported),
        attribution_method=citation.attribution_method,  # type: ignore[arg-type]
    )


def assign_global_markers(
    drafts: list[SectionDraft],
) -> tuple[list[dict[int, int]], dict[int, int]]:
    """各节局部编号 → 全篇编号。返回 `(每节的重映射表, {chunk_id: 全篇编号})`。

    按**首次出现**顺序编号（读论文时参考文献表就是按这个顺序排的），
    并且**按 chunk 而不是按 (节, 编号) 编号** —— 同一块证据在 Method 和 Experiment 都引了，
    它就该是同一个编号，否则参考文献表里会出现两条一模一样的条目。
    """
    chunk_marker: dict[int, int] = {}
    per_section: list[dict[int, int]] = []
    for draft in drafts:
        mapping: dict[int, int] = {}
        for citation in draft.report.citations:
            raw = str(citation.chunk_id or "").strip()
            key = int(raw) if raw else -(len(chunk_marker) + 1)  # chunk_id 缺失时给个不冲突的负键
            marker = chunk_marker.setdefault(key, len(chunk_marker) + 1)
            mapping[citation.marker] = marker
        per_section.append(mapping)
    return per_section, chunk_marker


def _usage(base: dict[str, int]) -> dict[str, Any]:
    """本次调用的用量增量。

    `get_llm()` 是 `@lru_cache` 单例，`usage` 是**进程级累计** ——
    直接读 `usage.total_tokens` 会得到"服务启动至今"的总数（见
    `app.llm.client.usage_snapshot`）。必须在每个入口拍快照再算差。
    """
    return usage_delta(base)


async def build_outline(session: AsyncSession, req: OutlineRequest) -> OutlineResult:
    started = time.perf_counter()
    usage_base = usage_snapshot()
    plan = await plan_outline(req.idea, language=req.language, sections=req.sections)
    paper_ids = [int(p) for p in req.paper_ids] or None

    if not req.draft:
        return OutlineResult(
            title=plan.title,
            idea=req.idea,
            language=req.language,
            rationale=plan.rationale,
            sections=[OutlineSection(key=s.key, title=s.title, brief=s.brief, points=s.points) for s in plan.sections],
            usage=_usage(usage_base),
        )

    # 并发上限：每节都要跑一次 bge-m3 编码 + CrossEncoder 重排，全是本地 CPU 活。
    # 六节一起放出去不会更快，只会让编码器互相抢核（并发的收益在网络等待上，不在本地算力上）。
    sem = asyncio.Semaphore(max(1, settings.WRITING_OUTLINE_CONCURRENCY))

    async def one(sec: SectionPlan) -> SectionDraft:
        async with sem:
            return await draft_section(
                req.idea, sec, language=req.language, top_k=req.evidence_per_section, paper_ids=paper_ids
            )

    drafts = list(await asyncio.gather(*(one(s) for s in plan.sections)))
    per_section, chunk_marker = assign_global_markers(drafts)

    all_chunks = {int(c.id): c for d in drafts for c in d.chunks}
    meta = await load_paper_meta(session, (c.paper_id for c in all_chunks.values()))

    sections: list[OutlineSection] = []
    phantom: list[int] = []
    for draft, mapping in zip(drafts, per_section, strict=True):
        phantom.extend(draft.dropped)
        citations: dict[int, CitationOut] = {}
        for citation in draft.report.citations:
            raw = str(citation.chunk_id or "").strip()
            key = int(raw) if raw else None
            global_marker = mapping.get(citation.marker)
            if global_marker is None or global_marker in citations:
                continue
            chunk = all_chunks.get(key) if key is not None else None
            paper = meta.get(int(getattr(chunk, "paper_id", 0) or 0))
            citations[global_marker] = _citation_out(citation, chunk, paper, global_marker)
        sections.append(
            OutlineSection(
                key=draft.plan.key,
                title=draft.plan.title or draft.plan.key,
                brief=draft.plan.brief,
                points=draft.plan.points,
                draft=remap_markers(draft.text, mapping),
                citations=[citations[m] for m in sorted(citations)],
                grounding_ratio=round(draft.report.grounding_ratio, 4),
                evidence_count=len(draft.chunks),
                removed_markers=sorted(set(draft.dropped)),
            )
        )

    references = await references_for_chunks(session, list(all_chunks.values()), chunk_marker, language=req.language)

    # 全篇有据率按实词加权，而不是各节简单平均 —— 平均会让一段 200 字的摘要
    # 与一节 800 字的实验等权，一个节写崩了在总数上看不出来。
    terms_total = sum(d.report.terms_total for d in drafts)
    terms_supported = sum(d.report.terms_supported for d in drafts)
    ratio = (terms_supported / terms_total) if terms_total else 0.0

    logger.info(
        "论文框架生成完成 sections={} 证据块={} 参考文献={} 有据率={:.2f} 摘掉幻觉编号={} 用时={}ms",
        len(sections),
        len(all_chunks),
        len(references),
        ratio,
        sorted(set(phantom)),
        int((time.perf_counter() - started) * 1000),
    )
    return OutlineResult(
        title=plan.title,
        idea=req.idea,
        language=req.language,
        rationale=plan.rationale,
        sections=sections,
        references=references,
        grounding_ratio=round(ratio, 4),
        phantom_markers=sorted(set(phantom)),
        usage=_usage(usage_base),
    )


# ==================================================================== 扩写
_EXPAND_SYSTEM = """你是学术写作助手，把用户给出的段落/要点扩写成正式正文。

**引用规则**：
1. 「可用材料」每块以 `[n]` 开头，`n` 是它的编号；引用材料必须在句末标 `[n]`。
2. 编号只能用材料里出现过的，越界编号会被程序摘掉。材料里没有的就不要写。
3. 保持原段落的核心主张不变，只做展开、补细节、补衔接 —— 不要换论点。

只输出扩写后的正文（可以比原文长，但不要加小标题、不要解释）。"""


async def expand_paragraph(session: AsyncSession, req: ExpandRequest) -> ExpandResult:
    """扩写一段。`marker_offset` 让调用方把本节编号接在文档已有编号之后。"""
    usage_base = usage_snapshot()
    query = (req.query or req.text[:400]).strip()
    chunks = await HybridRetriever().retrieve(
        query,
        paper_ids=[int(p) for p in req.paper_ids] or None,
        top_k=max(1, req.evidence_top_k),
    )
    context = to_context_block(chunks) if chunks else ""
    target = f"目标篇幅：约 {req.target_length} 字。\n" if req.target_length else ""
    instruction = (
        f"要扩写的段落：\n{req.text}\n\n"
        + (f"上下文（保持衔接，不要重复它）：\n{req.context}\n\n" if req.context.strip() else "")
        + (f"额外要求：{req.instruction}\n" if req.instruction.strip() else "")
        + target
        + f"所属章节：{req.section}\n输出语言：{_lang(req.language)}"
    )
    text = str(
        await get_llm().complete(
            Role.EXECUTOR,
            [
                {"role": "system", "content": _EXPAND_SYSTEM},
                {
                    "role": "user",
                    "content": f"{instruction}\n\n可用材料：\n{context or '（本轮没有检索到材料 —— 不要标注任何引用）'}",
                },
            ],
            temperature=0.4,
        )
        or ""
    )

    report = get_tracer().trace(text, chunks)
    dropped = phantoms_of(text, chunks, report)
    text = drop_markers(text, dropped)

    offset = int(req.marker_offset)
    shift = {m: m + offset for m in range(1, len(chunks) + 1)} if offset else {}
    if shift:
        text = remap_markers(text, shift)

    meta = await load_paper_meta(session, (c.paper_id for c in chunks))
    citations: list[CitationOut] = []
    for citation in report.citations:
        global_marker = citation.marker + offset
        chunk = next((c for c in chunks if str(c.id) == str(citation.chunk_id)), None)
        paper_id = int(getattr(chunk, "paper_id", 0) or 0)
        citations.append(_citation_out(citation, chunk, meta.get(paper_id), global_marker))

    logger.info(
        "段落扩写完成 证据块={} 引用={} 有据率={:.2f} 摘掉幻觉编号={} 偏移={}",
        len(chunks),
        len(citations),
        report.grounding_ratio,
        dropped,
        offset,
    )
    return ExpandResult(
        text=text,
        citations=citations,
        grounding_ratio=round(report.grounding_ratio, 4),
        retrieved_count=len(chunks),
        removed_markers=dropped,
        usage=_usage(usage_base),
    )


__all__ = [
    "DEFAULT_SECTIONS",
    "SECTION_BRIEFS",
    "SECTION_TITLES",
    "OutlinePlan",
    "SectionDraft",
    "SectionPlan",
    "assign_global_markers",
    "build_outline",
    "draft_section",
    "expand_paragraph",
    "phantoms_of",
    "plan_outline",
]
