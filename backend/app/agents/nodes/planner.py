"""Planner 节点 —— Plan-and-Execute 的规划端，用高性能模型。"""

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState, PlanStep
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured


class PlanResult(BaseModel):
    # 上限 5 步：超过 5 步的"计划"基本都是把一个动作拆成两句，不是更细的规划。
    # 下限留 1 而不是 3 —— 单步直连工具是 graph 里的快路径（route_after_planner_plan），
    # 强行凑 3 步会把本该一次调用搞定的事拆成三次模型往返。
    steps: list[PlanStep] = Field(min_length=1, max_length=5)
    reasoning: str = ""


def normalize_dag(steps: list[PlanStep]) -> tuple[list[PlanStep], list[str]]:
    """把 LLM 给的 steps 收敛成一张合法 DAG。纯函数，返回 (steps, 修正说明)。

    修三件模型常犯的事：

    1. `idx` 重排成 1..n —— 模型会从 0 开始、跳号、或者三步都写 idx=1。
       依赖按**原始 idx** 解析，再映射到重排后的编号。
    2. 依赖只保留指向**更早步骤**的：自指、前向引用、越界一律丢掉。
       这条同时消灭了环 —— 只允许向后指就不可能成环，不需要单独的拓扑排序检测。
    3. 同 `parallel_group` 内若出现互相依赖，把后来者踢出该组：并行组的前提就是
       组内互不依赖，否则并发跑会读到别人的未完成结果。

    空组（成员 < 2）也去掉 —— 一个成员的"并行组"只是个标签，没有意义。
    """
    notes: list[str] = []
    staged: list[PlanStep] = []
    remap: dict[int, int] = {}  # 模型给的 idx → 重排后的 idx
    for i, raw in enumerate(steps):
        s: PlanStep = {**raw, "idx": i + 1, "status": "pending"}
        with contextlib.suppress(KeyError, TypeError, ValueError):
            remap.setdefault(int(raw["idx"]), i + 1)

        raw_deps = raw.get("dependencies") or []
        if not isinstance(raw_deps, list):
            raw_deps = []
        deps: list[int] = []
        for d in raw_deps:
            try:
                target = remap.get(int(d))
            except (TypeError, ValueError):
                target = None
            # target <= i 保证它指向的是排在本步之前的步骤（本步的重排编号是 i+1）
            if target is not None and target <= i and target not in deps:
                deps.append(target)
        if len(deps) != len(raw_deps):
            notes.append(f"第 {i + 1} 步依赖 {raw_deps} 含非法/前向引用/重复，收敛为 {deps}")
        if deps:
            s["dependencies"] = deps
        else:
            s.pop("dependencies", None)
        staged.append(s)

    # 并行组去环：组内后进者若依赖同组任一成员，就退出该组（退化为串行）
    for name in {str(s["parallel_group"]) for s in staged if s.get("parallel_group")}:
        seen: list[int] = []
        for s in staged:
            if str(s.get("parallel_group")) != name:
                continue
            if set(s.get("dependencies") or []) & set(seen):
                notes.append(f"第 {s['idx']} 步依赖同并行组 {name!r} 的成员，已退出该组改为串行")
                s.pop("parallel_group", None)
            else:
                seen.append(s["idx"])

    # 成员 < 2 的组没有并行含义，去掉（要在上一步踢人之后重新数）
    left: dict[str, int] = {}
    for s in staged:
        if s.get("parallel_group"):
            left[str(s["parallel_group"])] = left.get(str(s["parallel_group"]), 0) + 1
    for s in staged:
        name = s.get("parallel_group")
        if name and left[str(name)] < 2:
            s.pop("parallel_group", None)

    return staged, notes


def _fallback_plan(intent: str | None) -> list[PlanStep]:
    """LLM 不可用时的最小可用计划 —— 一步检索，好过整图崩掉。"""
    if intent == "chitchat":
        return [{"idx": 1, "goal": "直接回答", "tool": "none", "status": "pending"}]
    return [
        {
            "idx": 1,
            "goal": "从本地论文库检索与问题最相关的片段",
            "tool": "retrieve_papers",
            "status": "pending",
        }
    ]


async def planner_node(state: AgentState) -> dict[str, Any]:
    query = state.get("query", "")
    intent = state.get("intent")
    extra = {k: v for k, v in state.get("slot_filling", {}).items() if k != "clarify_options"}
    papers = state.get("target_papers") or []

    try:
        result = await complete_structured(
            PlanResult,
            [
                {"role": "system", "content": render("planner")},
                {
                    "role": "user",
                    "content": (f"用户需求：{query}\n意图：{intent}\n槽位：{extra}\n指定论文：{papers or '未指定'}"),
                },
            ],
            role=Role.PLANNER,  # 规划用强模型
        )
        steps, notes = normalize_dag(result.steps)
        reasoning = result.reasoning
    except Exception as exc:  # noqa: BLE001
        logger.warning("规划失败，退化单步计划: {}", exc)
        steps, notes, reasoning = _fallback_plan(intent), [], f"规划失败退化为单步：{exc}"

    logger.info(
        "计划 {} 步：{} 并行组={}",
        len(steps),
        [s.get("tool") for s in steps],
        sorted({str(s["parallel_group"]) for s in steps if s.get("parallel_group")}),
    )
    return {
        "plan": steps,
        "plan_round": state.get("plan_round", 0),
        "current_step": 0,
        "trace": [{"node": "planner", "steps": steps, "reasoning": reasoning, "dag_notes": notes}],
    }


def route_after_planner(state: AgentState) -> str:
    """计划为空就直接收尾，别白跑 executor。"""
    return "executor" if state.get("plan") else "synthesizer"


__all__ = ["PlanResult", "normalize_dag", "planner_node", "route_after_planner"]
