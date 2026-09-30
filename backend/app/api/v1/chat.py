"""对话：SSE 流式入口 + 会话历史。

SSE 事件协议见 `app.llm.streaming` 模块头（前端 `composables/useChatStream.ts` 按此消费，
解帧后由 `stores/chat.ts` 分发到 UI）。
流式实现要点：
- `graph.astream(..., stream_mode=["updates", "custom"])`：`updates` 给节点级进度，
  `custom` 给 synthesizer 逐 token 推送的正文；
- 会话落库**自己开 session**（`session_scope`），不复用请求级依赖 ——
  流式响应期间请求依赖的生命周期不可靠，用自己的事务最稳。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.api.deps import PageDep, SessionDep
from app.core.errors import NotFoundError
from app.db.session import session_scope
from app.llm.client import get_llm
from app.llm.streaming import Event, done_event, error_event, sse, sse_comment
from app.models import Conversation, Message
from app.schemas import ApiResponse, Page, PageMeta

router = APIRouter(prefix="/chat", tags=["对话"])

# ------------------------------------------------------------------ 本地模型
# 只在这个模块用，不值得进公共 schemas —— 免得把「流式对话」的概念泄漏到全项目。


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    paper_ids: list[str] = Field(default_factory=list)
    intent: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    paper_ids: list[str] = Field(default_factory=list)
    message_count: int = 0
    created_at: Any = None
    updated_at: Any = None


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    intent: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    grounding_ratio: float | None = None
    created_at: Any = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


# ------------------------------------------------------------------ 事件映射
def _node_events(node: str, update: dict[str, Any]) -> list[tuple[Event, Any]]:
    """把节点返回的 state 增量翻译成 SSE 事件。"""
    out: list[tuple[Event, Any]] = []
    if node == "intent":
        out.append(
            (
                Event.INTENT,
                {
                    "intent": update.get("intent"),
                    "confidence": update.get("confidence", 0.0),
                    "target_papers": update.get("target_papers", []),
                    "slot_filling": update.get("slot_filling", {}),
                },
            )
        )
    elif node == "clarify":
        out.append((Event.CLARIFY, {"question": update.get("clarify_question", "")}))
    elif node == "planner":
        out.append((Event.PLAN, {"steps": update.get("plan", []), "round": update.get("plan_round", 0)}))
    elif node == "retriever":
        out.append(
            (
                Event.TOOL,
                {
                    "name": "retrieve_papers",
                    "status": "done",
                    "crag_level": update.get("crag_level"),
                    "n": len(update.get("retrieved", []) or []),
                },
            )
        )
    elif node == "tool":
        name = next(
            (str(t.get("tool", "")) for t in (update.get("trace") or []) if t.get("node") == "tool"),
            "",
        )
        status = next(
            (str(t.get("status", "")) for t in (update.get("trace") or []) if t.get("node") == "tool"),
            "done",
        )
        out.append((Event.TOOL, {"name": name, "status": status}))
    elif node == "replanner":
        out.append((Event.REPLAN, {"decision": update.get("replan_decision", ""), "plan": update.get("plan", [])}))
    elif node == "reflector":
        refl = update.get("reflection") or {}
        out.append(
            (
                Event.REFLECTION,
                {
                    "round": refl.get("round", 0),
                    "scores": refl.get("scores", {}),
                    "overall": refl.get("overall", 0.0),
                    "verdict": refl.get("verdict", ""),
                    "critique": refl.get("critique", ""),
                },
            )
        )
    elif node == "synthesizer":
        for cite in update.get("citations", []) or []:
            out.append((Event.CITATION, cite))
    elif node == "guardrails":
        action = next(
            (str(t.get("action", "")) for t in (update.get("trace") or []) if t.get("node") == "guardrails"),
            "",
        )
        out.append((Event.GUARDRAIL, {"action": action, "flags": update.get("guardrail_flags", [])}))
    return out


# ------------------------------------------------------------------ 流式入口
@router.post("/stream", summary="流式对话（SSE）")
async def stream(payload: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_frames(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 让 nginx 不缓冲
        },
    )


async def _stream_frames(payload: ChatRequest) -> AsyncIterator[str]:
    from app.agents.graph import get_graph

    started = time.perf_counter()
    merged: dict[str, Any] = {}
    conv_id = payload.conversation_id

    yield sse_comment("open")
    try:
        conv_id = await _prepare_turn(conv_id, payload)

        graph = get_graph()
        initial: dict[str, Any] = {
            "query": payload.query,
            "session_id": conv_id,
            "target_papers": payload.paper_ids,
        }
        if payload.intent:
            initial["intent"] = payload.intent

        async for mode, chunk in graph.astream(
            initial,
            config={"configurable": {"thread_id": conv_id}},
            stream_mode=["updates", "custom"],
        ):
            if mode == "custom":
                if isinstance(chunk, dict) and chunk.get("type") == "token":
                    yield sse(Event.TOKEN, {"text": chunk.get("text", "")})
                continue
            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                merged.update(update)
                for event, data in _node_events(str(node), update):
                    yield sse(event, data)

        latency = int((time.perf_counter() - started) * 1000)
        await _finish_turn(conv_id, merged, latency)

        usage = get_llm().usage
        yield done_event(
            grounding_ratio=merged.get("grounding_ratio"),
            citations=merged.get("citations") or [],
            usage={"total_tokens": usage.total_tokens, "calls": usage.calls},
            latency_ms=latency,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要以 error 帧收尾，前端才不会卡
        logger.exception("流式对话失败 conv={}", conv_id)
        yield error_event(5000, f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ 历史
async def _prepare_turn(conv_id: str | None, payload: ChatRequest) -> str:
    """建会话（如无）+ 落用户消息，返回会话 id。

    持久化是**尽力而为**的：数据库不可用时不能把整轮回答一起废掉。
    Agent 图自己用的是可降级的 checkpointer（AsyncPostgresSaver → MemorySaver），
    没有 Postgres 照样能跑出答案 —— 之前这里直接裸写库，导致 DB 一挂，
    整个 `/chat/stream` 连一个 token 都吐不出来（先抛连接超时，前端只能收到 error 帧）。
    现在失败只记警告并退化成临时会话。
    """
    try:
        async with session_scope() as session:
            conversation: Conversation | None = None
            if conv_id:
                conversation = await session.get(Conversation, conv_id)
            if conversation is None:
                conversation = Conversation(
                    title=payload.query[:60],
                    paper_ids=payload.paper_ids,
                )
                session.add(conversation)
                await session.flush()
            session.add(Message(conversation_id=conversation.id, role="user", content=payload.query))
            return conversation.id
    except Exception as exc:  # noqa: BLE001 - 存储层任何故障都不该中断问答
        logger.warning("会话落库失败，本轮以临时会话继续：{}", exc)
        # 复用调用方给的 id（若在），否则现造一个 —— LangGraph 的 thread_id 不能为空
        return conv_id or f"ephemeral-{uuid4().hex}"


async def _finish_turn(conv_id: str, merged: dict[str, Any], latency_ms: int) -> None:
    """落助手消息（含引用与溯源指标）。同样是尽力而为，失败不抛。"""
    usage = get_llm().usage
    reflections = [merged["reflection"]] if merged.get("reflection") else []
    try:
        async with session_scope() as session:
            session.add(
                Message(
                    conversation_id=conv_id,
                    role="assistant",
                    content=str(merged.get("answer") or ""),
                    intent=merged.get("intent"),
                    intent_confidence=merged.get("confidence"),
                    slot_filling=merged.get("slot_filling") or {},
                    target_papers=list(merged.get("target_papers") or []),
                    citations=list(merged.get("citations") or []),
                    grounding_ratio=merged.get("grounding_ratio"),
                    unsupported_claims=list(merged.get("unsupported_claims") or []),
                    reflections=reflections,
                    plan={"steps": merged.get("plan", [])},
                    tool_calls=[t for t in merged.get("trace", []) if t.get("node") in {"tool", "retriever"}],
                    latency_ms=latency_ms,
                    token_usage={"total_tokens": usage.total_tokens, "calls": usage.calls},
                )
            )
            # 首句回答后把默认标题换掉（"新对话" 这种没有信息量）
            conversation = await session.get(Conversation, conv_id)
            if conversation is not None and conversation.title in {"", "新对话"}:
                conversation.title = str(merged.get("answer") or "")[:60] or conversation.title
    except Exception as exc:  # noqa: BLE001
        logger.warning("助手消息落库失败（回答已正常返回）：{}", exc)


@router.get("/conversations", response_model=ApiResponse[Page[ConversationOut]], summary="会话列表")
async def list_conversations(session: SessionDep, page: PageDep) -> ApiResponse[Page[ConversationOut]]:
    total = int((await session.execute(select(func.count()).select_from(Conversation))).scalar_one())
    rows = (
        (
            await session.execute(
                select(Conversation).order_by(Conversation.updated_at.desc()).offset(page.offset).limit(page.page_size)
            )
        )
        .scalars()
        .all()
    )
    counts = dict(
        (
            await session.execute(
                select(Message.conversation_id, func.count())
                .where(Message.conversation_id.in_([r.id for r in rows] or [""]))
                .group_by(Message.conversation_id)
            )
        ).all()
    )
    return ApiResponse.ok(
        Page[ConversationOut](
            items=[
                ConversationOut(
                    id=r.id,
                    title=r.title,
                    paper_ids=list(r.paper_ids or []),
                    message_count=int(counts.get(r.id, 0)),
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in rows
            ],
            meta=PageMeta(total=total, page=page.page, page_size=page.page_size),
        )
    )


@router.get("/conversations/{conv_id}", response_model=ApiResponse[ConversationDetail], summary="会话详情")
async def get_conversation(conv_id: str, session: SessionDep) -> ApiResponse[ConversationDetail]:
    conversation = await session.get(Conversation, conv_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在: {conv_id}")
    messages = (
        (await session.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        ConversationDetail(
            id=conversation.id,
            title=conversation.title,
            paper_ids=list(conversation.paper_ids or []),
            message_count=len(messages),
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=[
                MessageOut(
                    id=m.id,
                    role=m.role,
                    content=m.content,
                    intent=m.intent,
                    citations=list(m.citations or []),
                    grounding_ratio=m.grounding_ratio,
                    created_at=m.created_at,
                )
                for m in messages
            ],
        )
    )


@router.delete("/conversations/{conv_id}", response_model=ApiResponse[dict[str, Any]], summary="删除会话")
async def delete_conversation(conv_id: str, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    conversation = await session.get(Conversation, conv_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在: {conv_id}")
    await session.execute(delete(Message).where(Message.conversation_id == conv_id))
    await session.delete(conversation)
    await session.commit()
    return ApiResponse.ok({"deleted": conv_id})
