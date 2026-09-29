"""写作辅助：章节草稿 / 翻译 / 模板清单。

写作走"检索 → 有据生成"：先取证据再写，并把上下文块交给模型，
这样写出来的段落天然带可核查的引用编号，而不是漂亮的空话。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from loguru import logger

from app.api.deps import SessionDep
from app.schemas import (
    ApiResponse,
    BilingualPair,
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


@router.post("/draft", response_model=ApiResponse[WriteResult], summary="生成章节草稿")
async def draft(payload: WriteRequest, session: SessionDep) -> ApiResponse[WriteResult]:
    from app.agents.mcp.local_tools import write_section
    from app.rag.retriever import HybridRetriever, to_context_block
    from app.rag.source_tracing import get_tracer

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

    from app.llm.client import get_llm

    usage = get_llm().usage
    logger.info("写作完成 kind={} chars={} grounding={:.2f}", payload.kind, len(content), grounding)
    return ApiResponse.ok(
        WriteResult(
            kind=payload.kind,
            content=content,
            citations=citations,
            grounding_ratio=grounding,
            retrieved_count=len(chunks),
            usage={"calls": usage.calls, "total_tokens": usage.total_tokens},
        )
    )


@router.post("/translate", response_model=ApiResponse[TranslateResult], summary="学术翻译")
async def translate(payload: TranslateRequest) -> ApiResponse[TranslateResult]:
    from app.agents.mcp.local_tools import translate_text
    from app.llm.client import get_llm

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

    usage = get_llm().usage
    return ApiResponse.ok(
        TranslateResult(
            text=text,
            pairs=pairs,
            usage={"calls": usage.calls, "total_tokens": usage.total_tokens},
        )
    )
