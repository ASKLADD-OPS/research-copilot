"""RAG 问答 API：`/qa/stream` 事件协议 + 跨篇时间轴 + 请求字段别名。

与 `test_qa_sources_api.py` 的分工：那边钉住 `/qa/ask` 的**响应体**（答案 + 溯源的
字段与数值），这边钉住三件它覆盖不到的事：

1. `/qa/stream` 的对外事件名（thinking / retrieval / citation / source /
   token / done）——前端按事件名分派渲染样式，事件名漂了页面就是"一直转圈"；
2. 跨篇对比的时间轴从哪来（arXiv 编号推年份，**不让模型复述年份**）；
3. 规格给的 `question` / `mode` 字段名与内部 `query` / `intent` 的别名兼容。

外部依赖的处理与 test_qa_sources_api 一致：假模型 + 假检索 + 旁路落库。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.rag.retriever import RetrievedChunk
from app.rag.source_tracing import SourceTracer

QUERY = "MoE 负载均衡的辅助损失系数是多少？"
CHUNK_TEXT = "MoE 负载均衡的辅助损失（auxiliary loss）系数取 0.01，作用于门控网络的负载分布。"
ANSWER = "负载均衡辅助损失的系数为 0.01 [1]。"
PAGE = 4
BBOX = [72.0, 120.0, 480.0, 143.0]


# ==================================================================== 假件


class FakeLLM:
    """按结构化 schema 的 title 分派：意图 → 计划 → 综合成文。"""

    def __init__(self, answer: str = ANSWER, intent: str = "single_paper_qa") -> None:
        self.answer = answer
        self.intent = intent

    def _node(self, messages: list[dict[str, Any]]) -> str:
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if '"title": "IntentResult"' in text:
            return "intent"
        if '"title": "PlanResult"' in text:
            return "plan"
        if '"title": "ReflectionResult"' in text:
            return "reflection"
        if '"title": "_LLMGrade"' in text:
            return "grade"
        return "synthesizer"

    def _payload(self, node: str) -> str:
        if node == "intent":
            return json.dumps(
                {
                    "intent": self.intent,
                    "confidence": 0.93,
                    "target_papers": [],
                    "slot_filling": {},
                    "reason": "test",
                },
                ensure_ascii=False,
            )
        if node == "plan":
            return json.dumps(
                {"steps": [{"idx": 1, "goal": "检索相关片段", "tool": "retrieve_papers", "status": "pending"}]},
                ensure_ascii=False,
            )
        if node == "reflection":
            return json.dumps(
                {"scores": dict.fromkeys(("faithfulness", "relevance", "coherence", "completeness"), 0.95)},
                ensure_ascii=False,
            )
        if node == "grade":
            return json.dumps({"relevance": 0.9, "reason": "test"}, ensure_ascii=False)
        return self.answer

    async def complete(self, role: Any, messages: list[dict[str, Any]], **extra: Any) -> str:
        return self._payload(self._node(messages))

    async def stream(self, role: Any, messages: list[dict[str, Any]], **extra: Any):
        text = await self.complete(role, messages)
        for i in range(0, len(text), 16):
            yield text[i : i + 16]


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        return FakeResult(self.rows)

    async def flush(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
def api(monkeypatch):
    """`install(intent="single_paper_qa")` → (client, llm)。"""
    from app.api.deps import get_session
    from app.api.v1 import chat as chat_api
    from app.llm.client import get_llm
    from app.main import app

    async def fake_session():
        yield FakeSession()

    monkeypatch.setitem(app.dependency_overrides, get_session, fake_session)

    with TestClient(app) as client:

        def install(intent: str = "single_paper_qa"):
            real = get_llm()
            llm = FakeLLM(intent=intent)
            monkeypatch.setattr(real, "complete", llm.complete)
            monkeypatch.setattr(real, "stream", llm.stream)

            chunks = [
                RetrievedChunk(
                    id=101,
                    paper_id=7,
                    content=CHUNK_TEXT,
                    section="Method",
                    page=PAGE,
                    bbox=BBOX,
                    score=0.9,
                    rerank_score=0.95,
                    sources=["dense", "sparse"],
                )
            ]

            class _FakeRetriever:
                def __init__(self, *args: Any, **kwargs: Any) -> None: ...

                async def retrieve(self, query: str, **kwargs: Any) -> list[RetrievedChunk]:
                    return list(chunks)

            monkeypatch.setattr("app.agents.nodes.retriever.HybridRetriever", _FakeRetriever)
            monkeypatch.setattr("app.rag.source_tracing.get_tracer", lambda: SourceTracer(enable_nli=False))

            async def no_titles(session: Any, rows: list) -> None: ...

            async def default_user(session: Any) -> int:
                return 1

            async def save_history(**kwargs: Any) -> int:
                return 42

            async def open_turn(session_id: str, payload: Any) -> int:
                return 1

            async def close_turn(*args: Any, **kwargs: Any) -> None: ...

            monkeypatch.setattr("app.api.v1.qa._attach_titles", no_titles)
            monkeypatch.setattr("app.api.v1.qa.ensure_default_user", default_user)
            monkeypatch.setattr("app.api.v1.qa.persist_qa_history", save_history)
            # 流式落库在这条链路上是旁路：真跑会连 PG（单测没有），补掉即可
            monkeypatch.setattr(chat_api, "_open_turn", open_turn)
            monkeypatch.setattr(chat_api, "_close_turn", close_turn)
            return client, llm

        yield install


def collect_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """按空行切帧并解析。与前端 `useChatStream.parseFrame` 同一套规则。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        name = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            line = line.replace("\r", "")
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            out.append((name, json.loads("\n".join(data_lines))))
    return out


