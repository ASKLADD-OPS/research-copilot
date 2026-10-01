"""Agent 图端到端测试 —— mock 掉模型层，让图从 START 真跑到 END。

为什么要这一层：`test_graph_routing.py` 钉的是"路由函数返回什么字符串"，但
LangGraph 的边表解析、reducer 合并、循环计数器、checkpointer、静态断点都**只在真跑图
时才会暴露问题**。已经栽过三次，且全都不在纯函数测试的射程内：

1. 路由返回的名字不在边表里 → runtime `KeyError`，SSE 只吐一帧 error；
2. `retriever`/`executor` 拿 `RetrievedChunk.page_start` 取值，而字段叫 `page` →
   检索一有结果就 `AttributeError`（slots dataclass，没有兜底属性）；
3. `replanner` 的 `remaining` 恒为空列表 → "重规划"是一条死分支。

模型层的 mock 方式：所有节点都通过 `lru_cache` 的 `get_llm()` 取**同一个** LLMClient
实例，所以直接替换这个实例的 `complete`/`stream` 就等于换掉整张图的模型 —— 不需要
给每个节点模块打各自的 patch。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.graph import build_graph
from app.agents.state import new_state
from app.llm.client import get_llm
from app.rag.retriever import RetrievedChunk
from app.rag.source_tracing import SourceTracer

# 检索片段与问题共享足够多的中文 token，CRAG 的词法预判会直接判 relevant，
# 省掉一次 LLM 判级调用（那条路径由 test_crag.py 单独覆盖）。
QUERY = "MoE 负载均衡的辅助损失系数是多少？"
CHUNK_TEXT = "MoE 负载均衡的辅助损失（auxiliary loss）系数取 0.01，作用于门控网络的负载分布。"

FINAL_ANSWER = "负载均衡辅助损失的系数为 0.01 [1]。"
ARXIV_ACTION = 'Thought: 先查外部文献\nAction: arxiv_search\nAction Input: {"query": "MoE load balancing"}'


# ==================================================================== 假模型层
# 节点调结构化输出时会把 Pydantic schema 塞进 system 提示，用 schema 标题分派即可。
_SCHEMA_MARKERS = (
    ("IntentResult", "_intent"),
    ("PlanResult", "_plan"),
    ("ReflectionResult", "_reflection"),
    ("ReplanResult", "_replan"),
    ("ClarifyResult", "_clarify"),
    ("_LLMGrade", "_grade"),
)


class FakeLLM:
    """按"谁来消费这次调用"分派固定答复。

    spec 里每一项都可以给 list：同一节点被多次调用时按顺序取，取完停在最后一项
    （refine / ReAct 循环要的就是"第二轮给出不同答复"）。
    """

    def __init__(self, **spec: Any) -> None:
        self.spec = spec
        self.calls: list[str] = []
        self._cursor: dict[str, int] = {}

    @staticmethod
    def _node(messages: list[dict[str, Any]]) -> str:
        text = "\n".join(str(m.get("content") or "") for m in messages)
        for marker, node in _SCHEMA_MARKERS:
            if f'"title": "{marker}"' in text:
                return node
        # 自由文本：executor 的 user 消息带"本步目标"，synthesizer 没有
        return "_executor" if "本步目标：" in text else "_synthesizer"

    def _take(self, node: str) -> Any:
        value = self.spec.get(node)
        if isinstance(value, list):
            i = min(self._cursor.get(node, 0), len(value) - 1)
            self._cursor[node] = i + 1
            return value[i]
        return value

    def calls_of(self, node: str) -> int:
        return self.calls.count(node)

    async def complete(
        self,
        role: Any,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        response_format: Any = None,
        **extra: Any,
    ) -> str:
        node = self._node(messages)
        self.calls.append(node)
        payload = self._take(node)
        if payload is None:
            raise AssertionError(f"FakeLLM 没为 {node} 准备答复（已发生调用：{self.calls}）")
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def stream(self, role: Any, messages: list[dict[str, Any]], **extra: Any):
        text = await self.complete(role, messages)
        for i in range(0, len(text), 8):
            yield text[i : i + 8]


@pytest.fixture
def install_llm(monkeypatch):
    """把整张图的模型层换成 FakeLLM。"""

    def _install(**spec: Any) -> FakeLLM:
        fake = FakeLLM(**spec)
        llm = get_llm()
        monkeypatch.setattr(llm, "complete", fake.complete)
        monkeypatch.setattr(llm, "stream", fake.stream)
        return fake

    return _install


@pytest.fixture(autouse=True)
def offline_tracer(monkeypatch):
    """溯源引擎强制走词法代理。

    `get_tracer()` 默认 `enable_nli=True`，会**真去 HuggingFace 拉 cross-encoder 权重**；
    网络不通时它不是抛错而是长时间挂起，`except` 兜不住。
    """
    monkeypatch.setattr("app.rag.source_tracing.get_tracer", lambda: SourceTracer(enable_nli=False))


@pytest.fixture
def fake_retrieval(monkeypatch):
    """把 Milvus 混合检索换成固定片段，让 retriever 节点走通 relevant 分支。"""
    chunks = [
        RetrievedChunk(
            id=101,
            paper_id=7,
            content=CHUNK_TEXT,
            section="Method",
            page=4,
            score=0.91,
            sources=["dense", "sparse"],
        )
    ]

    class _FakeHybridRetriever:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def retrieve(self, query: str, paper_ids: Any = None, **kwargs: Any) -> list[RetrievedChunk]:
            return list(chunks)

    monkeypatch.setattr("app.agents.nodes.retriever.HybridRetriever", _FakeHybridRetriever)
    return chunks


class FakeTools:
    """假的 MCP 工具层。`fail` 里的工具名一调就抛，用来造"工具失败"场景。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, Exception] = {}

    async def __call__(self, name: str, args: Any = None, **kwargs: Any) -> Any:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]
        return f"{name} 返回：示例结果"


