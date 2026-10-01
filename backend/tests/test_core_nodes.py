"""五个核心节点的节点级测试 —— 对照提示词的 6 条验收标准逐条钉死。

为什么还要这一层（已有 `test_graph_routing.py` 管路由、`test_agent_graph_e2e.py` 管全图）：

- e2e 用的是"一个假 LLM 顶替所有节点"，它验证的是**接线**，不是每个节点自己的契约。
  节点一旦单独改坏（比如 planner 丢掉了 DAG 字段、executor 分不清工具与直答），
  只要假 LLM 还按 schema 回话，e2e 照样绿。
- 这里直接调节点函数，断言节点**返回的那个 dict**。模型层按节点单独 mock。

对应关系：
1. 意图识别：8 类覆盖 + 边界样本（越界置信度/未知类别/空输入/模型故障）→ 本文件前三节
2. Planner：跨文对比生成 DAG（依赖 + 并行组 + 拓扑收敛）      → `TestPlanner`
3. Executor：区分"调用工具"与"直接回答"                       → `TestExecutor`
4. Reflector：检出注入的幻觉内容                              → `TestReflector`
5. Replanner：工具失败触发 replan，超限不再重规划              → `TestReplanner`
6. 所有 Prompt 独立成文件、可单独迭代                          → `TestPrompts`
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from app.agents.prompts import available, render
from app.agents.state import Intent

EXPECTED_INTENTS = {
    "single_paper_qa",
    "cross_paper_reasoning",
    "literature_search",
    "graph_analysis",
    "writing_assist",
    "visualization",
    "translation",
    "chitchat",
}

CORE_PROMPTS = ("intent", "planner", "executor", "reflector", "replanner")


def _user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")


# ==================================================================== 1. 意图识别
class TestIntent:
    @pytest.mark.unit
    def test_state_intent_literal_has_exactly_eight_classes(self):
        """意图类别是 `state.Intent` 这个 Literal —— 少一类会被静默降级。"""
        assert set(get_args(Intent)) == EXPECTED_INTENTS

    @pytest.mark.unit
    @pytest.mark.parametrize("intent", sorted(EXPECTED_INTENTS))
    async def test_every_class_survives_the_node(self, monkeypatch, intent):
        """8 类各自走一遍节点：模型说什么，state 里就得是什么，不能被改写。"""
        import app.agents.nodes.intent as m

        async def fake(schema, messages, **kw):  # noqa: ARG001
            assert schema is m.IntentResult, "意图节点必须请求 IntentResult"
            return m.IntentResult(intent=intent, confidence=0.9, reason="test")

        monkeypatch.setattr(m, "complete_structured", fake)
        out = await m.intent_node({"query": "任意问题"})

        assert out["intent"] == intent
        assert out["confidence"] == 0.9
        assert out["target_papers"] == []
        assert out["slot_filling"] == {}
        assert out["trace"][0]["node"] == "intent"

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [1.2, -0.01, "很高"])
    async def test_confidence_outside_range_is_rejected_by_pydantic(self, bad):
        """置信度是 [0,1] 的强契约 —— 模型给 1.2 或中文描述必须校验失败，
        而不是让它溜进 state 去和 0.6 阈值比较。"""
        from app.agents.nodes.intent import IntentResult

        with pytest.raises(ValidationError):
            IntentResult.model_validate({"intent": "chitchat", "confidence": bad})

    @pytest.mark.unit
    def test_unknown_intent_is_rejected(self):
        from app.agents.nodes.intent import IntentResult

        with pytest.raises(ValidationError):
            IntentResult.model_validate({"intent": "summarize", "confidence": 0.9})

    @pytest.mark.unit
    @pytest.mark.parametrize("payload", [{}, {"intent": "chitchat"}])
    async def test_invalid_model_output_degrades_to_clarify(self, monkeypatch, payload):
        """严格校验的收益在这里：模型吐不出合法 JSON 时，节点退化成"低置信 + 澄清"，

        而不是拿一个猜的类别继续往下跑。confidence=0.0 保证路由一定指向 clarify。
        """
        import app.agents.nodes.intent as m
        from app.core.errors import LLMError

        async def boom(schema, messages, **kw):  # noqa: ARG001
            try:  # 真链路里是 complete_structured 抛 LLMError，这里复现它的失败语义
                m.IntentResult.model_validate(payload)
            except ValidationError as exc:
                raise LLMError(f"结构化输出失败: {exc}") from exc
            raise AssertionError("不该走到这里")

        monkeypatch.setattr(m, "complete_structured", boom)
        out = await m.intent_node({"query": "那个方法怎么样？"})

        assert out["confidence"] == 0.0
        assert out["confidence"] < m.CONFIDENCE_FLOOR
        assert out["trace"][0].get("error")

    @pytest.mark.unit
    async def test_empty_query_short_circuits_without_calling_the_model(self, monkeypatch):
        """空输入不该浪费一次模型调用，也不该让模型自由发挥。"""
        import app.agents.nodes.intent as m

        async def never(*a, **kw):  # noqa: ARG001
            raise AssertionError("空 query 不该调模型")

        monkeypatch.setattr(m, "complete_structured", never)
        out = await m.intent_node({"query": "   "})

        assert out["intent"] == "chitchat"
        assert out["confidence"] == 1.0
        assert out["clarify_question"]


# ==================================================================== 2. Planner / DAG
def _dag_steps() -> list[dict[str, Any]]:
    """跨文对比的典型 DAG：两路取证据 → 两路并行汇总 → 一路 join。"""
    return [
        {"idx": 1, "goal": "取三篇的方法章节", "tool": "retrieve_papers"},
        {"idx": 2, "goal": "取三篇的评测章节", "tool": "retrieve_papers"},
        {"idx": 3, "goal": "汇总方法差异", "tool": "python_exec", "dependencies": [1, 2], "parallel_group": "agg"},
        {"idx": 4, "goal": "汇总指标差异", "tool": "python_exec", "dependencies": [1, 2], "parallel_group": "agg"},
        {"idx": 5, "goal": "合成对照表", "tool": "python_exec", "dependencies": [3, 4]},
    ]


class TestPlanner:
    @pytest.mark.unit
    async def test_cross_paper_task_yields_a_real_dag(self, monkeypatch):
        """验收 2：跨文对比任务必须产出带依赖与并行组的 DAG，而不是一条直线。"""
        import app.agents.nodes.planner as m

        async def fake(schema, messages, **kw):  # noqa: ARG001
            assert schema is m.PlanResult
            return m.PlanResult(steps=_dag_steps(), reasoning="两路取证据、两路并行汇总、一路 join")

        monkeypatch.setattr(m, "complete_structured", fake)
        out = await m.planner_node({"query": "这三篇的方法与指标有何异同？"})
        plan = out["plan"]

        assert len(plan) == 5
        assert [s["idx"] for s in plan] == [1, 2, 3, 4, 5]
        assert plan[2]["dependencies"] == [1, 2]
        assert plan[3]["dependencies"] == [1, 2]
        assert plan[4]["dependencies"] == [3, 4]
        assert plan[2]["parallel_group"] == plan[3]["parallel_group"] == "agg"
        # 1、2 无前置 → 是并行的起点，不该被硬塞依赖
        assert "dependencies" not in plan[0] and "dependencies" not in plan[1]
        assert out["trace"][0]["dag_notes"] == []

    @pytest.mark.unit
    def test_forward_and_dangling_dependencies_are_dropped(self):
        """只准向后指：前向引用、自指、越界一律丢掉 —— 这条同时保证无环。"""
        from app.agents.nodes.planner import normalize_dag

        steps, notes = normalize_dag(
            [
                {"idx": 1, "goal": "a", "tool": "t", "dependencies": [2]},  # 前向
                {"idx": 2, "goal": "b", "tool": "t", "dependencies": [2]},  # 自指
                {"idx": 3, "goal": "c", "tool": "t", "dependencies": [1, 99, "x"]},  # 混合
            ]
        )
        assert "dependencies" not in steps[0]
        assert "dependencies" not in steps[1]
        assert steps[2]["dependencies"] == [1]
        assert len(notes) == 3

    @pytest.mark.unit
    def test_idx_is_renumbered_from_one(self):
        from app.agents.nodes.planner import normalize_dag

        steps, _ = normalize_dag(
            [{"idx": 0, "goal": "a", "tool": "t"}, {"idx": 7, "goal": "b", "tool": "t", "dependencies": [0]}]
        )
        assert [s["idx"] for s in steps] == [1, 2]
        # idx=0 → 重排成 1，于是原本指向 0 的依赖合法了
        assert steps[1]["dependencies"] == [1]

    @pytest.mark.unit
    def test_group_member_depending_on_its_own_group_is_demoted_to_serial(self):
        """并行组的前提是组内互不依赖 —— 否则并发跑会读到别人的未完成结果。"""
        from app.agents.nodes.planner import normalize_dag

        steps, notes = normalize_dag(
            [
                {"idx": 1, "goal": "a", "tool": "t", "parallel_group": "g"},
                {"idx": 2, "goal": "b", "tool": "t", "parallel_group": "g", "dependencies": [1]},
            ]
        )
        assert "parallel_group" not in steps[1], "依赖了同组成员的步骤必须退出该组"
        assert notes
        # 组里只剩 1 个成员 → 这个标签也没有并行含义了，一并去掉
        assert "parallel_group" not in steps[0]

    @pytest.mark.unit
    @pytest.mark.parametrize("n", [1, 5])
    def test_step_count_bounds_are_enforced(self, n):
        from app.agents.nodes.planner import PlanResult

        ok = [{"idx": i + 1, "goal": "g", "tool": "t"} for i in range(n)]
        assert len(PlanResult.model_validate({"steps": ok}).steps) == n

    @pytest.mark.unit
    def test_six_steps_is_rejected(self):
        """上限 5 步是写进 schema 的硬约束，不是只写在 prompt 里的建议。"""
        from app.agents.nodes.planner import PlanResult

        too_many = [{"idx": i + 1, "goal": "g", "tool": "t"} for i in range(6)]
        with pytest.raises(ValidationError):
            PlanResult.model_validate({"steps": too_many})

    @pytest.mark.unit
    async def test_model_failure_falls_back_to_a_runnable_single_step(self, monkeypatch):
        import app.agents.nodes.planner as m

        async def boom(*a, **kw):  # noqa: ARG001
            raise RuntimeError("模型超时")

        monkeypatch.setattr(m, "complete_structured", boom)
        out = await m.planner_node({"query": "q", "intent": "single_paper_qa"})

        assert len(out["plan"]) == 1
        assert out["plan"][0]["status"] == "pending"
        assert "退化" in out["trace"][0]["reasoning"]


# ==================================================================== 3. Executor
class _ScriptedLLM:
    """按顺序吐固定回答的假模型。用尽后停在最后一条。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.i = 0
        self.seen: list[str] = []

    async def complete(self, role: Any, messages: list[dict[str, Any]], **kw: Any) -> str:
        self.seen.append(_user_text(list(messages)))
        reply = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return reply


