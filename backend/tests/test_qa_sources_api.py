"""验收 5：QA 请求返回**答案 + 溯源列表**（走真实 HTTP 层与真实 Agent 图）。

与 `test_agent_graph_e2e.py` 的分工：那边验"图能不能从 START 跑到 END"，
这边验"答案与溯源能不能**完整穿过 API 边界**"。两者都是必须的 ——
图跑通但响应模型漏字段（例如 bbox 没透传）在端到端图测试里完全看不见。

外部依赖的处理：
- 模型层：替换 `get_llm()` 单例的 complete/stream（与 e2e 同一手法）；
- 检索：替换 `HybridRetriever`（真检索要 Milvus + PG，本机没有）；
- 存储：替换 `_attach_titles` / `ensure_default_user` / `persist_qa_history`。
  这些都是"尽力而为"的旁路，替换掉不影响被测的响应映射；
- checkpointer：`get_graph()` 会真连一次 PG，连不上就退化 MemorySaver
  （见 app/agents/checkpointer.py），所以这里不需要打补丁。
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
HALLUCINATED_ANSWER = ANSWER + "此外该结论可推广到全部模态 [7]。"
PAGE = 4
BBOX = [72.0, 120.0, 480.0, 143.0]

CONTRACT_FIELDS = {"answer_span", "chunk_id", "paper_id", "page", "bbox", "confidence", "attribution_method"}


class FakeLLM:
    """按结构化 schema 的 title 分派：意图 → 计划 → 综合成文。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def _node(self, messages: list[dict[str, Any]]) -> str:
        # 不能用 json.dumps(messages) 再找标记 —— schema 是嵌在 content **字符串**里的，
        # 转成 JSON 后引号会被转义，标记就匹配不上了。
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
                    "intent": "single_paper_qa",
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
        node = self._node(messages)
        self.calls.append(node)
        return self._payload(node)

    async def stream(self, role: Any, messages: list[dict[str, Any]], **extra: Any):
        text = await self.complete(role, messages)
        for i in range(0, len(text), 16):
            yield text[i : i + 16]


