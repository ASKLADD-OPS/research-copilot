"""Agent 节点 —— 11 个节点，按图上的位置排列。

入口：intent → (clarify | planner)
主干：planner → executor(ReAct) → replanner → reflector → (refine | synthesizer)
收尾：synthesizer → guardrails
"""

from app.agents.nodes.clarify import clarify_node
from app.agents.nodes.executor import executor_node, route_after_execute
from app.agents.nodes.guardrails import guardrails_node
from app.agents.nodes.intent import intent_node, route_after_intent
from app.agents.nodes.planner import planner_node, route_after_planner
from app.agents.nodes.reflector import reflector_node, route_after_reflect
from app.agents.nodes.replanner import replanner_node, route_after_replan
from app.agents.nodes.retriever import retriever_node, route_after_retrieve
from app.agents.nodes.synthesizer import synthesizer_node
from app.agents.nodes.tool_node import route_after_tool, tool_node

__all__ = [
    "clarify_node",
    "executor_node",
    "guardrails_node",
    "intent_node",
    "planner_node",
    "reflector_node",
    "replanner_node",
    "retriever_node",
    "route_after_execute",
    "route_after_intent",
    "route_after_planner",
    "route_after_reflect",
    "route_after_replan",
    "route_after_retrieve",
    "route_after_tool",
    "synthesizer_node",
    "tool_node",
]