class _ToolSpy:
    def __init__(self, **fails: Exception) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fails = fails
        self.peak_concurrency = 0
        self._live = 0

    async def __call__(self, name: str, args: Any = None, **kw: Any) -> str:
        self.calls.append((name, args))
        self._live += 1
        self.peak_concurrency = max(self.peak_concurrency, self._live)
        await asyncio.sleep(0)  # 让出控制权，串行与并行的区别才会显形
        self._live -= 1
        if name in self.fails:
            raise self.fails[name]
        return f"{name}: ok"


@pytest.fixture
def scripted_executor(monkeypatch):
    """给 executor 装上脚本化模型与工具探针。"""

    def _install(replies: list[str], **fails: Exception):
        import app.agents.nodes.executor as m

        llm = _ScriptedLLM(replies)
        spy = _ToolSpy(**fails)
        monkeypatch.setattr(m, "get_llm", lambda: llm)
        monkeypatch.setattr("app.agents.mcp.registry.call_tool", spy)
        return llm, spy

    return _install


def _step_state(**over: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "query": "MoE 的辅助损失系数是多少？",
        "plan": [{"idx": 1, "goal": "取证据", "tool": "retrieve_papers", "status": "pending"}],
        "current_step": 0,
    }
    state.update(over)
    return state


class TestExecutor:
    @pytest.mark.unit
    async def test_action_means_call_the_tool(self, scripted_executor):
        """验收 3 上半：输出里带 Action → 真的去调工具，Observation 回喂后再收结论。"""
        llm, spy = scripted_executor(
            [
                'Thought: 先检索\nAction: retrieve_papers\nAction Input: {"query": "aux loss", "top_k": 8}',
                "Final Answer: 系数为 0.01 [1] 7:4:101",
            ]
        )
        import app.agents.nodes.executor as m

        out = await m.executor_node(_step_state())

        assert [c[0] for c in spy.calls] == ["retrieve_papers"]
        assert spy.calls[0][1] == {"query": "aux loss", "top_k": 8}
        assert out["plan"][0]["status"] == "done"
        assert "0.01" in out["plan"][0]["result"]
        # Observation 真的回到了模型手里（第二轮的用户消息里带上了）
        assert "Observation" in llm.seen[1]
        assert out["current_step"] == 1  # 计划跑完 → 指针推到队尾

    @pytest.mark.unit
    async def test_no_action_means_answer_directly(self, scripted_executor):
        """验收 3 下半：没有 Action 就是直接回答 —— **一次工具都不该调**。"""
        llm, spy = scripted_executor(["Thought: 上下文已足够\nFinal Answer: 未在已上传文献中找到相关内容。"])
        import app.agents.nodes.executor as m

        out = await m.executor_node(_step_state())

        assert spy.calls == []
        assert llm.i == 1, "直接回答只需一次模型调用"
        assert out["plan"][0]["status"] == "done"
        assert "未在已上传文献中找到" in out["plan"][0]["result"]

    @pytest.mark.unit
    async def test_tool_failure_is_fed_back_not_raised(self, scripted_executor):
        """工具炸了不能把节点带崩 —— 要作为 Observation 回喂，让模型换策略。"""
        llm, spy = scripted_executor(
            [
                'Action: arxiv_search\nAction Input: {"query": "x"}',
                "Final Answer: 外部检索不可用，改用本地语料。",
            ],
            arxiv_search=RuntimeError("503"),
        )
        import app.agents.nodes.executor as m

        out = await m.executor_node(_step_state())

        assert "工具调用失败" in llm.seen[1]
        assert out["plan"][0]["status"] == "done"

    @pytest.mark.unit
    async def test_react_exhaustion_marks_the_step_failed(self, scripted_executor, settings):
        """轮次用尽仍未收敛 → status=failed。这是 replanner 唯一的触发条件。"""
        action = 'Action: arxiv_search\nAction Input: {"query": "x"}'
        scripted_executor([action] * settings.AGENT_MAX_REACT_ROUNDS, arxiv_search=RuntimeError("503"))
        import app.agents.nodes.executor as m

        out = await m.executor_node(_step_state())

        assert out["plan"][0]["status"] == "failed"
        assert out["current_step"] == 1

    @pytest.mark.unit
    async def test_llm_failure_also_marks_the_step_failed(self, monkeypatch):
        import app.agents.nodes.executor as m

        class _Boom:
            async def complete(self, *a: Any, **kw: Any) -> str:
                raise RuntimeError("模型不可达")

        monkeypatch.setattr(m, "get_llm", lambda: _Boom())
        out = await m.executor_node(_step_state())

        assert out["plan"][0]["status"] == "failed"

    @pytest.mark.unit
    async def test_parallel_group_runs_concurrently(self, scripted_executor):
        """无依赖 + 同 `parallel_group` 的步骤真的并发跑（而非只是标了标签）。"""
        action = 'Action: python_exec\nAction Input: {"code": "1+1"}'
        llm, spy = scripted_executor([action, action, "Final Answer: 单路结论"])
        import app.agents.nodes.executor as m

        state = _step_state(
            plan=[
                {"idx": 1, "goal": "方法差异", "tool": "python_exec", "status": "pending", "parallel_group": "agg"},
                {"idx": 2, "goal": "指标差异", "tool": "python_exec", "status": "pending", "parallel_group": "agg"},
            ]
        )
        out = await m.executor_node(state)

        assert len(spy.calls) == 2, "两个就绪的同组步骤都该被调度"
        assert spy.peak_concurrency == 2, "同一并行组的步骤没有被并发执行"
        assert [s["status"] for s in out["plan"]] == ["done", "done"]
        assert out["current_step"] == 2

    @pytest.mark.unit
    async def test_unmet_dependency_blocks_execution(self, scripted_executor):
        """依赖没做完的步骤不许执行 —— 否则 DAG 只是装饰。"""
        llm, spy = scripted_executor(["Final Answer: x"])
        import app.agents.nodes.executor as m

        state = _step_state(
            plan=[
                {"idx": 1, "goal": "取证据", "tool": "retrieve_papers", "status": "failed"},
                {"idx": 2, "goal": "汇总", "tool": "python_exec", "status": "pending", "dependencies": [1]},
            ]
        )
        out = await m.executor_node(state)

        assert llm.i == 0 and spy.calls == []
        # 被依赖卡住时节点不动计划，只把指针推到队尾交给 replanner
        assert "plan" not in out, "没有可执行步骤时不该回写计划"
        assert out["current_step"] == 2

    @pytest.mark.unit
    async def test_ready_and_batch_selection_is_pure(self):
        import app.agents.nodes.executor as m

        plan = _dag_steps()
        for s in plan:
            s["status"] = "done"
        plan[1]["status"] = "pending"  # 第 2 步被单独重开
        assert m.ready_indices(plan) == [1]
        assert m.next_batch(plan) == [1]

    @pytest.mark.unit
    async def test_reflection_hint_is_injected_on_refine(self, scripted_executor):
        """被评审打回后，fix_hint 必须出现在 executor 的输入里 —— 否则 refine 只是重跑一遍。"""
        llm, _ = scripted_executor(["Final Answer: 改好了"])
        import app.agents.nodes.executor as m

        state = _step_state(refine_round=1, reflection={"verdict": "refine", "fix_hint": "删掉 0.83 这个数字"})
        out = await m.executor_node(state)

        assert "删掉 0.83 这个数字" in llm.seen[0]
        assert out["plan"][0]["status"] == "done"

    @pytest.mark.unit
    async def test_fix_hint_is_not_injected_on_the_first_pass(self, scripted_executor):
        """首轮没有评审意见，不该冒出"上一稿被打回"的提示词去干扰模型。"""
        llm, _ = scripted_executor(["Final Answer: 初稿"])
        import app.agents.nodes.executor as m

        await m.executor_node(_step_state(reflection={"fix_hint": "不该出现"}))
        assert "上一稿被评审打回" not in llm.seen[0]