class FakeResult:
    """够用的 SQLAlchemy 结果替身：`(await session.execute(stmt)).scalars().all()`。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """不连库的会话：任何查询都返回同一批假 chunk。

    `/qa/trace` 要真的查一次 `chunks`（默认白名单是"库里前 8 块"），
    所以那一步绕不开会话 —— 用依赖覆盖比 mock 会话方法更贴近真实调用链。
    """

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        return FakeResult(self.chunks)

    async def flush(self) -> None:  # pragma: no cover - 用不到但别缺
        return None


class FakeChunkRow:
    """长得像 `chunks` 表的一行。"""

    def __init__(self, chunk_id: int, content: str, *, page: int = PAGE, bbox: Any = None) -> None:
        self.id = chunk_id
        self.paper_id = 7
        self.content = content
        self.section = "Method"
        self.page = page
        self.bbox = bbox if bbox is not None else BBOX


@pytest.fixture
def api(monkeypatch):
    """返回 `install(answer)`：装好假依赖并给出 (client, state)。

    `state["saved"]` 是落库旁路收到的参数，用来验证"存下来的溯源 == 返回的溯源"。
    """
    from app.api.deps import get_session
    from app.llm.client import get_llm
    from app.main import app

    state: dict[str, Any] = {"saved": None, "llm": None}
    row = FakeChunkRow(101, CHUNK_TEXT)

    async def fake_session():
        yield FakeSession([row])

    monkeypatch.setitem(app.dependency_overrides, get_session, fake_session)

    with TestClient(app) as client:

        def install(answer: str = ANSWER):
            llm = FakeLLM(answer)
            state["llm"] = llm
            state["saved"] = None
            real = get_llm()
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

            # 旁路存储：不碰真库
            async def no_titles(session: Any, rows: list) -> None: ...

            async def default_user(session: Any) -> int:
                return 1

            async def save_history(**kwargs: Any) -> int:
                state["saved"] = kwargs
                return 42

            monkeypatch.setattr("app.api.v1.qa._attach_titles", no_titles)
            monkeypatch.setattr("app.api.v1.qa.ensure_default_user", default_user)
            monkeypatch.setattr("app.api.v1.qa.persist_qa_history", save_history)
            return client, state

        yield install


# ==================================================================== 验收 5
@pytest.mark.e2e_smoke
def test_ask_returns_answer_with_traced_sources(api):
    """一次问答：响应里既有答案，也有完整的溯源列表。"""
    client, _ = api(ANSWER)
    res = client.post("/api/v1/qa/ask", json={"query": QUERY})
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["answer"] == ANSWER

    # ---- 溯源列表：契约字段一个都不能少 ----
    assert len(data["sources"]) == 1, data["sources"]
    source = data["sources"][0]
    assert set(source) == CONTRACT_FIELDS, source
    assert source["chunk_id"] == 101
    assert source["paper_id"] == 7
    assert source["page"] == PAGE
    assert source["bbox"] == BBOX
    assert 0.0 < source["confidence"] <= 1.0
    assert source["attribution_method"] == "hybrid"  # 词法蕴含 + 数字硬规则
    assert "0.01" in source["answer_span"] and "[1]" in source["answer_span"]

    # ---- 引用明细与有据率 ----
    assert data["citations"][0]["marker"] == 1
    assert data["citations"][0]["confidence"] == pytest.approx(source["confidence"])
    assert data["citations"][0]["bbox"] == BBOX
    assert data["grounding_ratio"] == pytest.approx(1.0)
    assert data["passed_grounding"] is True

    # ---- 召回来源标记也要透传（前端要显示 dense/sparse）----
    # `/qa/ask` 走 state（RetrievedDoc dict），score 里已经折进了 rerank 分
    # （`final_score` 优先取 rerank_score）；原始 rerank_score 只在 `/qa/retrieve` 暴露。
    assert data["retrieved"][0]["sources"] == ["dense", "sparse"]
    assert data["retrieved"][0]["score"] == pytest.approx(0.95)
    assert data["retrieved"][0]["chunk_id"] == 101  # dict 形态也要正确映射，不能退化成 0
    assert data["retrieved"][0]["bbox"] == BBOX
    assert CHUNK_TEXT[:20] in data["retrieved"][0]["preview"]


@pytest.mark.e2e_smoke
def test_qa_history_keeps_the_same_sources(api):
    """落库的 `sources` 与响应里的溯源必须是同一批证据 —— 否则"复查上个月那次回答"
    会得到与当时不同的结论。"""
    from app.api.v1.qa import build_sources

    client, state = api(ANSWER)
    client.post("/api/v1/qa/ask", json={"query": QUERY})

    saved = state["saved"]
    assert saved is not None, "问答没有落库"
    assert saved["grounding_ratio"] == pytest.approx(1.0)
    assert saved["citations"][0]["chunk_id"] == "101"
    assert saved["citations"][0]["attribution_method"] == "hybrid"
    assert saved["citations"][0]["bbox"] == BBOX

    # 落库结构用的是**字符区间**（前端按区间高亮），响应里给的是文本片段，两者互补
    rows = build_sources(saved["citations"], ANSWER)
    assert rows[0]["chunk_id"] == 101
    assert rows[0]["method"] == "hybrid"
    assert rows[0]["confidence"] > 0
    start, end = rows[0]["answer_span"]
    assert ANSWER[start:end].startswith("负载均衡辅助损失")
    assert rows[0]["bbox"] == BBOX


@pytest.mark.e2e_smoke
def test_injected_hallucination_is_reported_and_not_confirmed(api):
    """注入幻觉引用 [7] 后：接口仍返回答案，但**明确标记未通过**并给出坏引用标记。"""
    client, _ = api(HALLUCINATED_ANSWER)
    res = client.post("/api/v1/qa/ask", json={"query": QUERY})
    data = res.json()["data"]

    assert "bad_citation_ref" in data["guardrail_flags"]
    assert "[7]" not in data["answer"]  # 越界编号被生成侧防护剥掉
    assert data["grounding_ratio"] < 0.8  # 注入句把有据率拉下来
    assert data["passed_grounding"] is False
    # 只有合法的 [1] 留下一条溯源，不能给幻觉编号编一条记录
    assert [s["chunk_id"] for s in data["sources"]] == [101]


@pytest.mark.e2e_smoke
def test_trace_endpoint_returns_same_contract(api):
    """`/qa/trace` 是"只校验不生成"的入口，返回的溯源结构必须与 /ask 一致。"""
    client, _ = api()
    res = client.post("/api/v1/qa/trace", json={"answer": ANSWER, "chunk_ids": []})
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["passed"] is True
    assert data["terms_total"] >= data["terms_supported"] > 0
    assert set(data["sources"][0]) >= CONTRACT_FIELDS
