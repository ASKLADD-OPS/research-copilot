"""图路由条件边测试。

三处循环（计划内多步 / Replan / Refine）都是显式的条件边，路由函数就是
"循环会不会失控"的唯一开关。全部是纯字典 → 字符串的函数，可以逐个钉死，
不需要跑模型、不需要连库。
"""

from __future__ import annotations

import pytest

from app.agents.graph import DIRECT_TOOLS, route_after_planner_plan
from app.agents.nodes import (
    route_after_execute,
    route_after_intent,
    route_after_reflect,
    route_after_replan,
    route_after_retrieve,
    route_after_tool,
)
from app.agents.nodes.executor import parse_action
from app.agents.nodes.intent import CONFIDENCE_FLOOR


# ---------------------------------------------------------------- 意图分流
@pytest.mark.unit
def test_confidence_floor_is_the_documented_value():
    assert CONFIDENCE_FLOOR == 0.6


@pytest.mark.unit
@pytest.mark.parametrize("confidence", [0.0, 0.31, 0.599])
def test_low_confidence_goes_to_clarify(confidence):
    assert route_after_intent({"confidence": confidence, "intent": "single_paper_qa"}) == "clarify"


@pytest.mark.unit
def test_confidence_exactly_at_floor_is_accepted():
    """边界：== 0.6 不算低置信，不应该白白多问一轮。"""
    assert route_after_intent({"confidence": 0.6, "intent": "literature_search"}) == "planner"


@pytest.mark.unit
def test_chitchat_skips_planner():
    assert route_after_intent({"confidence": 0.95, "intent": "chitchat"}) == "synthesizer"


@pytest.mark.unit
def test_missing_confidence_defaults_to_clarify():
    """state 里没有 confidence（异常路径）时应该保守追问，而不是硬猜。"""
    assert route_after_intent({}) == "clarify"


@pytest.mark.unit
@pytest.mark.parametrize(
    "intent",
    [
        "single_paper_qa",
        "cross_paper_reasoning",
        "literature_search",
        "graph_analysis",
        "writing_assist",
        "visualization",
        "translation",
    ],
)
def test_all_working_intents_go_to_planner(intent):
    """8 类意图里除 chitchat 外全部进规划 —— 少一类就会被静默降级成闲聊。"""
    assert route_after_intent({"confidence": 0.9, "intent": intent}) == "planner"


# ---------------------------------------------------------------- 规划后分流
@pytest.mark.unit
def test_empty_plan_short_circuits_to_synthesizer():
    assert route_after_planner_plan({"plan": []}) == "synthesizer"
    assert route_after_planner_plan({}) == "synthesizer"


@pytest.mark.unit
def test_retrieve_as_first_step_uses_dedicated_retriever():
    """检索是唯一带 CRAG 降级的节点，必须走它而不是通用 executor。"""
    state = {"plan": [{"tool": "retrieve_papers", "goal": "取证据"}]}
    assert route_after_planner_plan(state) == "retriever"


@pytest.mark.unit
def test_single_direct_tool_step_skips_executor():
    """单步直连工具不需要 LLM 参与 —— 省一次调用，也避免模型把参数改坏。"""
    for tool in DIRECT_TOOLS:
        state = {"plan": [{"tool": tool, "goal": "调用"}]}
        assert route_after_planner_plan(state) == "tool", tool


@pytest.mark.unit
def test_multi_step_plan_uses_executor():
    state = {
        "plan": [
            {"tool": "retrieve_papers", "goal": "取证据"},
            {"tool": "arxiv_search", "goal": "补充检索"},
        ]
    }
    # 首步是检索 → 先 retriever；这一步之后才交给 executor
    assert route_after_planner_plan(state) == "retriever"

    state2 = {"plan": [{"tool": "arxiv_search"}, {"tool": "write_section"}]}
    assert route_after_planner_plan(state2) == "executor"


@pytest.mark.unit
def test_unknown_tool_goes_to_executor():
    state = {"plan": [{"tool": "some_new_tool"}]}
    assert route_after_planner_plan(state) == "executor"


@pytest.mark.unit
def test_two_step_plan_starting_with_direct_tool_still_uses_executor():
    """len(plan) > 1 时即使首步是直连工具也走 executor —— 直连快路径只给单步计划。"""
    state = {"plan": [{"tool": "arxiv_search"}, {"tool": "arxiv_search"}]}
    assert route_after_planner_plan(state) == "executor"