# ==================================================================== 4. Reflector
def _plan_untouched(plan: list[dict[str, Any]]) -> bool:
    """反射节点返回的是新列表，原 plan 应保持原样（避免隐蔽的原地修改）。"""
    return plan[0]["status"] == "done"


class TestReflector:
    @pytest.mark.unit
    def test_injected_hallucination_is_caught_even_with_a_good_average(self):
        """验收 4：把幻觉注入进草稿（faithfulness 掉到 0.3），其余三项给满分。

        加权平均是 0.72 —— 低于 0.75，被总分挡下了。但真正该拦它的理由是
        faithfulness 一票否决，所以下面再验一条"总分够高也照样打回"。
        """
        from app.agents.nodes.reflector import score_of

        overall, verdict = score_of({"faithfulness": 0.3, "relevance": 1.0, "coherence": 1.0, "completeness": 1.0})
        assert overall < 0.75
        assert verdict == "refine"

    @pytest.mark.unit
    def test_high_average_cannot_buy_back_a_fabrication(self):
        """总分 0.84 ★高于阈值★，但 faithfulness=0.6（引了不存在的 chunk_id）→ 仍须 refine。"""
        from app.agents.nodes.reflector import score_of

        scores = {"faithfulness": 0.6, "relevance": 1.0, "coherence": 1.0, "completeness": 1.0}
        overall, verdict = score_of(scores)
        assert overall >= 0.75, "这条用例的前提就是总分够高"
        assert verdict == "refine"

    @pytest.mark.unit
    def test_relevance_floor_is_a_separate_gate(self):
        """答非所问（relevance 0.5）也要打回 —— 这条只靠 faithfulness 门限拦不住。"""
        from app.agents.nodes.reflector import score_of

        overall, verdict = score_of({"faithfulness": 0.95, "relevance": 0.5, "coherence": 1.0, "completeness": 1.0})
        assert overall >= 0.75
        assert verdict == "refine"

    @pytest.mark.unit
    def test_clean_draft_passes(self):
        from app.agents.nodes.reflector import score_of

        overall, verdict = score_of({"faithfulness": 0.9, "relevance": 0.9, "coherence": 0.9, "completeness": 0.9})
        assert overall >= 0.75
        assert verdict == "pass"

    @pytest.mark.unit
    async def test_node_surfaces_the_breached_floor_in_the_critique(self, monkeypatch):
        """节点级：幻觉草稿 → verdict=refine，且 critique 里点名是哪一项掉线。

        模型自己的 critique 未必写到"faithfulness 不够"，所以硬门限要自己标出来，
        否则下一轮 executor 不知道往哪改。
        """
        import app.agents.nodes.reflector as m

        async def fake(schema, messages, **kw):  # noqa: ARG001
            return m.ReflectionResult(
                scores={"faithfulness": 0.3, "relevance": 0.9, "coherence": 0.85, "completeness": 0.7},
                critique="第 2 段引用了上下文里不存在的 chunk_d9",
                fix_hint="删掉无出处的结论",
            )

        monkeypatch.setattr(m, "complete_structured", fake)
        out = await m.reflector_node({"query": "q", "draft": "编造了一段结论 chunk_d9"})

        assert out["reflection"]["verdict"] == "refine"
        assert "faithfulness=0.30 < 0.7" in out["reflection"]["critique"]
        assert out["refine_round"] == 1
        assert out["current_step"] == 0

    @pytest.mark.unit
    async def test_refine_reopens_the_plan(self, monkeypatch):
        """refine 必须把已完成的步骤重新打开成 pending。

        否则 executor 按"就绪的 pending 步骤"挑活时会认为无事可做，refine 循环空转
        （指针拨回 0 但没有任何步骤可执行）。
        """
        import app.agents.nodes.reflector as m

        async def fake(schema, messages, **kw):  # noqa: ARG001
            return m.ReflectionResult(scores=dict.fromkeys(m.WEIGHTS, 0.2), critique="重写")

        monkeypatch.setattr(m, "complete_structured", fake)
        plan = [{"idx": 1, "goal": "g", "tool": "t", "status": "done", "result": "旧稿"}]
        out = await m.reflector_node({"query": "q", "draft": "草稿", "plan": plan})

        assert out["plan"][0]["status"] == "pending"
        assert state_status_preserved(plan), "不该原地改到输入的那份 plan"

    @pytest.mark.unit
    async def test_empty_plan_markdown_draft_still_refines(self, monkeypatch):
        """草稿为空时不该去问模型 —— 直接判重写。"""
        import app.agents.nodes.reflector as m

        async def never(*a, **kw):  # noqa: ARG001
            raise AssertionError("空草稿不该调评审模型")

        monkeypatch.setattr(m, "complete_structured", never)
        out = await m.reflector_node({"query": "q", "draft": "  "})
        assert out["reflection"]["verdict"] == "refine"


