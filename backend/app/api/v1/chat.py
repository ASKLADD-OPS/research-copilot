"""对话：SSE 流式入口 + 会话历史。

SSE 事件协议见 `app.llm.streaming` 模块头（前端 `composables/useChatStream.ts` 按此消费，
解帧后由 `stores/chat.ts` 分发到 UI）。

会话模型（阶段 1 没有 conversations / messages 表）
---------------------------------------------------
- **会话** = `agent_runs.session_id`。同一轮对话里的多次提问共享一个 session_id，
  左侧历史列表就是按它 group by 出来的。
- **一轮对话的对话内容** = 该 run 的 `steps`：开头一条 `{"node": "user"}`，
  结尾一条 `{"node": "synthesizer"}` 带答案与引用。
- **留痕** = `qa_history`（含 sources 溯源数组），供审计与指标统计，不参与会话渲染。

流式落库自己开 session（`session_scope`），不复用请求级依赖 ——
流式响应期间请求依赖的生命周期不可靠，用自己的事务最稳。落库全程**尽力而为**：
数据库不可用时答案照常吐出来，这是踩过的坑（详见 MEMORY 的 0c561e9）。
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
from app.db.bootstrap import ensure_default_user
from app.db.session import session_scope
from app.llm.client import get_llm
from app.llm.streaming import Event, done_event, error_event, sse, sse_comment
from app.models import AgentRun
from app.schemas import ApiResponse, Page, PageMeta

router = APIRouter(prefix="/chat", tags=["对话"])


# ------------------------------------------------------------------ 本地模型
# 只在这个模块用，不值得进公共 schemas —— 免得把「流式对话」的概念泄漏到全项目。
class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    paper_ids: list[int] = Field(default_factory=list)
    intent: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    turns: int = 0
    last_intent: str | None = None
    created_at: Any = None
    updated_at: Any = None


class MessageOut(BaseModel):
    id: int
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
        name = next((str(t.get("tool", "")) for t in (update.get("trace") or []) if t.get("node") == "tool"), "")
        status = next(
            (str(t.get("status", "")) for t in (update.get("trace") or []) if t.get("node") == "tool"), "done"
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
            (str(t.get("action", "")) for t in (update.get("trace") or []) if t.get("node") == "guardrails"), ""
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
    session_id = payload.conversation_id or f"chat-{uuid4().hex[:16]}"

    yield sse_comment("open")
    try:
        run_id = await _open_turn(session_id, payload)

        graph = get_graph()
        initial: dict[str, Any] = {
            "query": payload.query,
            "session_id": session_id,
            "target_papers": payload.paper_ids,
        }
        if payload.intent:
            initial["intent"] = payload.intent

        async for mode, chunk in graph.astream(
            initial,
            config={"configurable": {"thread_id": session_id}},
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
        await _close_turn(session_id, run_id, payload, merged, latency)

        usage = get_llm().usage
        yield done_event(
            grounding_ratio=merged.get("grounding_ratio"),
            citations=merged.get("citations") or [],
            usage={"total_tokens": usage.total_tokens, "calls": usage.calls},
            latency_ms=latency,
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都要以 error 帧收尾，前端才不会卡
        logger.exception("流式对话失败 session={}", session_id)
        yield error_event(5000, f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ 落库
async def _open_turn(session_id: str, payload: ChatRequest) -> int | None:
    """建一条 running 的 agent_run，把用户这一问记进 steps。返回 run id（失败返回 None）。"""
    try:
        async with session_scope() as session:
            user_id = await ensure_default_user(session)
            run = AgentRun(
                user_id=user_id,
                session_id=session_id,
                intent=payload.intent,
                mode="pipeline",
                status="running",
                steps=[
                    {
                        "node": "user",
                        "content": payload.query,
                        "paper_ids": [int(p) for p in payload.paper_ids],
                    }
                ],
            )
            session.add(run)
            await session.flush()
            return int(run.id)
    except Exception as exc:  # noqa: BLE001 - 存储层任何故障都不该中断问答
        logger.warning("本轮运行记录落库失败，以无痕模式继续：{}", exc)
        return None


async def _close_turn(
    session_id: str, run_id: int | None, payload: ChatRequest, merged: dict[str, Any], latency_ms: int
) -> None:
    """收尾：更新 agent_run（轨迹/意图/耗时）+ 落一条 qa_history。全程尽力而为。"""
    usage = get_llm().usage
    answer = str(merged.get("answer") or "")
    citations = list(merged.get("citations") or [])
    reflection = merged.get("reflection") or {}

    if run_id is not None:
        try:
            async with session_scope() as session:
                run = await session.get(AgentRun, run_id)
                if run is not None:
                    steps = list(run.steps or [])
                    steps.extend(t for t in (merged.get("trace") or []) if isinstance(t, dict))
                    steps.append(
                        {
                            "node": "synthesizer",
                            "content": answer,
                            "citations": citations,
                            "grounding_ratio": merged.get("grounding_ratio"),
                            "latency_ms": latency_ms,
                        }
                    )
                    run.steps = steps
                    run.intent = merged.get("intent") or run.intent
                    run.status = "succeeded"
                    run.tokens_used = usage.total_tokens
        except Exception as exc:  # noqa: BLE001
            logger.warning("运行记录收尾失败（回答已正常返回）：{}", exc)

    try:
        from app.api.v1.qa import persist_qa_history

        default_user = await ensure_default_user()
        await persist_qa_history(
            user_id=default_user,
            question=payload.query,
            answer=answer,
            intent=merged.get("intent"),
            paper_ids=[int(p) for p in (payload.paper_ids or merged.get("target_papers") or [])],
            citations=citations,
            grounding_ratio=merged.get("grounding_ratio"),
            faithfulness=(reflection.get("scores") or {}).get("faithfulness") if reflection else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("qa_history 落库失败（回答已正常返回）：{}", exc)


# ------------------------------------------------------------------ 历史
@router.get("/conversations", response_model=ApiResponse[Page[ConversationOut]], summary="会话列表")
async def list_conversations(session: SessionDep, page: PageDep) -> ApiResponse[Page[ConversationOut]]:
    """按 `agent_runs.session_id` 聚合。这是阶段 1 没有 conversations 表后的替代方案。"""
    grouped = (
        await session.execute(
            select(
                AgentRun.session_id,
                func.count().label("turns"),
                func.min(AgentRun.created_at).label("created_at"),
                func.max(AgentRun.created_at).label("updated_at"),
            )
            .group_by(AgentRun.session_id)
            .order_by(func.max(AgentRun.created_at).desc())
            .offset(page.offset)
            .limit(page.page_size)
        )
    ).all()
    total = int(
        (
            await session.execute(select(func.count(func.distinct(AgentRun.session_id))).select_from(AgentRun))
        ).scalar_one()
    )

    items: list[ConversationOut] = []
    for sid, turns, created_at, updated_at in grouped:
        first = (
            await session.execute(
                select(AgentRun).where(AgentRun.session_id == sid).order_by(AgentRun.created_at).limit(1)
            )
        ).scalar_one_or_none()
        title = _first_question(first) or f"会话 {sid[:8]}"
        last = (
            await session.execute(
                select(AgentRun.intent)
                .where(AgentRun.session_id == sid, AgentRun.intent.is_not(None))
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(
            ConversationOut(
                id=sid,
                title=title[:60],
                turns=int(turns),
                last_intent=last,
                created_at=created_at,
                updated_at=updated_at,
            )
        )

    return ApiResponse.ok(
        Page[ConversationOut](
            items=items,
            meta=PageMeta(
                total=total, page=page.page, page_size=page.page_size, has_next=page.offset + len(items) < total
            ),
        )
    )


def _first_question(run: AgentRun | None) -> str:
    for step in run.steps or [] if run else []:
        if step.get("node") == "user":
            return str(step.get("content") or "")
    return ""


@router.get("/conversations/{session_id}", response_model=ApiResponse[ConversationDetail], summary="会话详情")
async def get_conversation(session_id: str, session: SessionDep) -> ApiResponse[ConversationDetail]:
    runs = (
        (await session.execute(select(AgentRun).where(AgentRun.session_id == session_id).order_by(AgentRun.created_at)))
        .scalars()
        .all()
    )
    if not runs:
        raise NotFoundError(f"会话不存在: {session_id}")

    messages: list[MessageOut] = []
    for run in runs:
        for step in run.steps or []:
            node = step.get("node")
            if node == "user":
                messages.append(
                    MessageOut(
                        id=run.id, role="user", content=str(step.get("content") or ""), created_at=run.created_at
                    )
                )
            elif node == "synthesizer":
                messages.append(
                    MessageOut(
                        id=run.id,
                        role="assistant",
                        content=str(step.get("content") or ""),
                        intent=run.intent,
                        citations=list(step.get("citations") or []),
                        grounding_ratio=step.get("grounding_ratio"),
                        created_at=run.created_at,
                    )
                )

    return ApiResponse.ok(
        ConversationDetail(
            id=session_id,
            title=(_first_question(runs[0]) or f"会话 {session_id[:8]}")[:60],
            turns=len(runs),
            last_intent=runs[-1].intent,
            created_at=runs[0].created_at,
            updated_at=runs[-1].created_at,
            messages=messages,
        )
    )


@router.delete("/conversations/{session_id}", response_model=ApiResponse[dict[str, Any]], summary="删除会话")
async def delete_conversation(session_id: str, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    result = await session.execute(delete(AgentRun).where(AgentRun.session_id == session_id))
    await session.commit()
    if not result.rowcount:
        raise NotFoundError(f"会话不存在: {session_id}")
    return ApiResponse.ok({"deleted": session_id, "runs": result.rowcount})


__all__ = ["router"]