# ---------------------------------------------------------------- 循环 1：计划内多步
@pytest.mark.unit
def test_executor_loops_until_plan_exhausted():
    plan = [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}]
    assert route_after_execute({"plan": plan, "current_step": 0}) == "executor"
    assert route_after_execute({"plan": plan, "current_step": 1}) == "executor"
    # 计划跑完交给 replanner（它再决定 re-plan 还是转 reflector），不是直接进 reflector。
    # 直接返回 "reflector" 会让 LangGraph 在边表里查不到目标而抛 KeyError。
    assert route_after_execute({"plan": plan, "current_step": 3}) == "replanner"


@pytest.mark.unit
def test_tool_node_loops_until_plan_exhausted():
    plan = [{"goal": "a"}, {"goal": "b"}]
    assert route_after_tool({"plan": plan, "current_step": 1}) == "tool"
    assert route_after_tool({"plan": plan, "current_step": 2}) == "reflector"


@pytest.mark.unit
def test_retriever_handoff_rules():
    plan = [{"goal": "取证据"}, {"goal": "写作"}]
    assert route_after_retrieve({"plan": plan, "current_step": 1}) == "executor"
    # 只检索一步的计划直接收尾：没有草稿的评审是空转
    assert route_after_retrieve({"plan": plan[:1], "current_step": 1}) == "synthesizer"


# ---------------------------------------------------------------- 循环 2：Replan
@pytest.mark.unit
def test_replan_continues_or_hands_to_reviewer():
    plan = [{"goal": "a"}, {"goal": "b"}]
    assert route_after_replan({"plan": plan, "current_step": 0}) == "executor"
    assert route_after_replan({"plan": plan, "current_step": 2}) == "reflector"


# ---------------------------------------------------------------- 循环 3：Refine
@pytest.mark.unit
def test_reflect_refines_only_when_verdict_says_so():
    state = {"reflection": {"verdict": "refine"}, "refine_round": 0}
    assert route_after_reflect(state) == "refine"

    state = {"reflection": {"verdict": "pass"}, "refine_round": 0}
    assert route_after_reflect(state) == "synthesizer"


@pytest.mark.unit
def test_refine_round_limit_is_enforced(settings):
    """轮次用尽必须收手，否则 executor ↔ reflector 会无限打转。"""
    limit = settings.REFLECTION_MAX_REFINE
    assert limit == 2
    at_limit = {"reflection": {"verdict": "refine"}, "refine_round": limit}
    assert route_after_reflect(at_limit) == "synthesizer"


@pytest.mark.unit
def test_reflect_without_reflection_dict_terminates():
    assert route_after_reflect({}) == "synthesizer"
    assert route_after_reflect({"reflection": None}) == "synthesizer"


# ---------------------------------------------------------------- ReAct 动作解析
@pytest.mark.unit
def test_parse_action_extracts_tool_and_args():
    text = 'Thought: 需要查文献\nAction: arxiv_search\nAction Input: {"query": "retrieval augmented generation"}'
    assert parse_action(text) == ("arxiv_search", {"query": "retrieval augmented generation"})


@pytest.mark.unit
def test_parse_action_returns_none_without_action():
    assert parse_action("Thought: 我直接回答") is None
    assert parse_action("") is None


@pytest.mark.unit
def test_parse_action_rejects_broken_json():
    """JSON 坏掉必须返回 None（让上层重试），不能吞掉半截参数。"""
    text = "Action: python_exec\nAction Input: {not json}"
    assert parse_action(text) is None


@pytest.mark.unit
def test_parse_action_requires_object_action_input():
    """Action Input 只认 `{...}`。数组/标量一律当作"没解析出动作"返回 None，
    让上层重新提问，而不是把裸值塞进工具参数里。"""
    assert parse_action("Action: python_exec\nAction Input: [1, 2, 3]") is None
    assert parse_action('Action: python_exec\nAction Input: "some text"') is None


@pytest.mark.unit
def test_parse_action_accepts_chinese_colon_and_empty_object():
    assert parse_action("Action：arxiv_search\nAction Input：{}") == ("arxiv_search", {})


