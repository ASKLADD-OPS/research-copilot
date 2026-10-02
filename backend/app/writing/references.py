"""无幻觉 References —— 逐条校验，再据此排参考文献表。

规格里的三关，这里是**机械**实现，不走模型：

1. **引用 chunk_id 存在**：编号能不能落回一块真实的库内 chunk。
   `CitationOut.chunk_id` 是我们写正文时自己填的，但它也可能是"编号对、那一块后来被删了"，
   所以一律回 `chunks` 表查一遍，查不到就是 `chunk_missing`。
2. **引用内容与声称的事实一致（NLI）**：把引用它的那句话（claim）与该 chunk 的正文
   交给溯源引擎的蕴含判定 —— 装了 `cross-encoder/nli-deberta-v3-base` 就是真蕴含，
   没装退化为词法代理。**数字另有一条硬规则**（`numbers_consistent`）：
   学术文本里最容易被幻觉篡改、又最容易机械校验的就是数字。
3. **参考文献信息来自元数据**：作者 / 年份 / 会议**只从 `papers` 表取**，
   一概不采信模型输出。参考文献表里最常被编造的就是这三项，而它们恰好库里都有。

判定口径（`status`）：

| status | 含义 | 处置 |
|---|---|---|
| `ok` | 三关全过 | 保留，进参考文献表 |
| `weak` | chunk 在、元数据齐，但语义蕴含不过阈值 | **保留但标灰**，并在 checks 里给出理由 |
| `phantom` | 编号不在检索上下文里（凭空的） | 摘掉 / 标 `[citation needed]` |
| `chunk_missing` | 编号有，chunk 查不到 | 同上 |
| `metadata_missing` | 论文不在库里或作者/年份/会议三项全空 | 同上 |

`weak` 之所以不摘：蕴含判定的阈值（0.6）本身是保守的，把真实的近义改写判成"不过"是常态；
而"编造一个不存在的 [7]"是完全不同性质的事故。两者混为一谈会让工具变得不可用。
真正的兜底是**前端把 weak 标灰**，让人去核对，而不是替人做决定。

再往上一层：`OutlineResult.references` 用的是同一套 `ReferenceOut`，
所以论文框架产出的参考文献表与这里产出的形状一致，前端一套组件两处都能画。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Paper
from app.rag.retriever import HybridRetriever, RetrievedChunk
from app.rag.source_tracing import (
    extract_markers,
    get_tracer,
    numbers_consistent,
    sentence_spans,
    strip_markers,
    term_grounding_ratio,
)
from app.schemas import CitationCheck, ReferenceOut, ReferenceRequest, ReferenceResult

#: 硬失败（必须处置）的状态；`weak` 不在其中，见模块头。
HARD_FAIL = frozenset({"phantom", "chunk_missing", "metadata_missing"})

#: 一整块引用标记，如 `[1]` / `[1,2]` / `[3-5]` / `【1】`。
_MARKER_TOKEN = re.compile(r"[\[【]\s*\d+(?:\s*[-–,，]\s*\d+)*\s*[\]】]")

PLACEHOLDER = "[citation needed]"


# ==================================================================== marker 重写
def remap_markers(text: str, mapping: dict[int, int]) -> str:
    """按 `mapping` 重写正文里的引用编号（保留没有映射的编号原样）。

    三个地方都要它：全局重编号（各节局部编号 → 全篇编号）、扩写的偏移、
    以及把越界编号换成 `[citation needed]`。写成一份，免得三处各有一套正则。
    """
    if not mapping:
        return text

    def repl(match: re.Match[str]) -> str:
        nums = extract_markers(match.group(0))
        if not nums:
            return match.group(0)
        out = [mapping.get(n, n) for n in nums]
        if out == nums:
            return match.group(0)
        return "[" + ",".join(str(n) for n in out) + "]"

    return _MARKER_TOKEN.sub(repl, text)


def drop_markers(text: str, markers: Iterable[int], *, placeholder: bool = False) -> str:
    """摘掉指定编号。`placeholder=True` 时原地留一个 `[citation needed]`。

    一块标记里只要含要摘的编号就整体重写（`[1,7]` → `[1]`），而不是只删那几个数字 ——
    `[1,]` 这种残骸比多留一个编号更糟。
    """
    victims = {int(m) for m in markers}
    if not victims:
        return text

    def repl(match: re.Match[str]) -> str:
        nums = extract_markers(match.group(0))
        keep = [n for n in nums if n not in victims]
        if len(keep) == len(nums):
            return match.group(0)
        if not keep:
            return PLACEHOLDER if placeholder else ""
        return "[" + ",".join(str(n) for n in keep) + "]"

    return _MARKER_TOKEN.sub(repl, text)


# ==================================================================== 论文元数据
def author_names(paper: Paper | None) -> list[str]:
    """`papers.authors` 是 `[{name, affiliation}]`，也容忍纯字符串（早期数据）。"""
    out: list[str] = []
    for item in getattr(paper, "authors", None) or []:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else str(item or "").strip()
        if name:
            out.append(name)
    return out


def metadata_ok(paper: Paper | None) -> bool:
    """作者 / 年份 / 会议**至少有一项**才算有元数据。

    为什么不是三项都要：本地 PDF 解析不出年份是常态，Semantic Scholar 补全前 `venue`
    也是 NULL（见 `papers` 表的注释）。要求三项齐全会把大量真实论文判成失败。
    反过来"三项全空"只可能来自一篇刚上传、什么都还没解析出来的记录 —— 那确实不该
    出现在参考文献表里。
    """
    if paper is None:
        return False
    return bool(author_names(paper) or paper.year or (paper.venue or "").strip())


def format_reference(paper: Paper | None, *, marker: int, language: str = "zh") -> str:
    """排一行参考文献。缺哪项就跳过哪项，**不拿占位符凑数**。

    格式接近 GB/T 7714 的顺序（作者. 标题. 会议, 年份.），中英文只在"等 / et al."上分叉 ——
    真正的期刊模板要求千奇百怪，这里的定位是"可直接核对的草稿"，不是排版成果。
    """
    if paper is None:
        return f"[{marker}] （元数据缺失）"
    names = author_names(paper)
    tail = " 等" if language.startswith("zh") else " et al."
    if not names:
        who = "佚名" if language.startswith("zh") else "Anonymous"
    elif len(names) > 3:
        who = ", ".join(names[:3]) + tail
    else:
        who = ", ".join(names)

    parts = [who]
    if (paper.title or "").strip():
        parts.append(paper.title.strip())
    venue = (paper.venue or "").strip()
    if venue:
        parts.append(f"{venue}, {paper.year}" if paper.year else venue)
    elif paper.year:
        parts.append(str(paper.year))
    if paper.doi:
        parts.append(f"DOI: {paper.doi}")
    elif paper.arxiv_id:
        parts.append(f"arXiv:{paper.arxiv_id}")
    return f"[{marker}] " + ". ".join(parts) + "."


def reference_of(
    paper: Paper | None,
    *,
    marker: int,
    chunk_id: int | None = None,
    section: str | None = None,
    page: int | None = None,
    quote: str = "",
    language: str = "zh",
) -> ReferenceOut:
    return ReferenceOut(
        marker=marker,
        paper_id=int(paper.id) if paper is not None else None,
        chunk_id=chunk_id,
        title=(getattr(paper, "title", "") or "").strip(),
        authors=author_names(paper),
        year=getattr(paper, "year", None),
        venue=(getattr(paper, "venue", "") or "").strip(),
        doi=(getattr(paper, "doi", "") or "").strip(),
        arxiv_id=(getattr(paper, "arxiv_id", "") or "").strip(),
        url=(getattr(paper, "source_url", "") or "").strip(),
        section=section,
        page=page,
        quote=quote,
        formatted=format_reference(paper, marker=marker, language=language),
    )


async def load_paper_meta(session: AsyncSession, paper_ids: Iterable[Any]) -> dict[int, Paper]:
    """批量取论文行。一次查询吃掉所有 id（不是 N+1）。"""
    ids = sorted({int(p) for p in paper_ids if p is not None})
    if not ids:
        return {}
    rows = (await session.execute(select(Paper).where(Paper.id.in_(ids)))).scalars().all()
    return {int(r.id): r for r in rows}


async def references_for_chunks(
    session: AsyncSession,
    chunks: Sequence[RetrievedChunk],
    markers: dict[Any, int],
    *,
    language: str = "zh",
) -> list[ReferenceOut]:
    """按 `{chunk_id: marker}` 排参考文献表（论文框架用它，避免每节各排一份）。

    `markers` 里没有的 chunk 直接跳过 —— 说明它虽然被检索到了但从没被正文引用，
    不该出现在 References 里（写了一堆没引的文献，读者核对时会当成幻觉）。
    """
    wanted = {int(cid): m for cid, m in markers.items() if cid is not None}
    if not wanted:
        return []
    picked = [c for c in chunks if int(c.id) in wanted]
    meta = await load_paper_meta(session, (c.paper_id for c in picked))
    out = [
        reference_of(
            meta.get(int(c.paper_id)),
            marker=wanted[int(c.id)],
            chunk_id=int(c.id),
            section=c.section,
            page=c.page,
            quote=(c.content or "")[:280],
            language=language,
        )
        for c in picked
    ]
    out.sort(key=lambda r: r.marker)
    return out


# ==================================================================== 逐条校验
def _claims_of(content: str) -> tuple[list[int], dict[int, list[str]], list[str]]:
    """正文 → (编号出现顺序, 编号 → **引用它的每一句话**, 句子列表)。

    一个编号被多句引用时**全部留下**，不取第一句。取第一句会让"`[1]` 第一次用对了、
    第三次把数字改成 21%"这种事逃过校验 —— 而那条编号此刻正在支撑一个假结论，
    正是抗幻觉该拦的东西。判定口径因此收紧为"该编号的**每一处**用法都得站得住"。
    """
    order: list[int] = []
    occurrences: dict[int, list[str]] = {}
    sentences: list[str] = []
    for _, _, sentence in sentence_spans(content):
        sentences.append(sentence)
        for marker in extract_markers(sentence):
            claim = strip_markers(sentence)
            if marker not in occurrences:
                order.append(marker)
                occurrences[marker] = [claim]
            elif claim not in occurrences[marker]:  # 同一句里同一编号出现两次不该算两处
                occurrences[marker].append(claim)
    return order, occurrences, sentences


async def build_references(session: AsyncSession, req: ReferenceRequest) -> ReferenceResult:
    """校验 → 清洗正文 → 排参考文献表。全流程无模型调用。"""
    tracer = get_tracer()
    order, occurrences, sentences = _claims_of(req.content)

    # ---- 1. 编号 → chunk 的映射：优先用调用方回传的（与写正文时完全一致），否则重检索
    provided: dict[int, dict[str, Any]] = {}
    for item in req.citations:
        if isinstance(item, dict) and item.get("marker") is not None:
            provided[int(item["marker"])] = item
    from_provided = bool(provided)

    by_marker: dict[int, dict[str, Any]] = dict(provided)
    if not from_provided:
        query = (req.query or req.content[:600]).strip()
        chunks = await HybridRetriever().retrieve(
            query,
            paper_ids=[int(p) for p in req.paper_ids] or None,
            top_k=req.top_k,
        )
        # 编号规则与 `to_context_block` 一致：第 i 个 chunk 就是正文里的 [i]。
        by_marker = {
            i: {"marker": i, "chunk_id": c.id, "paper_id": c.paper_id, "page": c.page, "quote": (c.content or "")[:280]}
            for i, c in enumerate(chunks, start=1)
        }

    # ---- 2. chunk 一律回表核实（"引用 chunk_id 存在"这一关）
    wanted_ids = {int(v["chunk_id"]) for v in by_marker.values() if v.get("chunk_id") is not None}
    rows: list[Chunk] = []
    if wanted_ids:
        rows = (await session.execute(select(Chunk).where(Chunk.id.in_(sorted(wanted_ids))))).scalars().all()
    chunks_by_id = {int(r.id): r for r in rows}
    meta = await load_paper_meta(session, (r.paper_id for r in rows))

    # ---- 3. 逐条判
    checks: list[CitationCheck] = []
    for marker in order:
        claims = occurrences.get(marker, [])
        info = by_marker.get(marker)
        if info is None:
            checks.append(
                CitationCheck(
                    marker=marker,
                    claim=" ".join(claims)[:400],
                    status="phantom",
                    reason="该编号不在检索上下文里（模型凭空编的编号）",
                )
            )
            continue

        chunk_id = int(info["chunk_id"]) if info.get("chunk_id") is not None else None
        row = chunks_by_id.get(chunk_id) if chunk_id is not None else None
        if row is None:
            checks.append(
                CitationCheck(
                    marker=marker,
                    claim=" ".join(claims)[:400],
                    chunk_id=chunk_id,
                    paper_id=int(info["paper_id"]) if info.get("paper_id") is not None else None,
                    page=info.get("page"),
                    quote=str(info.get("quote") or ""),
                    status="chunk_missing",
                    reason=f"编号对应的 chunk_id={chunk_id} 在库中不存在（被删过？索引不一致？）",
                )
            )
            continue

        paper = meta.get(int(row.paper_id))
        chunk_text = row.content or ""
        # 该编号的**每一处**用法都要站得住，取最差的一处当结论（见 `_claims_of`）
        verdicts = [
            (claim, tracer.entail(claim, chunk_text), numbers_consistent(claim, chunk_text)) for claim in claims
        ]
        scores = [score for _, score, _ in verdicts] or [0.0]
        bad_numbers = [claim for claim, _, nums_ok in verdicts if not nums_ok]
        score = min(scores)
        consistent = bool(verdicts) and all(s >= tracer.nli_threshold and n for _, s, n in verdicts)
        # 报告里给出"最该看的那句话"：优先给没过的那一句
        culprit = next(
            (claim for claim, s, n in verdicts if not (s >= tracer.nli_threshold and n)),
            claims[0] if claims else "",
        )
        has_meta = metadata_ok(paper)

        if not has_meta:
            status, reason = "metadata_missing", "论文不在库中，或作者/年份/会议三项全空 —— 无法排入参考文献表"
        elif not consistent:
            why = (
                "句子里的数字在证据里找不到" if bad_numbers else f"语义蕴含 {score:.2f} 低于阈值 {tracer.nli_threshold}"
            )
            status, reason = "weak", f"证据不支撑该论断（{why}）"
        else:
            status, reason = "ok", f"chunk 存在 · 蕴含 {score:.2f} · 数字一致 · 元数据齐全"

        checks.append(
            CitationCheck(
                marker=marker,
                claim=culprit[:400],
                chunk_id=chunk_id,
                paper_id=int(row.paper_id),
                page=row.page,
                quote=chunk_text[:280],
                nli_score=round(float(score), 4),
                numbers_ok=not bad_numbers,
                chunk_exists=True,
                content_consistent=consistent,
                metadata_ok=has_meta,
                status=status,
                reason=reason,
            )
        )

    # ---- 4. 清洗正文
    hard_failed = [c.marker for c in checks if c.status in HARD_FAIL]
    weak = [c.marker for c in checks if c.status == "weak"]
    cleaned = drop_markers(req.content, hard_failed, placeholder=not req.remove_invalid)

    # ---- 5. 参考文献表（只收 ok / weak）
    kept = [c for c in checks if c.status in ("ok", "weak")]
    references = [
        reference_of(
            meta.get(c.paper_id) if c.paper_id is not None else None,
            marker=c.marker,
            chunk_id=c.chunk_id,
            section=chunks_by_id[c.chunk_id].section if c.chunk_id in chunks_by_id else None,
            page=c.page,
            quote=c.quote,
            language=req.language,
        )
        for c in kept
    ]
    bibliography = [r.formatted for r in references]

    # ---- 6. 有据率：按"句"给权，实的仍按实词数（复用溯源引擎的那把尺子）
    supported_markers = {c.marker for c in kept if c.content_consistent}
    claims: list[tuple[str, bool]] = []
    for _, _, sentence in sentence_spans(cleaned):
        markers = extract_markers(sentence)
        claims.append((strip_markers(sentence), any(m in supported_markers for m in markers)))
    ratio = term_grounding_ratio(claims)

    logger.info(
        "References 校验 markers={} ok={} weak={} 硬失败={} 有据率={:.2f} 编号映射来源={}",
        len(order),
        len(kept) - len(weak),
        len(weak),
        hard_failed,
        ratio,
        "citations" if from_provided else "重新检索",
    )
    return ReferenceResult(
        content=cleaned,
        references=references,
        bibliography=bibliography,
        checks=checks,
        total_markers=len(order),
        ok_count=len(kept) - len(weak),
        invalid_count=len(hard_failed),
        removed_markers=hard_failed,
        flagged_markers=weak,
        grounding_ratio=round(ratio, 4),
    )


__all__ = [
    "HARD_FAIL",
    "PLACEHOLDER",
    "author_names",
    "build_references",
    "drop_markers",
    "format_reference",
    "load_paper_meta",
    "metadata_ok",
    "reference_of",
    "references_for_chunks",
    "remap_markers",
]
