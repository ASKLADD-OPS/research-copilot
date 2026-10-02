"""问答：走完整 Agent 图（意图 → 规划 → 检索/工具 → 评审 → 合成 → 防护）。

另有两条只读辅助接口：
- `/qa/retrieve`：只跑检索链路，用于调参与排障（不烧生成 token）；
- `/qa/trace`：对给定文本做溯源校验，用于前端"引用可信度"面板。

每轮问答都会落一条 `qa_history`，里面带 `sources` 溯源数组 —— 有了它，
"上个月那次回答凭什么这么说"才可复查。抗幻觉系统如果自己不可追溯，就没法自证。
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.errors import EmptyRetrievalError, GuardrailBlockedError
from app.db.bootstrap import ensure_default_user
from app.db.session import session_scope
from app.llm.client import get_llm
from app.llm.streaming import Event, done_event, error_event, sse, sse_comment
from app.models import Chunk, Paper, QAHistory
from app.schemas import (
    ApiResponse,
    AskRequest,
    AskResult,
    CitationOut,
    QAHistoryOut,
    RetrievalDebug,
    RetrievedChunkOut,
    SourceTraceOut,
    TimelineItem,
    TraceRequest,
    TraceResult,
)

router = APIRouter(prefix="/qa", tags=["问答"])

# 切句：中英文句末 + 换行。保留定界符本身，否则字符偏移就对不上了。
_SENTENCE_RE = re.compile(r"[^。！？.!?\n]*[。！？.!?]?")
_MARKER_RE = re.compile(r"\[(\d+)\]")


def _chunk_out(chunk: Any) -> RetrievedChunkOut:
    """检索片段的响应视图。

    要同时接受两种形态，这不是洁癖：`/qa/retrieve` 拿到的是一串
    `RetrievedChunk` 对象，而 `/qa/ask` 拿到的是 state 里的 `RetrievedDoc` dict。
    只认其中一种时另一种会**静默退化成全零**（getattr 取不到就给默认值），
    前端于是展示出一堆 chunk_id=0 的"证据"。
    """
    as_dict = isinstance(chunk, dict)

    def pick(name: str, *, alt: str | None = None, default: Any = None) -> Any:
        value = chunk.get(name) if as_dict else getattr(chunk, alt or name, None)
        return default if value is None else value

    if as_dict:
        # state 里的 source 是逗号拼接的字符串（见 agents/nodes/retriever._to_docs）
        raw_sources = str(pick("source", default=""))
        sources = [s for s in raw_sources.split(",") if s]
    else:
        sources = list(pick("sources", default=[]) or [])

    return RetrievedChunkOut(
        chunk_id=int(pick("chunk_id", alt="id", default=0) or 0),
        paper_id=int(pick("paper_id", default=0) or 0),
        title=str(pick("title", default="")),
        section=pick("section"),
        page=pick("page"),
        bbox=pick("bbox"),
        score=round(float(pick("score", default=0.0) or 0.0), 6),
        rerank_score=None if as_dict else pick("rerank_score"),
        sources=sources,
        preview=(str(pick("text", alt="content", default="")) or "")[:300],
    )


async def _attach_titles(session: Any, rows: list[RetrievedChunkOut]) -> None:
    """补论文标题。一次查询搞定，别在循环里查库。"""
    ids = {r.paper_id for r in rows if r.paper_id}
    if not ids:
        return
    titles = dict((await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(ids)))).all())
    for row in rows:
        row.title = str(titles.get(row.paper_id, "") or "")


# ------------------------------------------------------------------ 时间线
# arXiv 编号自带 YYMM：新式 `2301.12345`、老式 `cs.CL/0701001`。
# 老式分类名里可以有点（cs.CL）和数字（cs1），所以字符类比 `[a-z-]` 宽一点。
_ARXIV_NEW_RE = re.compile(r"^(\d{2})(\d{2})\.")
_ARXIV_OLD_RE = re.compile(r"^[a-z][a-z0-9.-]*/(\d{2})(\d{2})")


def year_from_arxiv(arxiv_id: str | None) -> int | None:
    """从 arXiv 编号前缀推发表年份。纯函数，不联网。

    `papers` 表里没有 year 列 —— 而"方法演进"这条线离不开年份。arXiv 编号是
    **唯一免费且不会幻觉**的来源；推不出（非 arXiv 论文）就给 None，前端不画这个点，
    好过编一个年份出来。
    """
    text = (arxiv_id or "").strip().lower()
    for pattern in (_ARXIV_OLD_RE, _ARXIV_NEW_RE):
        match = pattern.match(text)
        if match is None:
            continue
        yy, mm = int(match.group(1)), int(match.group(2))
        if 1 <= mm <= 12:
            # arXiv 1991 年开张：91-99 归 1900s，00-90 归 2000s
            return 1900 + yy if yy >= 91 else 2000 + yy
    return None


def _as_int(value: Any) -> int | None:
    """宽松转 int：state 里的 id 是字符串，转不动就给 None（不编一个 0 出来）。"""
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _paper_id_of(row: Any) -> int | None:
    """从 `RetrievedChunkOut` 或合并后的 state 字典里取 paper_id。"""
    pid = row.get("paper_id") if isinstance(row, dict) else getattr(row, "paper_id", None)
    return _as_int(pid)


async def build_timeline(session: Any, chunks: list[Any]) -> list[TimelineItem]:
    """把本次命中的论文排成时间轴（年份升序，年份缺失的沉底）。一次查询搞定。"""
    counts: dict[int, int] = {}
    for row in chunks:
        pid = _paper_id_of(row)
        if pid is not None:
            counts[pid] = counts.get(pid, 0) + 1
    if not counts:
        return []

    rows = (await session.execute(select(Paper.id, Paper.title, Paper.arxiv_id).where(Paper.id.in_(counts)))).all()
    items = [
        TimelineItem(
            paper_id=int(pid),
            title=str(title or ""),
            arxiv_id=arxiv_id,
            year=year_from_arxiv(arxiv_id),
            chunks=counts[pid],
        )
        for pid, title, arxiv_id in rows
    ]
    items.sort(key=lambda it: (it.year is None, it.year or 0))
    return items


async def timeline_done_extra(merged: dict[str, Any]) -> dict[str, Any]:
    """收尾帧要带的附加数据：跨篇对比才有时间轴。

    `/chat/stream` 与 `/qa/stream` 共用这一份 —— 两条流都靠它把时间轴送到前端，
    各写一遍迟早只改到一条。自己开 session（流式生成器里没有请求级 session），
    并且**失败不算数**：时间轴是锦上添花，算不出不该拖垮整条回答。
    """
    if merged.get("intent") != "cross_paper_reasoning":
        return {"timeline": []}
    try:
        async with session_scope() as session:
            items = await build_timeline(session, merged.get("retrieved") or [])
        return {"timeline": [it.model_dump() for it in items]}
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.exception("时间轴组装失败")
        return {"timeline": []}


def locate_answer_spans(answer: str, markers: set[int]) -> dict[int, tuple[int, int]]:
    """找出每个引用编号 `[n]` 所在**句子**的字符区间。

    `qa_history.sources[].answer_span` 要的是"这条证据支撑答案的哪一段"，
    而粒度的自然选择是句子 —— 前端高亮一整句才看得懂，高亮 `[3]` 这三个字符没有意义。

    拿不到就返回空 dict，而不是编一个 (0, len(answer)) 糊弄过去：
    一个错误的区间会让溯源面板指到错误的段落，比不指更糟。
    """
    spans: dict[int, tuple[int, int]] = {}
    for match in _SENTENCE_RE.finditer(answer):
        sentence = match.group(0)
        if not sentence.strip():
            continue
        for marker in _MARKER_RE.findall(sentence):
            n = int(marker)
            if n in markers and n not in spans:
                spans[n] = (match.start(), match.end())
    return spans


def build_sources(citations: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """把 Agent 的 citations 转成 `qa_history.sources` 的结构。

    结构（规格约定）：{answer_span, chunk_id, paper_id, page, bbox, confidence, method}
    其中 `answer_span` 是**字符区间**（落库用，前端按区间高亮）。
    """
    markers = {int(c.get("marker", 0)) for c in citations if c.get("marker")}
    spans = locate_answer_spans(answer, markers)

    out: list[dict[str, Any]] = []
    for cite in citations:
        chunk_id = cite.get("chunk_id")
        if chunk_id in (None, "", 0):
            continue  # 没有 chunk 的引用等于幻觉引用，不收进溯源记录
        score = float(cite.get("nli_score") or cite.get("confidence") or 0.0)
        # 优先采信溯源引擎给出的认定方式；旧数据（没有该字段）按分数反推
        method = str(cite.get("attribution_method") or "")
        if method not in {"self_citation", "nli", "hybrid"}:
            method = "nli" if score > 0 else "self_citation"
        span = cite.get("char_span") or (list(spans[int(cite["marker"])]) if cite.get("marker") in spans else None)
        out.append(
            {
                "answer_span": list(span) if span else None,
                "chunk_id": int(chunk_id),
                "paper_id": int(cite["paper_id"]) if cite.get("paper_id") not in (None, "") else None,
                "page": cite.get("page_start") or cite.get("page"),
                "bbox": cite.get("bbox"),
                "confidence": round(score, 4),
                "method": method,
            }
        )
    return out


def build_source_traces(citations: list[dict[str, Any]], answer: str) -> list[SourceTraceOut]:
    """API 响应里的溯源数组（规格契约：answer_span 给**文本片段**，不是区间）。

    与落库结构共用同一套解析逻辑 —— 直接从 `build_sources` 的区间切回文本，
    免得两处各写一份"哪个 marker 对应哪句话"的判断然后慢慢漂开。
    """
    traces: list[SourceTraceOut] = []
    for item in build_sources(citations, answer):
        span = item.get("answer_span")
        text = answer[span[0] : span[1]].strip() if span else ""
        traces.append(
            SourceTraceOut(
                answer_span=text,
                chunk_id=item["chunk_id"],
                paper_id=item.get("paper_id"),
                page=item.get("page"),
                bbox=item.get("bbox"),
                confidence=float(item.get("confidence") or 0.0),
                attribution_method=item.get("method") or "self_citation",
            )
        )
    return traces


async def persist_qa_history(
    *,
    user_id: int,
    question: str,
    answer: str,
    intent: str | None,
    paper_ids: list[int],
    citations: list[dict[str, Any]],
    grounding_ratio: float | None,
    faithfulness: float | None,
) -> int | None:
    """落一条问答留痕。**尽力而为**：存储失败不该把已经生成的答案废掉。"""
    try:
        async with session_scope() as session:
            row = QAHistory(
                user_id=user_id,
                paper_ids=[int(p) for p in paper_ids],
                intent=intent,
                question=question,
                answer=answer,
                sources=build_sources(citations, answer),
                grounding_ratio=grounding_ratio,
                faithfulness=faithfulness,
            )
            session.add(row)
            await session.flush()
            return int(row.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qa_history 落库失败（答案已正常返回）：{}", exc)
        return None


# ------------------------------------------------------------------ 路由
@router.post(
    "/ask", response_model=ApiResponse[AskResult], summary="提问（完整 Agent 链路）", operation_id="ask_question"
)
async def ask(payload: AskRequest, session: SessionDep) -> ApiResponse[AskResult]:
    from app.agents.graph import get_graph

    started = time.perf_counter()
    initial: dict[str, Any] = {
        "query": payload.query,
        "target_papers": payload.paper_ids,
        "top_k": payload.top_k,
    }
    if payload.intent:
        initial["intent"] = payload.intent

    graph = get_graph()
    state: dict[str, Any] = await graph.ainvoke(initial, config={"configurable": {"thread_id": "rest"}})

    # 防护硬拒绝（注入 / 越权 / 敏感内容）：不回答案、不落库，统一给 {code:4003} 信封。
    # 软性问题（有据率低、幻觉引用）不在这里 —— 它们在图内回 synthesizer 重写过一轮了。
    if state.get("guardrail_action") == "block":
        logger.warning("问答被防护拦截 flags={}", state.get("guardrail_flags"))
        raise GuardrailBlockedError(
            str(state.get("answer") or GuardrailBlockedError.message),
            data={"flags": list(state.get("guardrail_flags") or []), "reason": state.get("error")},
        )

    retrieved = [_chunk_out(c) for c in (state.get("reranked") or state.get("retrieved") or [])]
    await _attach_titles(session, retrieved)

    grounding = float(state.get("grounding_ratio") or 0.0)
    raw_citations = list(state.get("citations") or [])
    citations = [
        CitationOut(
            marker=c.get("marker"),
            chunk_id=int(c["chunk_id"]) if c.get("chunk_id") not in (None, "") else None,
            paper_id=int(c["paper_id"]) if c.get("paper_id") not in (None, "") else None,
            title=str(c.get("title", "")),
            section=c.get("section"),
            page=c.get("page_start") or c.get("page"),
            bbox=c.get("bbox"),
            quote=str(c.get("quote", ""))[:280],
            answer_span=str(c.get("answer_span", "")),
            nli_score=float(c.get("nli_score") or 0.0),
            confidence=float(c.get("confidence") or c.get("nli_score") or 0.0),
            verified=bool(c.get("verified", False)),
            attribution_method=c.get("attribution_method") or ("hybrid" if c.get("verified") else "self_citation"),
        )
        for c in raw_citations
    ]
    for cite in citations:
        if not cite.title:
            cite.title = next((r.title for r in retrieved if r.chunk_id == cite.chunk_id), "")

    answer = str(state.get("answer") or "")
    # 溯源列表：答案 + 溯源数组一起返回，前端拿到就能做句子高亮与 PDF 跳转
    sources = build_source_traces(raw_citations, answer)
    reflection = state.get("reflection") or {}
    faithfulness = (reflection.get("scores") or {}).get("faithfulness") if reflection else None
    # 时间轴只在跨篇对比时排：单篇问答排出来只有一个点，白搭一次查询
    timeline = await build_timeline(session, retrieved) if state.get("intent") == "cross_paper_reasoning" else []
    usage = get_llm().usage
    latency = int((time.perf_counter() - started) * 1000)
    logger.info("问答完成 intent={} grounding={:.2f} latency={}ms", state.get("intent"), grounding, latency)

    user_id = await ensure_default_user(session)
    history_id = await persist_qa_history(
        user_id=user_id,
        question=payload.query,
        answer=answer,
        intent=state.get("intent"),
        paper_ids=[int(p) for p in (payload.paper_ids or state.get("target_papers") or [])],
        citations=raw_citations,
        grounding_ratio=grounding,
        faithfulness=faithfulness,
    )

    return ApiResponse.ok(
        AskResult(
            answer=answer,
            intent=str(state.get("intent") or ""),
            intent_confidence=float(state.get("confidence") or 0.0),
            citations=citations,
            sources=sources,
            grounding_ratio=grounding,
            faithfulness=faithfulness,
            passed_grounding=state.get("guardrail_passed", grounding >= 0.8),
            unsupported_claims=state.get("unsupported_claims") or [],
            guardrail_flags=list(state.get("guardrail_flags") or []),
            plan=list(state.get("plan") or []),
            reflections=[reflection] if reflection else [],
            timeline=timeline,
            retrieved=retrieved,
            debug=RetrievalDebug(
                crag_level=state.get("crag_level"),
                rewrite_round=int(state.get("rewrite_round") or 0),
                rewritten_query=str(state.get("rewritten_query") or ""),
                rerank_applied=bool(state.get("reranked")),
                top_k=payload.top_k or 0,
                trace=list(state.get("trace") or []),
            )
            if payload.debug
            else None,
            usage=usage.snapshot(),
            latency_ms=latency,
            history_id=history_id,
        )
    )


# ------------------------------------------------------------------ 流式（规格事件）
def _qa_events(node: str, update: dict[str, Any]) -> list[tuple[Event, Any]]:
    """把节点增量翻成 `/qa/stream` 的对外事件。

    与 `/chat/stream` 的 `chat._node_events` 是**并列**的两套映射，不是替换：
    那边细到 intent/plan/tool/reflection 每一种，这边按"前端要分几种样式渲染"
    归并成 thinking / retrieval / citation / source。改一套别动另一套。
    """
    if node == "intent":
        return [
            (
                Event.THINKING,
                {
                    "stage": "intent",
                    "intent": update.get("intent"),
                    "confidence": update.get("confidence", 0.0),
                    "target_papers": update.get("target_papers", []),
                },
            )
        ]
    if node == "clarify":
        return [(Event.THINKING, {"stage": "clarify", "question": update.get("clarify_question", "")})]
    if node == "planner":
        return [
            (
                Event.THINKING,
                {"stage": "plan", "steps": update.get("plan", []), "round": update.get("plan_round", 0)},
            )
        ]
    if node == "retriever":
        return [
            (
                Event.RETRIEVAL,
                {
                    "name": "retrieve_papers",
                    "status": "done",
                    "n": len(update.get("retrieved") or []),
                    "crag_level": update.get("crag_level"),
                },
            )
        ]
    if node == "tool":
        trace = update.get("trace") or []
        name = next((str(t.get("tool", "")) for t in trace if t.get("node") == "tool"), "")
        status = next((str(t.get("status", "")) for t in trace if t.get("node") == "tool"), "done")
        return [(Event.RETRIEVAL, {"name": name, "status": status})]
    if node == "replanner":
        return [(Event.THINKING, {"stage": "replan", "decision": update.get("replan_decision", "")})]
    if node == "reflector":
        refl = update.get("reflection") or {}
        return [
            (
                Event.THINKING,
                {
                    "stage": "reflection",
                    "round": refl.get("round", 0),
                    "scores": refl.get("scores", {}),
                    "overall": refl.get("overall", 0.0),
                    "verdict": refl.get("verdict", ""),
                },
            )
        ]
    if node == "synthesizer":
        out: list[tuple[Event, Any]] = []
        for cite in update.get("citations", []) or []:
            out.append((Event.CITATION, cite))
            # source 帧只带"去哪儿看"的三元组（外加 bbox），前端拿它渲染 [paper_id:page:chunk_id] 徽章。
            # 三个 id 一律转成 int：state 里的 Citation 存的是字符串（要过 checkpointer 的
            # pickle 往返），而对外契约 SourceTraceOut 是 int —— 不统一，前端按 int 处理
            # 就会拿到 "7" 这种字符串，两类溯源长得不一样。
            out.append(
                (
                    Event.SOURCE,
                    {
                        "paper_id": _as_int(cite.get("paper_id")),
                        "page": _as_int(cite.get("page_start") or cite.get("page")),
                        "chunk_id": _as_int(cite.get("chunk_id")),
                        "bbox": cite.get("bbox"),
                        "title": cite.get("title", ""),
                        "quote": str(cite.get("quote", ""))[:280],
                        "confidence": float(cite.get("confidence") or cite.get("nli_score") or 0.0),
                    },
                )
            )
        return out
    if node == "guardrails":
        trace = update.get("trace") or []
        action = next((str(t.get("action", "")) for t in trace if t.get("node") == "guardrails"), "")
        if action == "block":
            # 硬拒绝用 error 帧收场（而不是 guardrail 帧 + done）：前端只认"流以 done 或 error 结束"
            return [(Event.ERROR, {"code": GuardrailBlockedError.code, "message": str(update.get("answer") or "")})]
        return [(Event.GUARDRAIL, {"action": action, "flags": update.get("guardrail_flags", [])})]
    return []


@router.post("/stream", summary="流式问答（SSE）", operation_id="ask_question_stream")
async def stream(payload: AskRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_frames(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 让 nginx 不缓冲
        },
    )


async def _stream_frames(payload: AskRequest) -> AsyncIterator[str]:
    """跑同一张 Agent 图，事件按 /qa 的规格名发（见 `_qa_events`）。

    落库复用 `chat` 的 `_open_turn` / `_close_turn`：会话与留痕的写法只此一份，
    在 qa 里再抄一遍只会让两边慢慢漂开（会话模型 = `agent_runs.session_id` 那套约定
    见 chat.py 模块头）。
    """
    from app.agents.graph import get_graph
    from app.api.v1 import chat

    started = time.perf_counter()
    merged: dict[str, Any] = {}
    session_id = f"qa-{uuid4().hex[:16]}"
    turn = chat.ChatRequest(query=payload.query, paper_ids=payload.paper_ids, intent=payload.intent)

    yield sse_comment("open")
    try:
        run_id = await chat._open_turn(session_id, turn)
        initial: dict[str, Any] = {
            "query": payload.query,
            "session_id": session_id,
            "target_papers": payload.paper_ids,
        }
        if payload.top_k:
            initial["top_k"] = payload.top_k
        if payload.intent:
            initial["intent"] = payload.intent

        async for mode, chunk in get_graph().astream(
            initial, config={"configurable": {"thread_id": session_id}}, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                if isinstance(chunk, dict) and chunk.get("type") == "token":
                    yield sse(Event.TOKEN, {"text": chunk.get("text", "")})
                continue
            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                merged.update(update)
                for event, data in _qa_events(str(node), update):
                    yield sse(event, data)

        latency = int((time.perf_counter() - started) * 1000)
        await chat._close_turn(session_id, run_id, turn, merged, latency)

        # 已经发过 error 帧的（防护硬拒绝）不再补 done —— 一条流只能有一种收尾
        if merged.get("guardrail_action") == "block":
            return

        usage = get_llm().usage
        yield done_event(
            grounding_ratio=merged.get("grounding_ratio"),
            citations=merged.get("citations") or [],
            usage={"total_tokens": usage.total_tokens, "calls": usage.calls},
            latency_ms=latency,
            extra=await timeline_done_extra(merged),
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要以 error 帧收尾，前端才不会卡
        logger.exception("流式问答失败 session={}", session_id)
        yield error_event(5000, f"{type(exc).__name__}: {exc}")


@router.post(
    "/retrieve",
    response_model=ApiResponse[list[RetrievedChunkOut]],
    summary="只检索（调参用）",
    operation_id="retrieve_chunks",
)
async def retrieve(payload: AskRequest, session: SessionDep) -> ApiResponse[list[RetrievedChunkOut]]:
    from app.rag.retriever import HybridRetriever

    chunks = await HybridRetriever().retrieve(payload.query, paper_ids=payload.paper_ids or None, top_k=payload.top_k)
    if not chunks:
        raise EmptyRetrievalError("没有检索到任何片段：库可能是空的，或问题与已入库论文无关")

    rows = [_chunk_out(c) for c in chunks]
    await _attach_titles(session, rows)
    return ApiResponse.ok(rows)


@router.post(
    "/trace", response_model=ApiResponse[TraceResult], summary="溯源校验（不生成）", operation_id="trace_answer"
)
async def trace(payload: TraceRequest, session: SessionDep) -> ApiResponse[TraceResult]:
    from app.rag.source_tracing import get_tracer

    stmt = select(Chunk)
    # 没给白名单就取前几块，至少能验出"纯幻觉引用"
    stmt = stmt.where(Chunk.id.in_(payload.chunk_ids)) if payload.chunk_ids else stmt.limit(8)
    chunks = (await session.execute(stmt)).scalars().all()

    report = get_tracer().trace(payload.answer, list(chunks))
    return ApiResponse.ok(
        TraceResult(
            grounding_ratio=report.grounding_ratio,
            sentences_total=report.sentences_total,
            sentences_supported=report.sentences_supported,
            terms_total=report.terms_total,
            terms_supported=report.terms_supported,
            passed=report.passed,
            phantom_markers=report.phantom_markers,
            unsupported_claims=report.unsupported_claims,
            uncited_claims=report.uncited_claims,
            number_mismatches=report.number_mismatches,
            citations=[
                CitationOut(
                    marker=c.marker,
                    chunk_id=int(c.chunk_id) if c.chunk_id not in (None, "") else None,
                    paper_id=int(c.paper_id) if c.paper_id not in (None, "") else None,
                    section=c.section,
                    page=c.page_start,
                    bbox=c.bbox,
                    quote=c.quote,
                    answer_span=c.answer_span,
                    nli_score=round(c.nli_score, 4),
                    confidence=round(c.confidence, 4),
                    verified=c.supported,
                    attribution_method=c.attribution_method,
                )
                for c in report.citations
            ],
            sources=[
                SourceTraceOut(
                    answer_span=s.answer_span,
                    chunk_id=int(s.chunk_id) if s.chunk_id not in (None, "") else None,
                    paper_id=int(s.paper_id) if s.paper_id not in (None, "") else None,
                    page=s.page,
                    bbox=s.bbox,
                    confidence=round(s.confidence, 4),
                    attribution_method=s.attribution_method,
                )
                for s in report.source_traces()
            ],
        )
    )


@router.get(
    "/history",
    response_model=ApiResponse[list[QAHistoryOut]],
    summary="问答历史（含溯源）",
    operation_id="get_qa_history",
)
async def history(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200, description="返回条数")] = 20,
) -> ApiResponse[list[QAHistoryOut]]:
    rows = (await session.execute(select(QAHistory).order_by(QAHistory.created_at.desc()).limit(limit))).scalars().all()
    return ApiResponse.ok([QAHistoryOut.model_validate(r) for r in rows])


__all__ = [
    "build_source_traces",
    "build_sources",
    "build_timeline",
    "locate_answer_spans",
    "persist_qa_history",
    "router",
    "year_from_arxiv",
]
