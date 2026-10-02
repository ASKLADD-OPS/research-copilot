"""SSE 流式工具。

事件协议（前端 `composables/useChatStream.ts` 按此消费）：

    event: token      data: {"text": "..."}              增量正文
    event: intent     data: {"intent": "...", "confidence": 0.92, ...}
    event: plan       data: {"steps": [...]}             规划好后的计划
    event: tool       data: {"name": "...", "status": "running|done", ...}
    event: citation   data: {"chunk_id": "...", ...}     每确认一条引用推一条
    event: reflection data: {"round": 1, "scores": {...}, "verdict": "..."}
    event: guardrail  data: {"layer": "...", "action": "..."}  四层防护的处置记录
    event: error      data: {"code": 3002, "message": "..."}
    event: done       data: {"grounding_ratio": 0.87, "usage": {...}}

`/qa/stream` 另有一套更粗的对外事件名（规格约定），由 `app/api/v1/qa.py` 的
`_qa_events` 产出 —— 它把上面这些细粒度事件归并成前端真正要分四种样式渲染的类别：

    event: thinking  data: {"stage": "intent|plan|reflection|replan", ...}
    event: retrieval data: {"name": "...", "status": "...", "n": 8, "crag_level": "..."}
    event: citation  data: {"marker": 1, "chunk_id": 77, ...}
    event: source    data: {"paper_id": 3, "page": 5, "chunk_id": 77, "bbox": [...]}
    event: token     data: {"text": "..."}
    event: done      data: {"grounding_ratio": 0.87, "timeline": [...]}

两套是并列的，`/chat/stream` 用上面那套、`/qa/stream` 用下面这套 —— 不要往同一条流里
混着发，前端按事件名分派，混发会让同一段内容被渲染两遍。

约定：**任何一条流都必须以 done 或 error 收尾**，前端据此关闭连接与 loading 态。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from enum import StrEnum
from typing import Any


class Event(StrEnum):
    INTENT = "intent"
    CLARIFY = "clarify"
    PLAN = "plan"
    TOOL = "tool"
    TOKEN = "token"
    CITATION = "citation"
    REFLECTION = "reflection"
    REPLAN = "replan"
    GUARDRAIL = "guardrail"
    ERROR = "error"
    DONE = "done"
    # ---- /qa/stream 的对外事件（见模块头第二段）----
    THINKING = "thinking"
    RETRIEVAL = "retrieval"
    SOURCE = "source"


def sse(event: Event | str, data: Any) -> str:
    """把一条事件编码成 SSE 帧。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    # data 里可能含换行（正文增量），SSE 要求逐行加 data: 前缀
    body = "\n".join(f"data: {line}" for line in payload.split("\n"))
    return f"event: {event}\n{body}\n\n"


def sse_comment(text: str) -> str:
    """注释帧。用于长任务期间保活（防代理 60s 掐连接）。"""
    return f": {text}\n\n"


async def encode_events(source: AsyncIterator[tuple[Event, Any]]) -> AsyncIterator[str]:
    """把 (事件, 数据) 异步流编码成 SSE 帧流。"""
    async for event, data in source:
        yield sse(event, data)


async def token_events(
    text_stream: AsyncIterator[str],
    *,
    chunk_size: int = 1,
) -> AsyncIterator[tuple[Event, Any]]:
    """把纯文本增量流包装成 token 事件流。chunk_size 用于合并过碎的分片，降低前端渲染压力。"""
    buf: list[str] = []
    async for piece in text_stream:
        buf.append(piece)
        if len(buf) >= chunk_size:
            yield Event.TOKEN, {"text": "".join(buf)}
            buf.clear()
    if buf:
        yield Event.TOKEN, {"text": "".join(buf)}


def done_event(
    *,
    grounding_ratio: float | None = None,
    citations: Iterable[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {}
    if grounding_ratio is not None:
        payload["grounding_ratio"] = round(grounding_ratio, 4)
    if citations is not None:
        payload["citation_count"] = len(list(citations))
    if usage:
        payload["usage"] = usage
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    # 收尾帧是唯一能保证"一定发出去"的帧，所以需要跟答案一起交付的附加数据
    # （如跨篇对比的时间轴）挂在这里，不另开一个可能被中途掐断的事件。
    if extra:
        payload.update(extra)
    return sse(Event.DONE, payload)


def error_event(code: int, message: str, data: Any = None) -> str:
    return sse(Event.ERROR, {"code": code, "message": message, "data": data})