@pytest.fixture
def fake_tools(monkeypatch):
    tools = FakeTools()
    # executor 在函数体内 import（run-time 取属性），tool_node 在模块级 import（已绑在模块命名空间里）。
    # tool_node 这里必须走 sys.modules：`app.agents.nodes.__init__` 把同名**函数**也导出成
    # `app.agents.nodes.tool_node`，字符串路径会被解析成那个函数而不是子模块。
    tool_node_module = sys.modules["app.agents.nodes.tool_node"]
    monkeypatch.setattr("app.agents.mcp.registry.call_tool", tools)
    monkeypatch.setattr(tool_node_module, "call_tool", tools)
    return tools


def _intent(intent: str = "literature_search", confidence: float = 0.92) -> dict[str, Any]:
    return {"intent": intent, "confidence": confidence, "target_papers": [], "slot_filling": {}, "reason": "test"}


def _plan(*tools: str) -> dict[str, Any]:
    return {
        "steps": [
            {"idx": i + 1, "goal": f"第 {i + 1} 步", "tool": t, "status": "pending"} for i, t in enumerate(tools)
        ],
        "reasoning": "test",
    }


def _scores(value: float) -> dict[str, Any]:
    return {"scores": dict.fromkeys(("faithfulness", "relevance", "coherence", "completeness"), value)}


def _nodes_in(state: dict[str, Any]) -> list[str]:
    return [str(t.get("node")) for t in (state.get("trace") or []) if isinstance(t, dict)]


