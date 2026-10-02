"""SSE 事件协议测试。

前端 `useChatStream` 是按这个协议手写解析的，所以帧格式一旦漂了，
表现是"页面一直转圈"而不是报错 —— 很难查。这里把帧格式钉死。

另外钉住一条硬约定：**任何一条流都必须以 done 或 error 收尾**，
前端靠它关连接与 loading 态。
"""

from __future__ import annotations

import json

import pytest

from app.llm.streaming import (
    Event,
    done_event,
    encode_events,
    error_event,
    sse,
    sse_comment,
    token_events,
)


def parse_frame(frame: str) -> tuple[str, str]:
    """把一帧拆成 (event, data)。与前端 parseFrame 的逻辑保持一致。"""
    assert frame.endswith("\n\n"), repr(frame)
    name = ""
    data_lines: list[str] = []
    for line in frame[:-2].split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value
        elif field == "data":
            data_lines.append(value)
    return name, "\n".join(data_lines)


@pytest.mark.unit
def test_event_enum_covers_the_documented_protocol():
    """三套协议并存，各有各的消费方。

    - 细粒度那套给 `/chat/stream`（intent/plan/tool/reflection/...）；
    - 归并那套给 `/qa/stream`（thinking/retrieval/source）——归并（thinking / retrieval / source）
      不是"多余的事件名"：前端要按事件名分派四种渲染样式，把 intent/plan/replan/reflection
      四种轨迹各自发一遍，客户端就得知道这四种其实是同一类。归并在服务端做一次，
      比在客户端做四次判断划算；
    - 探索轨迹那套给 `/tools/explore`（thought/action/observation/progress/recommend），
      见 `app/agents/explore.py` 的模块头。前端 `pages/app/tools/index.vue` 按事件名
      分派成 Thought / Action / Observation 三段式。

    这个断言的作用是"新增事件必须在这里登记"——协议是前后端之间唯一的契约，
    悄悄多一个事件名意味着有一端在猜。
    """
    assert {e.value for e in Event} == {
        # /chat/stream
        "intent",
        "clarify",
        "plan",
        "tool",
        "token",
        "citation",
        "reflection",
        "replan",
        "guardrail",
        "error",
        "done",
        # /qa/stream 的归并事件
        "thinking",
        "retrieval",
        "source",
        # /tools/explore 的探索轨迹
        "thought",
        "action",
        "observation",
        "progress",
        "recommend",
    }


@pytest.mark.unit
def test_done_event_carries_extra_payload():
    """收尾帧是唯一"一定发得出去"的帧，跨篇时间轴等附加数据挂在这里。"""
    name, data = parse_frame(done_event(grounding_ratio=0.9, extra={"timeline": [{"year": 2021}]}))
    assert name == "done"
    assert json.loads(data)["timeline"] == [{"year": 2021}]
    # 不传 extra 时不能凭空多出 timeline 字段
    assert "timeline" not in json.loads(parse_frame(done_event(grounding_ratio=0.9))[1])


@pytest.mark.unit
def test_sse_frame_shape():
    frame = sse(Event.TOKEN, {"text": "你好"})
    name, data = parse_frame(frame)
    assert name == "token"
    assert json.loads(data) == {"text": "你好"}


@pytest.mark.unit
def test_sse_keeps_chinese_unreadable_free():
    """ensure_ascii=False：中文按原样传输，省带宽也便于抓包看日志。"""
    assert "你好" in sse(Event.TOKEN, {"text": "你好"})


@pytest.mark.unit
def test_sse_multiline_payload_gets_data_prefix_per_line():
    """SSE 规范要求每行都加 data: 前缀，漏掉的话正文会在换行处被截断。"""
    frame = sse(Event.TOKEN, "第一行\n第二行")
    assert frame.count("data: ") == 2
    name, data = parse_frame(frame)
    assert name == "token"
    assert data == "第一行\n第二行"


@pytest.mark.unit
def test_sse_accepts_plain_string_event_name():
    frame = sse("custom", {"a": 1})
    assert frame.startswith("event: custom\n")


@pytest.mark.unit
def test_sse_comment_is_a_valid_keepalive_frame():
    """保活帧必须是注释帧（: 开头），不能被前端当成事件解析。"""
    frame = sse_comment("open")
    assert frame == ": open\n\n"
    assert parse_frame(frame) == ("", "")


@pytest.mark.unit
def test_done_event_carries_grounding_and_counts_citations():
    frame = done_event(
        grounding_ratio=0.8712,
        citations=[{"a": 1}, {"b": 2}],
        usage={"total": 1234},
        latency_ms=4567,
    )
    name, data = parse_frame(frame)
    payload = json.loads(data)
    assert name == "done"
    assert payload["grounding_ratio"] == 0.8712
    assert payload["citation_count"] == 2
    assert payload["usage"] == {"total": 1234}
    assert payload["latency_ms"] == 4567


@pytest.mark.unit
def test_done_event_omits_absent_fields():
    """没给的字段不发空值 —— 前端用 `?? null` 兜底，多发只会让 payload 变脏。"""
    payload = json.loads(parse_frame(done_event())[1])
    assert payload == {}


@pytest.mark.unit
def test_error_event_shape():
    name, data = parse_frame(error_event(3002, "检索不到相关资料"))
    assert name == "error"
    assert json.loads(data)["code"] == 3002
    assert json.loads(data)["message"] == "检索不到相关资料"


@pytest.mark.unit
async def test_token_events_merges_and_flushes_tail():
    async def source():
        for piece in ["a", "b", "c", "d", "e"]:
            yield piece

    events = [evt async for evt in token_events(source(), chunk_size=2)]
    assert [e for e, _ in events] == [Event.TOKEN] * 3
    assert "".join(d["text"] for _, d in events) == "abcde"
    # 2 + 2 + 1：尾巴不足一块也要发出去，否则最后几个字会丢
    assert [len(d["text"]) for _, d in events] == [2, 2, 1]


@pytest.mark.unit
async def test_token_events_empty_stream():
    async def empty():
        if False:  # pragma: no cover
            yield ""

    assert [e async for e in token_events(empty())] == []


@pytest.mark.unit
async def test_encode_events_frames_each_item():
    async def source():
        yield Event.TOKEN, {"text": "x"}
        yield Event.DONE, {"grounding_ratio": 0.9}

    frames = [f async for f in encode_events(source())]
    assert len(frames) == 2
    assert parse_frame(frames[0])[0] == "token"
    assert parse_frame(frames[1])[0] == "done"


@pytest.mark.unit
def test_every_frame_is_double_newline_terminated():
    for frame in (
        sse(Event.TOKEN, {"text": "a"}),
        sse_comment("ping"),
        done_event(grounding_ratio=1.0),
        error_event(1, "boom"),
    ):
        assert frame.endswith("\n\n")