def state_status_preserved(plan: list[dict[str, Any]]) -> bool:
    """反射节点返回的是新列表，原 plan 应保持原样（避免隐蔽的原地修改）。"""
    return plan[0]["status"] == "done"


# ==================================================================== 5. Replanner
def _failed_plan() -> list[dict[str, Any]]:
    return [
        {"idx": 1, "goal": "取证据", "tool": "retrieve_papers", "status": "done", "result": "1 个片段"},
        {"idx": 2, "goal": "外部检索", "tool": "arxiv_search", "status": "failed", "result": "503"},
    ]


class TestReplanner:
    @pytest.mark.unit
    async def test_tool_failure_triggers_replan(self, monkeypatch):
        """验收 5 上半：有 failed 步骤 → 允许（且应当）replan。"""
        import app.agents.nodes.replanner as m

        seen: list[str] = []

        async def fake(schema, messages, **kw):  # noqa: ARG001
            seen.append(_user_text(list(messages)))
            return m.ReplanResult(
                decision="replan",
                reason="第 2 步 arxiv 不可用",
                new_steps=[{"idx": 1, "goal": "改用本地语料作答", "tool": "python_exec"}],
            )

        monkeypatch.setattr(m, "complete_structured", fake)
        out = await m.replanner_node({"query": "q", "plan": _failed_plan(), "current_step": 2})

        assert out["plan_round"] == 1
        assert [s["status"] for s in out["plan"]] == ["done", "replanned", "pending"]
        assert out["plan"][2]["idx"] == 3  # 尾追：idx 重排到队尾
        assert out["current_step"] == 2  # 指向第一个新步骤
        assert out["trace"][0]["decision"] == "replan"

    @pytest.mark.unit
    async def test_reflection_history_reaches_the_model(self, monkeypatch):
        """Reflexion：工具都成功但产出不忠实的偏差，只能靠评审记录传下去。"""
        import app.agents.nodes.replanner as m

        seen: list[str] = []

        async def fake(schema, messages, **kw):  # noqa: ARG001
            seen.append(_user_text(list(messages)))
            return m.ReplanResult(decision="continue")

        monkeypatch.setattr(m, "complete_structured", fake)
        state = {
            "query": "q",
            "plan": _failed_plan(),
            "current_step": 2,
            "trace": [
                {
                    "node": "reflector",
                    "round": 0,
                    "verdict": "refine",
                    "scores": {"faithfulness": 0.3},
                    "critique": "编造",
                },
            ],
        }
        await m.replanner_node(state)

        assert "faithfulness=0.30" in seen[0]
        assert "编造" in seen[0]

    @pytest.mark.unit
    async def test_no_reflection_history_is_stated_explicitly(self, monkeypatch):
        import app.agents.nodes.replanner as m

        seen: list[str] = []

        async def fake(schema, messages, **kw):  # noqa: ARG001
            seen.append(_user_text(list(messages)))
            return m.ReplanResult(decision="continue")

        monkeypatch.setattr(m, "complete_structured", fake)
        await m.replanner_node({"query": "q", "plan": _failed_plan(), "current_step": 2})
        assert "（无）" in seen[0]

    @pytest.mark.unit
    async def test_exhausted_budget_forces_finish_without_asking_the_model(self, monkeypatch, settings):
        """验收 5 下半：轮次用尽 → 不问模型、不再 replan，指针推到队尾收口。

        "强制 finish" 的落点就在这里：预算判断在**调用模型之前**，所以模型连
        一次"再 replan 一次"的机会都没有。
        """
        import app.agents.nodes.replanner as m

        async def never(*a, **kw):  # noqa: ARG001
            raise AssertionError("预算用尽后不该再问模型")

        monkeypatch.setattr(m, "complete_structured", never)
        out = await m.replanner_node(
            {"query": "q", "plan": _failed_plan(), "current_step": 2, "plan_round": settings.REPLAN_MAX_ROUNDS}
        )

        assert out["current_step"] == len(_failed_plan())
        assert out["trace"][0]["decision"] == "continue"
        assert "预算用尽" in out["trace"][0]["reason"]
        assert "plan" not in out, "预算用尽时不该动计划"

    @pytest.mark.unit
    async def test_no_open_steps_short_circuits(self, monkeypatch):
        """全做完 → 不问模型。省一次调用，也避免模型硬要 replan 加步骤。"""
        import app.agents.nodes.replanner as m

        async def never(*a, **kw):  # noqa: ARG001
            raise AssertionError("没有未完成步骤时不该问模型")

        monkeypatch.setattr(m, "complete_structured", never)
        plan = [{"idx": 1, "goal": "g", "tool": "t", "status": "done"}]
        out = await m.replanner_node({"query": "q", "plan": plan, "current_step": 1})

        assert out["current_step"] == 1
        assert out["trace"][0]["reason"] == "无未完成步骤"

    @pytest.mark.unit
    def test_replanned_steps_do_not_trigger_another_round(self):
        """被取代的失败步骤状态是 replanned，既不算 done 也不算未完成 ——

        漏掉这条会让每一轮重规划都再触发一次重规划，直到预算耗尽。
        """
        from app.agents.nodes.replanner import OPEN_STATUSES

        assert "replanned" not in OPEN_STATUSES
        plan = [
            {"idx": 1, "goal": "a", "tool": "t", "status": "replanned"},
            {"idx": 2, "goal": "b", "tool": "t", "status": "done"},
        ]
        assert [s for s in plan if s["status"] in OPEN_STATUSES] == []

    @pytest.mark.unit
    async def test_model_failure_keeps_the_original_plan(self, monkeypatch):
        import app.agents.nodes.replanner as m

        async def boom(*a, **kw):  # noqa: ARG001
            raise RuntimeError("超时")

        monkeypatch.setattr(m, "complete_structured", boom)
        out = await m.replanner_node({"query": "q", "plan": _failed_plan(), "current_step": 2})

        assert out["trace"][0]["decision"] == "continue"
        assert "plan" not in out