# ==================================================================== 测试
@pytest.mark.e2e
async def test_happy_path_runs_start_to_end(install_llm, fake_retrieval, fake_tools, settings):
    """验收 2：mock LLM 后从 START 跑到 END（单篇问答：检索 → 综合 → 防护）。"""
    llm = install_llm(
        _intent=_intent("single_paper_qa"),
        _plan=_plan("retrieve_papers"),
        _synthesizer=FINAL_ANSWER,
    )
    graph = build_graph(with_checkpointer=False)
    state = await graph.ainvoke(new_state(QUERY, session_id="s-happy"))

    assert state["intent"] == "single_paper_qa"
    assert state["answer"] == FINAL_ANSWER
    assert state["done"] is True
    assert not state.get("error")

    # 检索结果真的进了 state，且页码字段能取到（曾经在 page_start 上炸）
    assert [d["page"] for d in state["retrieved"]] == [4]
    assert state["crag_level"] == "relevant"

    nodes = _nodes_in(state)
    for expected in ("intent", "planner", "retriever", "synthesizer", "guardrails"):
        assert expected in nodes, f"{expected} 没在轨迹里：{nodes}"

    # 溯源真的跑了：引用落在白名单内 → grounding_ratio 是真实数字
    assert isinstance(state["grounding_ratio"], float)
    assert state["citations"]
    assert llm.calls == ["_intent", "_plan", "_synthesizer"]


@pytest.mark.e2e
async def test_low_confidence_intent_goes_to_clarify(install_llm, fake_retrieval, fake_tools):
    """验收 3：低置信度 → clarify 路径，且不能白白跑一遍规划。"""
    llm = install_llm(
        _intent=_intent("single_paper_qa", confidence=0.31),
        _clarify={"question": "你想问的是哪一篇论文的方法？", "options": ["A 篇", "B 篇"]},
    )
    graph = build_graph(with_checkpointer=False)
    state = await graph.ainvoke(new_state("那个方法呢", session_id="s-clarify"))

    assert state["clarify_question"] == "你想问的是哪一篇论文的方法？"
    assert state["done"] is True
    assert "_plan" not in llm.calls, "低置信度不该进规划"
    assert "_synthesizer" not in llm.calls, "澄清轮不该生成答案"
    assert state.get("retrieved") in (None, [])


@pytest.mark.e2e
async def test_tool_failure_leads_to_reflector_then_replan(install_llm, fake_retrieval, fake_tools, settings):
    """验收 4：工具失败 → 步骤标 failed → reflector 批判 → replanner 追加补救步骤。

    这条链路曾经整段不可达：`replanner.remaining` 取的是 `plan[current_step:]`，
    而本节点只在计划跑完时才被进入，于是恒为空 —— replan 是死分支。
    """
    fake_tools.fail["arxiv_search"] = RuntimeError("arxiv 服务不可用")
    install_llm(
        _intent=_intent(),
        _plan=_plan("retrieve_papers", "arxiv_search"),
        # executor 第一次进入：ReAct 每轮都发 Action、工具每轮都炸 → 轮次用尽仍未收敛
        # → 该步 status=failed，触发重规划。第二次进入（补救步骤）才给出结论。
        _executor=[ARXIV_ACTION] * settings.AGENT_MAX_REACT_ROUNDS + [f"Final Answer: {FINAL_ANSWER}"],
        _replan={
            "decision": "replan",
            "reason": "外部检索不可用",
            "new_steps": [{"idx": 99, "goal": "改用本地语料作答", "tool": "python_exec", "status": "pending"}],
        },
        _reflection=_scores(0.95),
        _synthesizer=FINAL_ANSWER,
    )
    graph = build_graph(with_checkpointer=False)
    state = await graph.ainvoke(new_state(QUERY, session_id="s-replan"))

    # 工具真的被调用且真的失败了
    assert any(name == "arxiv_search" for name, _ in fake_tools.calls)

    # 重规划发生了：轮次 +1、原计划被追加、失败步骤被标记为已被取代
    assert state["plan_round"] == 1
    assert len(state["plan"]) == 3
    assert [s["status"] for s in state["plan"]] == ["done", "replanned", "done"]
    decisions = [t.get("decision") for t in state["trace"] if t.get("node") == "replanner"]
    assert "replan" in decisions

    # 补救步骤真的被 executor 接手执行，最终仍然收在答案上
    assert state["answer"] == FINAL_ANSWER
    executed_goals = [t.get("goal") for t in state["trace"] if t.get("node") == "executor" and t.get("goal")]
    assert "改用本地语料作答" in executed_goals, executed_goals
    nodes = _nodes_in(state)
    for expected in ("retriever", "executor", "reflector", "replanner", "synthesizer", "guardrails"):
        assert expected in nodes, f"{expected} 不在轨迹里：{nodes}"


