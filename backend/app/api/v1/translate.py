"""学术翻译（逐段 + 术语表 + 双向联动）。

| 端点 | 干什么 |
|---|---|
| `POST /translate` | 逐段翻译，返回**段落 ID 映射**，前端据此做左右分栏的滚动同步 |
| `POST /translate/glossary` | 解析上传的术语表文件（CSV / TSV / `A → B` / `A = B` 都认） |

三件事按"实现方式"分：

- **保持公式与引用不变**：写在提示词的规则里（`local_tools.translate_text`）。这类约束没有
  机械可验的判据（LaTeX 片段千变万化），靠模型遵守，但逐段送进去能显著降低它"顺手重排"的概率。
- **术语一致**：不用提示词祈祷模型记住，而是**把术语表原文抄进 prompt 并声明必须照译**，
  同时在返回里给出 `unused_terms` —— 一致性能被核对，才算真的做到了。
- **段落映射**：`p1/p2/…` 由后端切分时定死，翻译不改动这个顺序，所以前端两个 `scroll` 容器
  按 ID 对齐时不会错位（若让前端自己切分，两边切法一旦不同，同步就开始漂）。

并发上限是模块常量而不是给它加配置项：翻译一篇论文是十几个段落量级，
`_CONCURRENCY = 4` 已经能把耗时压到单段的两三倍，参数化它收益为零。
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from loguru import logger

from app.core.errors import BadRequestError
from app.schemas import (
    ApiResponse,
    GlossaryEntry,
    GlossaryParseResult,
    TranslateParagraphsRequest,
    TranslateParagraphsResult,
    TranslatedParagraph,
)

router = APIRouter(prefix="/translate", tags=["翻译"])

_CONCURRENCY = 4
_MAX_CONTENT_MB = 2
_MAX_TERMS = 500
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_PAIR_SPLIT = re.compile(r"\t|,|;|\||→|->|=>|=|:|\u3000")
_HEADER_WORDS = {"source", "target", "原文", "译文", "术语", "term", "translation", "src", "dst"}


def split_paragraphs(text: str) -> list[str]:
    """按空行分段。整篇没有空行时退化成按单行分 —— 常见于"一行一段"的摘抄。

    空行分段是唯一稳定的切法：靠标题、缩进、句号去猜段落边界，遇到公式块、
    参考文献列表就会切错，而切错会让左右两栏的段落对应关系整体错位。
    """
    body = (text or "").strip()
    if not body:
        return []
    blocks = [b.strip() for b in _PARAGRAPH_SPLIT.split(body)]
    blocks = [b for b in blocks if b]
    if len(blocks) == 1 and "\n" in blocks[0]:
        blocks = [b.strip() for b in blocks[0].split("\n") if b.strip()]
    return blocks


def parse_glossary(text: str) -> tuple[list[GlossaryEntry], list[str]]:
    """`(词条, 认不出的行)`。逐行取**第一个**分隔符切成两半，右半边整体保留 ——
    译名里带逗号（"卷积神经网络, 深度"）不该被切碎。"""
    entries: list[GlossaryEntry] = []
    skipped: list[str] = []
    seen: set[str] = set()
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    for raw in lines:
        line = raw.strip().lstrip("-*").strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in _PAIR_SPLIT.split(line, maxsplit=1)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            skipped.append(raw.strip())
            continue
        source, target = parts[0], parts[1]
        if len(entries) == 0 and source.lower() in _HEADER_WORDS and target.lower() in _HEADER_WORDS:
            continue  # 表头行（source,target）不是词条
        if source in seen:
            continue
        seen.add(source)
        entries.append(GlossaryEntry(source=source, target=target))
        if len(entries) >= _MAX_TERMS:
            break
    return entries, skipped


@router.post(
    "/glossary",
    response_model=ApiResponse[GlossaryParseResult],
    summary="解析术语表文件",
    operation_id="translate_glossary",
)
async def upload_glossary(
    file: Annotated[UploadFile, File(description="术语表：每行 `原文,译名`；分隔符也可以是制表符/→/=")],
) -> ApiResponse[GlossaryParseResult]:
    """把上传的术语表文件解析成词条，供 `POST /translate` 的 `glossary` 字段使用。

    解析纯本地、不调模型；认不出成对关系的行原样放进 `skipped` 返回 —— 静默丢弃会让人
    以为"术语表已经生效了"，而实际上那几条根本没进去。
    """
    raw = await file.read()
    if len(raw) > _MAX_CONTENT_MB * 1024 * 1024:
        raise BadRequestError(f"术语表超过上限 {_MAX_CONTENT_MB}MB")

    text = raw.decode("utf-8-sig", errors="replace")
    entries, skipped = parse_glossary(text)
    total = len([ln for ln in text.splitlines() if ln.strip()])
    logger.info("术语表解析：{} 条，跳过 {} 行", len(entries), len(skipped))
    return ApiResponse.ok(
        GlossaryParseResult(entries=entries, skipped=skipped, total_lines=total),
        message=f"解析出 {len(entries)} 条术语" + (f" · {len(skipped)} 行没认出来" if skipped else ""),
    )


@router.post(
    "",
    response_model=ApiResponse[TranslateParagraphsResult],
    summary="逐段学术翻译",
    operation_id="translate_paragraphs",
)
async def translate_paragraphs(payload: TranslateParagraphsRequest) -> ApiResponse[TranslateParagraphsResult]:
    """按空行切段 → 并发翻译（上限 4）→ 回传 `p1/p2/…` 的段落映射。

    `glossary` 里"命中即必须照译"的约束随每段一起下发；`unused_terms` 告诉你哪些词条
    在原文里根本没出现过 —— 术语表配错了（拼写、大小写）时，这一条是唯一的线索。
    """
    from app.agents.mcp.local_tools import translate_text
    from app.llm.client import usage_delta, usage_snapshot

    usage_base = usage_snapshot()
    blocks = split_paragraphs(payload.text)
    terms = {e.source: e.target for e in payload.glossary[:200]}
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(index: int, block: str) -> TranslatedParagraph:
        async with gate:
            out = await translate_text(
                block,
                target=payload.target,
                keep_terms=payload.keep_terms,
                glossary=terms,
                passive=payload.passive,
            )
        return TranslatedParagraph(id=f"p{index + 1}", index=index, source=block, target=str(out.get("text", "")))

    paragraphs = list(await asyncio.gather(*(one(i, b) for i, b in enumerate(blocks))))

    # 大小写不敏感：句首大写过的 "Attention" 与词条 "attention" 是同一个词，
    # 按原样比对会把它报成"没用上"，这种假警报会让人不再看这个字段
    lowered = payload.text.lower()
    unused = [e.source for e in payload.glossary if e.source.lower() not in lowered]
    result = TranslateParagraphsResult(
        language=payload.target,
        paragraphs=paragraphs,
        source_text="\n\n".join(p.source for p in paragraphs),
        target_text="\n\n".join(p.target for p in paragraphs),
        glossary=payload.glossary[:200],
        unused_terms=unused,
        usage=usage_delta(usage_base),
    )
    logger.info(
        "翻译完成 target={} 段数={} 术语={} 未命中={}",
        payload.target,
        len(paragraphs),
        len(payload.glossary),
        len(unused),
    )
    message = f"{len(paragraphs)} 段 · {'中译英' if payload.target == 'en' else '英译中'}"
    if payload.glossary:
        message += f" · 术语表 {len(payload.glossary)} 条（未命中 {len(unused)}）"
    if payload.passive:
        message += " · 被动语态"
    return ApiResponse.ok(result, message=message)


__all__ = ["parse_glossary", "router", "split_paragraphs"]
