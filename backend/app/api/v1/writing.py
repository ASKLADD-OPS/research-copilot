"""写作辅助。

两组东西，共用同一个"有据才写"的原则：

| 端点 | 干什么 | 实现 |
|---|---|---|
| `GET /writing/templates`、`POST /writing/draft`、`POST /writing/translate` | 工作台右栏的单段生成 / 翻译 | 本文件 + `agents/mcp/local_tools.py` |
| `POST /writing/outline` | Idea → 完整论文框架（逐节有据草稿） | `app/writing/outline.py` |
| `POST /writing/expand` | 段落扩写（带引用偏移） | `app/writing/outline.py` |
| `POST /writing/references` | 无幻觉 References（逐条校验 + 排表） | `app/writing/references.py` |
| `POST /writing/diagram` | TikZ / Graphviz / Mermaid / Matplotlib 图 | `app/writing/diagrams.py` |

单段 `draft` 走的是"检索 → 写"两步；框架/扩写多一层**全局引用重编号**（各节分别起草，
局部编号必须先并成全篇编号才拼得起来），`references` 则是纯粹的机械校验 —— 它一次模型都不调，
理由写在那个模块头上。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from loguru import logger

from app.api.deps import SessionDep
from app.schemas import (
    ApiResponse,
    BilingualPair,
    DiagramRequest,
    DiagramResult,
    ExpandRequest,
    ExpandResult,
    OutlineRequest,
    OutlineResult,
    ReferenceRequest,
    ReferenceResult,
    TranslateRequest,
    TranslateResult,
    WriteRequest,
    WriteResult,
    WritingTemplateOut,
)

router = APIRouter(prefix="/writing", tags=["写作"])

TEMPLATES: list[WritingTemplateOut] = [
    WritingTemplateOut(
        kind="summary",
        name="文献综述摘要",
        description="把若干篇论文的要点合并成一段结构化总结，每句都带引用",
        default_length=600,
        outline=["研究问题", "主流方法", "共同结论", "分歧与空白"],
    ),
    WritingTemplateOut(
        kind="related_work",
        name="相关工作",
        description="按方法脉络组织已有工作，并点明与本文的差异",
        default_length=1200,
        outline=["方法族划分", "代表工作与贡献", "演进关系", "本文定位"],
    ),
    WritingTemplateOut(
        kind="abstract",
        name="摘要",
        description="问题—方法—结果—意义 四段式",
        default_length=400,
        outline=["研究问题", "方法", "关键结果", "意义"],
    ),
    WritingTemplateOut(
        kind="introduction",
        name="引言",
        description="从背景到具体问题的漏斗式写法",
        default_length=1000,
        outline=["领域背景", "未解决的问题", "本文思路", "贡献列表"],
    ),
    WritingTemplateOut(
        kind="review",
        name="审稿意见",
        description="按新颖性 / 严谨性 / 可复现性给结构化评审",
        default_length=800,
        outline=["贡献判断", "方法问题", "实验充分性", "修改建议"],
    ),
    WritingTemplateOut(
        kind="rebuttal",
        name="Response 回复",
        description="逐条回应审稿人，先认同后澄清",
        default_length=900,
        outline=["意见重述", "回应", "已做的修改"],
    ),
]


@router.get("/templates", response_model=ApiResponse[list[WritingTemplateOut]], summary="写作模板")
async def templates() -> ApiResponse[list[WritingTemplateOut]]:
    return ApiResponse.ok(TEMPLATES)


@router.post("/draft", response_model=ApiResponse[WriteResult], summary="生成章节草稿", operation_id="write_draft")
async def draft(payload: WriteRequest, session: SessionDep) -> ApiResponse[WriteResult]:
    from app.agents.mcp.local_tools import write_section
    from app.llm.client import usage_delta, usage_snapshot
    from app.rag.retriever import HybridRetriever, to_context_block
    from app.rag.source_tracing import get_tracer

    # usage 是进程累计，本次只报增量 —— 见 `app.llm.client.usage_snapshot`
    usage_base = usage_snapshot()

    chunks: list[Any] = []
    context = ""
    if payload.use_retrieval:
        chunks = await HybridRetriever().retrieve(
            payload.topic,
            paper_ids=payload.paper_ids or None,
            top_k=10,
        )
        context = to_context_block(chunks) if chunks else ""

    instruction = payload.topic
    if payload.target_length:
        instruction += f"\n目标篇幅：约 {payload.target_length} 字。"
    if payload.style:
        instruction += f"\n风格要求：{payload.style}"

    result = await write_section(instruction, section=payload.kind, language=payload.language, context=context)
    content = str(result.get("text", ""))

    citations: list[dict[str, Any]] = []
    grounding = 0.0
    if chunks:
        # 写作输出同样过一遍溯源：写手最容易"顺手编一条引用"
        report = get_tracer().trace(content, chunks)
        grounding = report.grounding_ratio
        citations = [c.to_dict() for c in report.citations]

    logger.info("写作完成 kind={} chars={} grounding={:.2f}", payload.kind, len(content), grounding)
    return ApiResponse.ok(
        WriteResult(
            kind=payload.kind,
            content=content,
            citations=citations,
            grounding_ratio=grounding,
            retrieved_count=len(chunks),
            usage=usage_delta(usage_base),
        )
    )


@router.post(
    "/translate", response_model=ApiResponse[TranslateResult], summary="学术翻译", operation_id="write_translation"
)
async def translate(payload: TranslateRequest) -> ApiResponse[TranslateResult]:
    from app.agents.mcp.local_tools import translate_text
    from app.llm.client import usage_delta, usage_snapshot

    usage_base = usage_snapshot()

    if payload.bilingual:
        pairs: list[BilingualPair] = []
        for para in [p.strip() for p in payload.text.split("\n") if p.strip()]:
            out = await translate_text(para, target=payload.target, keep_terms=payload.keep_terms)
            pairs.append(BilingualPair(source=para, target=str(out.get("text", ""))))
        text = "\n\n".join(p.target for p in pairs)
    else:
        out = await translate_text(payload.text, target=payload.target, keep_terms=payload.keep_terms)
        text = str(out.get("text", ""))
        pairs = []

    return ApiResponse.ok(
        TranslateResult(
            text=text,
            pairs=pairs,
            usage=usage_delta(usage_base),
        )
    )


# ==================================================================== 论文框架
@router.post(
    "/outline",
    response_model=ApiResponse[OutlineResult],
    summary="从 Idea 生成论文框架",
    operation_id="write_outline",
)
async def outline(payload: OutlineRequest, session: SessionDep) -> ApiResponse[OutlineResult]:
    """Idea → 章节结构 → 每节检索证据 → Self-Citation 起草 → 全篇重编号 → 参考文献表。

    **慢**：默认六节，每节一次检索 + 一次模型调用（并发上限见 `WRITING_OUTLINE_CONCURRENCY`），
    实测十几秒到一分钟。不拆成"先给框架再逐节要正文"两个端点，是因为拆开以后
    全局引用编号就没法统一 —— 各节分开拿到的 `[1]` 指的不是同一篇。

    `draft=false` 只要框架（不检索、不写、不排参考文献），那是几百毫秒的事。
    """
    from app.writing.outline import build_outline

    result = await build_outline(session, payload)
    cited = sum(len(s.citations) for s in result.sections)
    return ApiResponse.ok(
        result,
        message=f"{len(result.sections)} 节 · 引用 {cited} 条 · 参考文献 {len(result.references)} 条"
        + (f" · 有据率 {result.grounding_ratio:.0%}" if payload.draft else "（仅框架）"),
    )


# ==================================================================== 段落扩写
@router.post(
    "/expand",
    response_model=ApiResponse[ExpandResult],
    summary="段落扩展",
    operation_id="write_expand",
)
async def expand(payload: ExpandRequest, session: SessionDep) -> ApiResponse[ExpandResult]:
    """把一段话（或编辑器选区）扩写成正式正文，引用只允许来自本次检索结果。

    返回值里的编号已经按 `marker_offset` 后移过 —— 插到已有正文后面时把
    "文档当前最大编号"传进来，编号就不会和前面撞车。
    """
    from app.writing.outline import expand_paragraph

    result = await expand_paragraph(session, payload)
    return ApiResponse.ok(
        result,
        message=f"扩写 {len(result.text)} 字 · 引用 {len(result.citations)} 条"
        + (f" · 有据率 {result.grounding_ratio:.0%}" if result.retrieved_count else " · 本轮无可用证据")
        + (f" · 摘掉幻觉编号 {result.removed_markers}" if result.removed_markers else ""),
    )


# ==================================================================== 无幻觉 References
@router.post(
    "/references",
    response_model=ApiResponse[ReferenceResult],
    summary="无幻觉 References 生成",
    operation_id="write_references",
)
async def references(payload: ReferenceRequest, session: SessionDep) -> ApiResponse[ReferenceResult]:
    """逐条校验正文里的引用，再把参考文献表排出来。**全程不调模型**（理由见 `app/writing/references.py`）。

    三关：`chunk_id` 回表存在 → 句子与证据的语义蕴含（NLI / 词法代理 + 数字硬规则）
    → 作者/年份/会议只从 `papers` 表取。
    硬失败（编号越界 / chunk 不存在 / 无元数据）的引用按 `remove_invalid` 摘掉或标 `[citation needed]`；
    **语义不过但证据存在的标成 `weak` 保留下来并在 checks 里说明理由** —— 把近义改写和凭空编造
    一锅端会让工具没法用，前端把 weak 标灰让人自己核对才是正解。

    `citations` 传「写正文那一步返回的 citations」最稳（编号映射无需重新猜）；
    不传则按 `query` 重新检索一遍，编号沿用 `to_context_block` 的规则。
    """
    from app.writing.references import build_references

    result = await build_references(session, payload)
    return ApiResponse.ok(
        result,
        message=(
            f"校验 {result.total_markers} 条引用：通过 {result.ok_count}、待核对 {len(result.flagged_markers)}、"
            f"已处置 {result.invalid_count}"
        ),
    )


# ==================================================================== 图表
@router.post(
    "/diagram",
    response_model=ApiResponse[DiagramResult],
    summary="生成架构拓扑图",
    operation_id="write_diagram",
)
async def diagram(payload: DiagramRequest) -> ApiResponse[DiagramResult]:
    """`tikz` / `graphviz` / `mermaid` / `matplotlib` 四种。

    **任何渲染失败都不算请求失败**：源码一定带回来，`renderer` 与 `warning` 如实说明图是从哪来的、
    缺了什么。规格里的"后端沙箱执行 → 返回图片 base64"这条契约落在 `image` 字段上
    （base64 PNG，不含 data: 前缀）；`matplotlib` 那条路刻意改成"约束模型只产出图表规格、
    绘图代码由我们固定" —— 执行模型写的 Python 是这套系统里唯一能让模型碰到解释器的地方，
    不值得为一张图留着（详见 `app/writing/diagrams.py` 模块头）。
    """
    from app.writing.diagrams import generate_diagram

    result = await generate_diagram(payload)
    return ApiResponse.ok(
        result,
        message=(
            f"{result.kind} · 渲染器 {result.renderer}"
            + (f" · 已出图 {len(result.image or '') // 1024}KB(base64) " if result.image else " · 仅源码")
        ),
    )