@pytest.mark.e2e
async def test_low_reflection_refines_then_stops_at_limit(install_llm, fake_retrieval, fake_tools, settings):
    """验收 5：Reflection 低分 → refine 循环，且到轮次上限必须收手。"""
    llm = install_llm(
        _intent=_intent(),
        _plan=_plan("python_exec"),
        _executor=f"Final Answer: {FINAL_ANSWER}",
        _reflection=_scores(0.2),  # 每一轮都低分 → 每次都要求 refine
        _synthesizer=FINAL_ANSWER,
    )
    graph = build_graph(with_checkpointer=False)
    state = await graph.ainvoke(new_state(QUERY, session_id="s-refine"))

    limit = settings.REFLECTION_MAX_REFINE
    assert limit == 2
    # refine 恰好执行 limit 次：计数器记的是"已执行次数"，不是"已决策次数"
    assert state["refine_round"] == limit
    assert llm.calls_of("_executor") == limit, "每次 refine 都要回到 executor 重做"
    # 第 limit+1 次评审仍然低分，但预算用尽 → 改判 pass 收尾
    assert llm.calls_of("_reflection") == limit + 1
    assert _nodes_in(state).count("reflector") == limit + 1
    assert state["reflection"]["verdict"] == "pass"
    assert state["answer"] == FINAL_ANSWER


@pytest.mark.e2e
async def test_checkpointer_persists_state_and_isolates_sessions(install_llm, fake_retrieval, fake_tools):
    """验收 6：Checkpointer 把状态按 thread_id 存住，同一 session 续得上、不同 session 不串味。"""
    install_llm(_intent=_intent(), _plan=_plan("retrieve_papers"), _synthesizer=FINAL_ANSWER)
    graph = build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "sess-restore"}}

    first = await graph.ainvoke(new_state(QUERY, session_id="sess-restore"), cfg)
    saved = await graph.aget_state(cfg)
    assert saved.values["answer"] == FINAL_ANSWER
    assert saved.values["session_id"] == "sess-restore"

    # 每一节点一步 → 快照数量至少是 1（起点）+ 节点数
    snapshots = [snap async for snap in graph.aget_state_history(cfg)]
    assert len(snapshots) >= 2

    # 同一 thread 再问一轮：trace 是 operator.add，续跑而不是从零开始
    second = await graph.ainvoke(new_state(QUERY, session_id="sess-restore"), cfg)
    assert len(second["trace"]) > len(first["trace"])

    # 换一个 thread：干净的状态，读不到别人的东西
    other = await graph.aget_state({"configurable": {"thread_id": "sess-other"}})
    assert not other.values