def first(frames: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    """取某一类事件的第一帧。

    **不能** `dict(frames)`：同名事件会重复出现（thinking 至少三次、token 几十次），
    转字典只会留下最后一帧 —— 于是"意图"那一帧被"评审"那帧顶掉。
    """
    return next(data for evt, data in frames if evt == name)


# ==================================================================== 请求别名


@pytest.mark.unit
def test_ask_request_accepts_spec_aliases():
    """规格里叫 `question` / `mode`，内部既有实现叫 `query` / `intent`，两个都收。"""
    from app.schemas import AskRequest

    by_spec = AskRequest.model_validate({"question": "系数是多少", "mode": "cross_paper_reasoning"})
    assert by_spec.query == "系数是多少"
    assert by_spec.intent == "cross_paper_reasoning"

    # 内部名照旧可用（既有的图、提示词、测试全依赖它）
    by_internal = AskRequest.model_validate({"query": "系数是多少", "intent": "single_paper_qa"})
    assert (by_internal.query, by_internal.intent) == ("系数是多少", "single_paper_qa")


# ==================================================================== 年份与时间轴


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arxiv_id", "expected"),
    [
        ("2301.12345", 2023),  # 新式 YYMM
        ("2301.12345v3", 2023),  # 带版本号
        ("cs/0701001", 2007),  # 老式 分类/YYMMNNN
        ("cs.CL/0701001", 2007),  # 老式分类名可以带点
        ("9912.0001", 1999),  # arXiv 1991 开张：91-99 归 1900s
        ("9101.0001", 1991),
        ("2501.0001", 2025),
        ("1234.5678", None),  # 月份 34 非法 —— 宁可没有年份，也不编一个
        ("oops", None),
        ("", None),
        (None, None),
    ],
)
def test_year_from_arxiv(arxiv_id, expected):
    from app.api.v1.qa import year_from_arxiv

    assert year_from_arxiv(arxiv_id) == expected


@pytest.mark.unit
async def test_build_timeline_sorts_by_year_with_unknown_last():
    """时间轴按年份升序；推不出年份的沉底（不编年份）。"""
    from app.api.v1.qa import build_timeline

    rows = [(1, "晚的", "2301.00001"), (2, "早的", "cs/0701001"), (3, "无编号", None)]
    session = FakeSession(rows)
    chunks = [{"paper_id": 1}, {"paper_id": 1}, {"paper_id": 2}, {"paper_id": 3}]

    items = await build_timeline(session, chunks)
    assert [i.paper_id for i in items] == [2, 1, 3]
    assert [i.year for i in items] == [2007, 2023, None]
    assert [i.chunks for i in items] == [1, 2, 1]  # 命中片段数按 paper 聚合
    assert items[0].arxiv_id == "cs/0701001"


@pytest.mark.unit
async def test_timeline_done_extra_only_for_cross_paper(monkeypatch):
    """单篇问答不排时间轴（排出来只有一个点，白查一次库）。"""
    from app.api.v1.qa import timeline_done_extra

    monkeypatch.setattr(
        "app.api.v1.qa.session_scope",
        lambda: pytest.fail("单篇问答不该碰数据库"),
    )
    assert await timeline_done_extra({"intent": "single_paper_qa", "retrieved": [{"paper_id": 1}]}) == {"timeline": []}