# ==================================================================== 6. Prompt 文件
class TestPrompts:
    @pytest.mark.unit
    def test_all_core_prompts_are_separate_files(self):
        """验收 6：每个节点的 prompt 都是独立 .md，可以单独迭代。"""
        names = set(available())
        assert set(CORE_PROMPTS) <= names, f"缺 prompt 文件：{set(CORE_PROMPTS) - names}"

    @pytest.mark.unit
    @pytest.mark.parametrize("name", CORE_PROMPTS)
    def test_prompt_renders_without_leftover_placeholders(self, name):
        """节点把 query / context 拼在 user 消息里，prompt 自身不该留未替换的占位。"""
        text = render(name)
        assert len(text.strip()) > 200
        assert "{{" not in text, f"{name}.md 里有没被替换的占位符"

    @pytest.mark.unit
    def test_intent_prompt_is_few_shot_for_all_eight_classes(self):
        text = render("intent")
        for intent in EXPECTED_INTENTS:
            assert intent in text, f"intent.md 没覆盖 {intent}"
        # 每类至少一个"用户 → JSON"示例（16 条示例 + 5 条边界）
        assert text.count("→ `{") >= 8
        assert "Chain of Thought" in text
        assert "边界案例" in text

    @pytest.mark.unit
    def test_planner_prompt_teaches_the_dag_fields(self):
        text = render("planner")
        for token in ("dependencies", "parallel_group", "3–5 步", "DAG"):
            assert token in text, f"planner.md 缺少 {token}"

    @pytest.mark.unit
    def test_executor_prompt_states_the_citation_triple_and_the_refusal_phrase(self):
        """反幻觉约束是 prompt 层的硬契约，改这里等于改生成侧的行为。"""
        text = render("executor")
        assert "[paper_id:page:chunk_id]" in text
        assert "未在已上传文献中找到" in text

    @pytest.mark.unit
    def test_reflector_prompt_documents_both_hard_floors(self):
        text = render("reflector")
        assert "一票否决" in text
        assert "≥ 0.8" in text and "≥ 0.6" in text

    @pytest.mark.unit
    def test_replanner_prompt_matches_the_tail_append_semantics(self):
        """prompt 里不能还写着"替换剩余步骤"—— 实现是尾追追加。"""
        text = render("replanner")
        assert "尾追" in text and "replanned" in text
        assert "最多重规划 2 轮" in text


# ==================================================================== 结构自检
@pytest.mark.unit
def test_prompt_renderer_caches_but_returns_the_file_content():
    """`load()` 带 lru_cache —— 开发期改完 prompt 要重启进程才生效，这里钉住这个语义。"""
    from app.agents.prompts import load

    assert load("intent") == render("intent")
    with pytest.raises(FileNotFoundError):
        load("no_such_prompt")


@pytest.mark.unit
def test_intent_result_serializes_to_json():
    """意图结果要能落进 trace / checkpointer（不能是 numpy / 自定义对象）。"""
    from app.agents.nodes.intent import IntentResult

    payload = IntentResult(intent="translation", confidence=0.88, slot_filling={"lang_pair": "en->zh"}).model_dump()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["slot_filling"] == {"lang_pair": "en->zh"}