@pytest.mark.e2e
async def test_human_in_the_loop_pauses_before_executor_and_resumes(install_llm, fake_retrieval, fake_tools):
    """验收 6 的 Human-in-the-Loop：executor 前停下等人工改计划，改完能恢复。"""
    llm = install_llm(
        _intent=_intent(),
        _plan=_plan("retrieve_papers", "python_exec"),  # 两步才会进 executor（首步是检索）
        _executor=f"Final Answer: {FINAL_ANSWER}",
        _reflection=_scores(0.95),
        _synthesizer=FINAL_ANSWER,
    )
    graph = build_graph(checkpointer=MemorySaver(), human_in_the_loop=True)
    cfg = {"configurable": {"thread_id": "sess-hitl"}}

    await graph.ainvoke(new_state(QUERY, session_id="sess-hitl"), cfg)
    pending = await graph.aget_state(cfg)
    assert pending.next == ("executor",), f"断点位置不对：{pending.next}"
    assert llm.calls_of("_synthesizer") == 0, "断点之前不该开始收尾"
    assert llm.calls_of("_executor") == 0, "断点之前不该开始执行"

    # 用户改计划：换掉剩下那一步。
    # 注意 `current_step` 必须一起改 —— executor 靠它取步骤，改成单步计划却留着旧指针，
    # executor 会取不到步骤直接空转（不报错，但什么都不做）。
    edited = [{"idx": 1, "goal": "用户改过的目标", "tool": "python_exec", "status": "pending"}]
    await graph.aupdate_state(cfg, {"plan": edited, "current_step": 0})

    resumed = await graph.ainvoke(None, cfg, interrupt_before=[])  # 放行一次，让 executor 真的跑
    assert llm.calls_of("_executor") == 1, "恢复后 executor 没有执行"
    assert llm.calls_of("_synthesizer") == 1
    # 被改过的那一步真的带着内容跑完了
    assert resumed["plan"][0]["goal"] == "用户改过的目标"
    assert resumed["plan"][0]["status"] == "done"
    assert resumed["answer"] == FINAL_ANSWER
    # 恢复后本次 thread 已经走到图末，没有残留断点
    assert not (await graph.aget_state(cfg)).next


@pytest.mark.e2e
async def test_stream_events_are_ordered(install_llm, fake_retrieval, fake_tools):
    """验收 7：`astream_events(version="v2")` 的事件按图序到达。"""
    install_llm(_intent=_intent(), _plan=_plan("retrieve_papers"), _synthesizer=FINAL_ANSWER)
    graph = build_graph(with_checkpointer=False)

    events = [e async for e in graph.astream_events(new_state(QUERY), version="v2")]
    started = [
        e["name"]
        for e in events
        if e["event"] == "on_chain_start"
        and e["name"]
        in {
            "intent",
            "planner",
            "retriever",
            "tool",
            "executor",
            "reflector",
            "replanner",
            "synthesizer",
            "guardrails",
            "clarify",
        }
    ]
    assert started == ["intent", "planner", "retriever", "synthesizer", "guardrails"], started
    assert events[-1]["event"] == "on_chain_end"


@pytest.mark.e2e
async def test_chat_sse_frames_come_out_in_graph_order(install_llm, fake_retrieval, fake_tools, monkeypatch):
    """验收 7 的下半段：SSE 出口把图事件翻成前端帧，顺序与收尾都符合协议。"""
    from app.api.v1.chat import ChatRequest, _stream_frames

    install_llm(_intent=_intent(), _plan=_plan("retrieve_papers"), _synthesizer=FINAL_ANSWER)
    graph = build_graph(with_checkpointer=False)
    monkeypatch.setattr("app.agents.graph.get_graph", lambda: graph)

    frames = [frame async for frame in _stream_frames(ChatRequest(query=QUERY))]
    names = [f.split("\n", 1)[0].removeprefix("event: ") for f in frames if f.startswith("event:")]

    assert names[0] == "intent"
    assert names[-1] == "done", f"流必须以 done/error 收尾，实际：{names}"
    assert names.index("plan") < names.index("tool") < names.index("done")

    # 每帧都是合法的 SSE（data: 行 + 空行结尾），且 payload 是 JSON
    for frame in frames[1:]:
        assert frame.endswith("\n\n")
        body = "\n".join(
            line.removeprefix("data: ") for line in frame.strip().splitlines() if line.startswith("data: ")
        )
        json.loads(body)


@pytest.mark.e2e
def test_draw_mermaid_png_renders():
    """验收 1：`draw_mermaid_png()` 能产出真实 PNG。

    走的是 mermaid.ink，需要外网；不可达时跳过而不是判失败 —— 渲染能力本身
    是 langgraph 的，不是本项目的代码。
    """
    try:
        png = build_graph(with_checkpointer=False).get_graph().draw_mermaid_png()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"mermaid.ink 不可达，跳过 PNG 渲染：{exc}")

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 5000