@pytest.mark.unit
async def test_timeline_done_extra_survives_failure(monkeypatch):
    """时间轴算不出来，也不能让整条回答废掉（收尾帧必须发得出去）。"""
    from app.api.v1.qa import timeline_done_extra

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr("app.api.v1.qa.session_scope", boom)
    extra = await timeline_done_extra({"intent": "cross_paper_reasoning", "retrieved": [{"paper_id": 1}]})
    assert extra == {"timeline": []}


@pytest.mark.unit
async def test_timeline_done_extra_serialises_items(monkeypatch):
    """跨篇对比时给出可 JSON 化的时间轴（前端直接吃 model_dump 的结果）。"""
    from app.api.v1.qa import timeline_done_extra
    from app.schemas import TimelineItem

    class _Ctx:
        async def __aenter__(self) -> Any:
            return FakeSession()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    async def fake_timeline(session: Any, chunks: list[Any]) -> list[TimelineItem]:
        return [TimelineItem(paper_id=2, title="早的", year=2007, arxiv_id="cs/0701001", chunks=1)]

    monkeypatch.setattr("app.api.v1.qa.session_scope", lambda: _Ctx())
    monkeypatch.setattr("app.api.v1.qa.build_timeline", fake_timeline)

    extra = await timeline_done_extra({"intent": "cross_paper_reasoning", "retrieved": [{"paper_id": 2}]})
    assert extra == {
        "timeline": [{"paper_id": 2, "title": "早的", "year": 2007, "arxiv_id": "cs/0701001", "chunks": 1}]
    }
    json.dumps(extra)  # 必须能进 SSE（JSON 序列化）


# ==================================================================== 事件映射


@pytest.mark.unit
def test_qa_events_maps_each_node_to_spec_events():
    """节点 → 对外事件名的映射表。改这里等于改前端的渲染分派。"""
    from app.api.v1.qa import _qa_events
    from app.llm.streaming import Event

    assert _qa_events("intent", {"intent": "single_paper_qa", "confidence": 0.9})[0][0] == Event.THINKING
    assert _qa_events("clarify", {"clarify_question": "你指哪篇？"})[0][1]["stage"] == "clarify"
    assert _qa_events("planner", {"plan": [{"idx": 1}]})[0][1]["stage"] == "plan"
    assert _qa_events("replanner", {"replan_decision": "补齐对比"})[0][1]["stage"] == "replan"
    assert _qa_events("reflector", {"reflection": {"scores": {"faithfulness": 0.9}}})[0][1]["stage"] == "reflection"
    assert _qa_events("retriever", {"retrieved": [1, 2], "crag_level": "relevant"})[0][0] == Event.RETRIEVAL
    assert _qa_events("reflector", {})[0][0] == Event.THINKING
    assert _qa_events("unknown-node", {}) == []


@pytest.mark.unit
def test_qa_events_synthesizer_emits_citation_then_source():
    """徽章要的 `[paper_id:page:chunk_id]` 三元组由 source 帧给出。"""
    from app.api.v1.qa import _qa_events
    from app.llm.streaming import Event

    cite = {
        "marker": 1,
        "chunk_id": 101,
        "paper_id": 7,
        "page_start": PAGE,
        "bbox": BBOX,
        "quote": "系数取 0.01" * 100,  # 超长也要截断，别把整段正文塞进事件
        "title": "MoE 论文",
        "confidence": 0.88,
    }
    events = _qa_events("synthesizer", {"citations": [cite]})
    assert [e for e, _ in events] == [Event.CITATION, Event.SOURCE]

    source = events[1][1]
    # id 一律是 int：state 里存字符串，对外契约（SourceTraceOut）是数字，SSE 出口统一掉
    assert (source["paper_id"], source["page"], source["chunk_id"]) == (7, PAGE, 101)
    assert source["bbox"] == BBOX
    assert source["confidence"] == pytest.approx(0.88)
    assert len(source["quote"]) == 280


@pytest.mark.unit
def test_qa_events_source_coerces_string_ids():
    """state 里的 Citation 用的是字符串 id（要过 checkpointer 的 pickle），出口必须是数字。"""
    from app.api.v1.qa import _qa_events

    events = _qa_events(
        "synthesizer",
        {"citations": [{"chunk_id": "101", "paper_id": "7", "page_start": "4"}]},
    )
    assert events[1][1]["chunk_id"] == 101
    assert events[1][1]["paper_id"] == 7
    assert events[1][1]["page"] == 4
    # 转不动就给 None，不能变成 0（0 会让前端跳到第 0 页）
    bad = _qa_events("synthesizer", {"citations": [{"chunk_id": "", "paper_id": None}]})
    assert bad[1][1]["chunk_id"] is None and bad[1][1]["paper_id"] is None