# ---------------------------------------------------------------- 图结构
@pytest.mark.unit
def test_graph_compiles_without_checkpointer():
    """不落库编译一遍：能编出来说明节点/边的声明是自洽的。"""
    from app.agents.graph import build_graph

    compiled = build_graph(with_checkpointer=False)
    nodes = set(compiled.get_graph().nodes)
    assert {
        "intent",
        "clarify",
        "planner",
        "retriever",
        "tool",
        "executor",
        "replanner",
        "reflector",
        "synthesizer",
        "guardrails",
    } <= nodes


@pytest.mark.unit
def test_graph_declares_three_loops():
    """三处循环必须画在图上，而不是藏在节点内部 —— 否则无法观测与断言。"""
    from app.agents.graph import build_graph

    pairs = {(e.source, e.target) for e in build_graph(with_checkpointer=False).get_graph().edges}
    assert ("executor", "executor") in pairs  # 计划内多步
    assert ("executor", "replanner") in pairs  # Replan 循环入口
    assert ("replanner", "executor") in pairs  # Replan 回到执行
    assert ("reflector", "executor") in pairs  # Refine 循环
    assert ("synthesizer", "guardrails") in pairs


@pytest.mark.unit
def test_direct_tools_set_matches_registry_expectations():
    """直连工具必须是本地/远程**都真实存在**的名字，否则会路由到空节点。"""
    assert "retrieve_papers" not in DIRECT_TOOLS  # 检索有专门节点，不在直连集合里
    assert {"arxiv_search", "pubmed_search", "semantic_scholar_search", "python_exec"} <= DIRECT_TOOLS


# ---------------------------------------------------------------- 边表契约
@pytest.mark.unit
def test_every_router_output_is_declared_in_the_edge_table():
    """路由器返回的名字必须真的出现在该源节点的条件边表里。

    这是一个**只在 runtime 才会炸**的坑：LangGraph 拿路由函数的返回值去查边表，
    查不到直接抛 `KeyError`（`langgraph/graph/_branch.py` 的 `self.ends[r]`），
    整轮 SSE 只剩一帧 error。上面的逐个断言抓不到它 —— 它们验证的是"路由器返回了
    什么"，而不是"图认不认这个名字"。

    实际踩过：`route_after_execute` 返回 `"reflector"`，但 executor 的边表只声明了
    `{"executor", "replanner"}` —— 于是 writing_assist / translation / visualization
    这些走 Planner 的意图，第一轮执行完就崩。

    例外：reflector 的路由函数返回的是判定名（refine），边表里 "refine" 映射到 executor，
    所以它不适用"返回值即节点名"这条，由 test_reflect_refines_only_when_verdict_says_so 单独覆盖。
    """
    from app.agents.graph import build_graph

    declared: dict[str, set[str]] = {}
    for e in build_graph(with_checkpointer=False).get_graph().edges:
        declared.setdefault(e.source, set()).add(e.target)

    two_steps = [{"goal": "a"}, {"goal": "b"}]
    cases = [
        (
            "intent",
            route_after_intent,
            [{}, {"confidence": 0.9, "intent": "literature_search"}, {"confidence": 0.95, "intent": "chitchat"}],
        ),
        (
            "planner",
            route_after_planner_plan,
            [
                {},
                {"plan": [{"tool": "arxiv_search"}]},
                {"plan": [{"tool": "retrieve_papers"}]},
                {"plan": [{"tool": "some_new_tool"}]},
            ],
        ),
        (
            "retriever",
            route_after_retrieve,
            [{"plan": two_steps, "current_step": 1}, {"plan": two_steps[:1], "current_step": 1}],
        ),
        ("tool", route_after_tool, [{"plan": two_steps, "current_step": 1}, {"plan": two_steps, "current_step": 2}]),
        (
            "executor",
            route_after_execute,
            [{"plan": two_steps, "current_step": 0}, {"plan": two_steps, "current_step": 2}],
        ),
        (
            "replanner",
            route_after_replan,
            [{"plan": two_steps, "current_step": 0}, {"plan": two_steps, "current_step": 2}],
        ),
    ]
    for source, router, states in cases:
        for state in states:
            got = router(state)
            assert got in declared[source], (
                f"{router.__name__} 返回 {got!r}，但 {source} 的边表只有 {sorted(declared[source])} —— "
                f"LangGraph 会在 runtime 抛 KeyError，请改路由函数或补边表"
            )
