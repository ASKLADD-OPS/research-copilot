"""LangGraph 图定义 —— 三范式融合在一个有向循环图里。

```
                    ┌─────────────┐
   START ──────────▶│   intent    │
                    └──────┬──────┘
             ┌─────────────┼──────────────┐
      conf<0.6        chitchat        conf>=0.6
             ▼             │              ▼
       ┌──────────┐        │       ┌─────────────┐
       │ clarify  │        │       │   planner   │◀────────┐
       └────┬─────┘        │       └──────┬──────┘         │
            ▼              │    ┌─────────┼────────┐        │
           END             │    ▼         ▼        ▼        │
                        ┌──┴────┐  retriever  tool  executor │
                        │ synth │
                        └───┬───┘  └────┬─────┘  │    ▲       │
                            │           ▼        │    │       │
                            │        executor ───┘    │       │
                            │           │  ▲          │       │
                            │           ▼  │loop      ▼       │
                            │      ┌──────────┐  ┌──────────┐  │
                            │      │ replanner│─▶│ reflector│  │
                            │      └──────────┘  └────┬─────┘  │
                            │           │  refine ◀───┘  ▲     │
                            │           └────────────────┼─────┘
                            ▼                            │
                      ┌────────────┐                     │
                      │ guardrails │◀────────────────────┘
                      └──────┬─────┘        pass / verdict!=refine
                             ▼
                            END
```

三处循环都在图上显式表达（不藏进节点内部），便于观测与断言：
1. `executor → executor`：计划内多步
2. `executor → replanner → executor`：Replan 循环（上限 2）
3. `reflector → executor`：Refine 循环（上限 2）
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.checkpointer import build_checkpointer
from app.agents.nodes import (
    clarify_node,
    executor_node,
    guardrails_node,
    intent_node,
    planner_node,
    reflector_node,
    replanner_node,
    retriever_node,
    route_after_execute,
    route_after_intent,
    route_after_reflect,
    route_after_replan,
    route_after_retrieve,
    route_after_tool,
    synthesizer_node,
    tool_node,
)
from app.agents.state import AgentState
from app.core.logging import logger

# 计划里若只有一步且是这些工具，直接走 tool 节点（确定性调用，无需 LLM 参与）
DIRECT_TOOLS = {
    "arxiv_search",
    "pubmed_search",
    "semantic_scholar_search",
    "python_exec",
    "graph_analyze",
    "make_chart",
}


def route_after_planner_plan(state: AgentState) -> str:
    """规划后的分流：检索专用节点 / 直连工具 / 通用执行器 / 直接收尾。"""
    plan = state.get("plan") or []
    if not plan:
        return "synthesizer"
    first = plan[0]
    tool = str(first.get("tool") or "")
    if tool == "retrieve_papers":
        return "retriever"
    if len(plan) == 1 and tool in DIRECT_TOOLS:
        return "tool"
    return "executor"


def build_graph(
    *,
    with_checkpointer: bool = True,
    checkpointer: Any = None,
    human_in_the_loop: bool = False,
) -> Any:
    """构建图。

    - `with_checkpointer=False`：单测用，不落库。
    - `checkpointer=`：显式注入（单测给 MemorySaver，免 Postgres）。
    - `human_in_the_loop=True`：在 executor 前 `interrupt_before`，让用户改完计划再恢复。
      **默认关闭** —— `/chat/stream` 靠一次 `astream` 跑到底，默认开中断会让 SSE 每轮都停在
      半途（前端只会看到一帧都没吐完就结束）。只有需要人工审批计划的入口才开。
    """
    g = StateGraph(AgentState)

    # ---- 节点 ----
    g.add_node("intent", intent_node)
    g.add_node("clarify", clarify_node)
    g.add_node("planner", planner_node)
    g.add_node("retriever", retriever_node)
    g.add_node("tool", tool_node)
    g.add_node("executor", executor_node)
    g.add_node("replanner", replanner_node)
    g.add_node("reflector", reflector_node)
    g.add_node("synthesizer", synthesizer_node)
    g.add_node("guardrails", guardrails_node)

    # ---- 入口 ----
    g.add_edge(START, "intent")
    g.add_conditional_edges(
        "intent",
        route_after_intent,
        {"clarify": "clarify", "planner": "planner", "synthesizer": "synthesizer"},
    )
    g.add_edge("clarify", END)

    # ---- 规划 ----
    g.add_conditional_edges(
        "planner",
        route_after_planner_plan,
        {
            "retriever": "retriever",
            "tool": "tool",
            "executor": "executor",
            "synthesizer": "synthesizer",
        },
    )

    # ---- 循环 1：计划内多步 ----
    g.add_conditional_edges(
        "retriever",
        route_after_retrieve,
        {"executor": "executor", "synthesizer": "synthesizer"},
    )
    g.add_conditional_edges(
        "tool",
        route_after_tool,
        {"tool": "tool", "reflector": "reflector"},
    )
    g.add_conditional_edges(
        "executor",
        route_after_execute,
        {"executor": "executor", "replanner": "replanner"},
    )

    # ---- 循环 2：Replan ----
    g.add_conditional_edges(
        "replanner",
        route_after_replan,
        {"executor": "executor", "reflector": "reflector"},
    )

    # ---- 循环 3：Refine ----
    g.add_conditional_edges(
        "reflector",
        route_after_reflect,
        {"refine": "executor", "synthesizer": "synthesizer"},
    )

    # ---- 收尾 ----
    g.add_edge("synthesizer", "guardrails")
    g.add_edge("guardrails", END)

    cp = checkpointer if checkpointer is not None else (build_checkpointer() if with_checkpointer else None)
    compiled = g.compile(
        checkpointer=cp,
        interrupt_before=["executor"] if human_in_the_loop else None,
    )
    logger.info(
        "Agent 图编译完成（checkpointer={} hitl={}）",
        type(cp).__name__ if cp else "None",
        human_in_the_loop,
    )
    return compiled


_graph: Any | None = None


def get_graph() -> Any:
    """进程内单例。图编译较重，且 checkpointer 需要复用连接池。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def graph_mermaid(with_checkpointer: bool = False) -> str:
    """导出 Mermaid 图，供 /api/v1/tools/graph 展示与文档引用。"""
    try:
        return build_graph(with_checkpointer=with_checkpointer).get_graph().draw_mermaid()
    except Exception as exc:  # noqa: BLE001
        logger.warning("导出 Mermaid 失败: {}", exc)
        return ""


# 注意：**不要**在模块级写 `graph = get_graph()`。
# 那会让 `import app.agents.graph` 就触发 checkpointer 连接（要求 Postgres 已在跑），
# 单测与 `python -c "import app.main"` 都会挂。需要入口时显式调 get_graph()。

__all__ = [
    "DIRECT_TOOLS",
    "build_graph",
    "get_graph",
    "graph_mermaid",
    "route_after_planner_plan",
]