@pytest.mark.unit
def test_qa_events_source_falls_back_to_page_field():
    """老结构只有 `page`（没有 page_start）时也得给出页码，否则点引用跳不动。"""
    from app.api.v1.qa import _qa_events

    events = _qa_events("synthesizer", {"citations": [{"chunk_id": 1, "page": 6}]})
    assert events[1][1]["page"] == 6


@pytest.mark.unit
def test_qa_events_guardrail_block_ends_the_stream_with_error():
    """硬拒绝用 error 帧收场：一条流只能有一种收尾，前端据此关连接。"""
    from app.api.v1.qa import _qa_events
    from app.llm.streaming import Event

    events = _qa_events(
        "guardrails",
        {"trace": [{"node": "guardrails", "action": "block"}], "answer": "抱歉，无法回答该问题。"},
    )
    assert events[0][0] == Event.ERROR
    assert "无法回答" in events[0][1]["message"]

    # 非硬拒绝只记一条 guardrail 轨迹，不打断流
    ok = _qa_events("guardrails", {"trace": [{"node": "guardrails", "action": "rewrite"}], "guardrail_flags": ["pii"]})
    assert ok[0][0] == Event.GUARDRAIL and ok[0][1]["flags"] == ["pii"]


# ==================================================================== 流式端到端


@pytest.mark.e2e_smoke
def test_qa_stream_emits_spec_events_and_finishes_with_done(api):
    """一条完整流：thinking → retrieval → citation+source → token → done。"""
    client, _ = api()
    with client.stream("POST", "/api/v1/qa/stream", json={"question": QUERY, "mode": "single_paper_qa"}) as res:
        assert res.status_code == 200, res.read()
        assert res.headers["content-type"].startswith("text/event-stream")
        frames = collect_frames(b"".join(res.iter_bytes()).decode("utf-8"))

    names = [n for n, _ in frames]
    for required in ("thinking", "retrieval", "citation", "source", "token", "done"):
        assert required in names, (required, names)

    # 收尾必须是 done 或 error —— 且这里不该是 error
    assert names[-1] == "done", names[-3:]

    assert first(frames, "thinking")["stage"] == "intent"
    assert first(frames, "thinking")["intent"] == "single_paper_qa"
    assert first(frames, "retrieval")["n"] == 1
    source = first(frames, "source")
    assert (source["paper_id"], source["page"], source["chunk_id"]) == (7, PAGE, 101)
    assert source["bbox"] == BBOX
    assert isinstance(first(frames, "done").get("timeline"), list)

    # 正文是逐段推的：拼起来等于答案，且不含被剥掉的越界编号
    assert "".join(d["text"] for n, d in frames if n == "token") == ANSWER


@pytest.mark.e2e_smoke
def test_qa_stream_single_paper_has_no_timeline_points(api):
    """单篇问答的时间轴是空表 —— 前端据此整块不渲染。"""
    client, _ = api()
    with client.stream("POST", "/api/v1/qa/stream", json={"query": QUERY}) as res:
        frames = collect_frames(b"".join(res.iter_bytes()).decode("utf-8"))
    assert first(frames, "done")["timeline"] == []


@pytest.mark.e2e_smoke
def test_ask_reports_faithfulness_and_timeline(api):
    """规格里的响应字段在 `/qa/ask` 上齐全：answer/sources/grounding_ratio/faithfulness。

    `faithfulness` 为 null 是**正确**的：这条链路（单步检索 → 综合）不经过 Reflector
    （图上 reflector 只挂在 tool / executor / replanner 之后），评审根本没跑。
    前端据此不显示"忠实度"胶囊 —— 显示一个没跑过的 0 分才是错的。
    """
    client, _ = api()
    res = client.post("/api/v1/qa/ask", json={"query": QUERY})
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["answer"] == ANSWER
    assert data["sources"]
    assert data["grounding_ratio"] == pytest.approx(1.0)
    assert "faithfulness" in data and data["faithfulness"] is None
    assert data["timeline"] == []  # 单篇不排时间轴


@pytest.mark.e2e_smoke
def test_citation_exposes_page_start_alias(api):
    """前端按 `page_start` 读页码，后端字段叫 `page`，两个都得在。"""
    client, _ = api()
    data = client.post("/api/v1/qa/ask", json={"query": QUERY}).json()["data"]
    cite = data["citations"][0]
    assert cite["page"] == PAGE
    assert cite["page_start"] == PAGE
    assert cite["answer_span"]  # 答案里那句话（前端做句子高亮用）
    assert cite["bbox"] == BBOX
