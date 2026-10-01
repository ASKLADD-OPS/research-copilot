"""AgentState —— LangGraph 图的全局状态定义。

设计要点：
- 用 `Annotated[list, operator.add]` 让并发分支写同一字段时自动归并，而不是互相覆盖。
- 所有字段都给默认值，节点可以只 `return` 自己改的那几项。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Intent = Literal[
    "single_paper_qa",
    "cross_paper_reasoning",
    "literature_search",
    "graph_analysis",
    "writing_assist",
    "visualization",
    "translation",
    "chitchat",
]

ReflectDim = Literal["faithfulness", "relevance", "coherence", "completeness"]


class RetrievedDoc(TypedDict, total=False):
    """检索到的一个片段。chunk_id 是溯源的唯一凭据。

    `bbox` 与 `page` 一起构成"跳回原文"的能力：page 让前端翻页，
    bbox 让 PDF.js 在页内精确框出命中段落。所以它必须一路传到溯源输出，
    不能只在检索层存在。
    """

    chunk_id: str
    paper_id: str
    title: str
    section: str
    page: int
    bbox: list[float] | None
    text: str
    score: float
    source: str  # dense | sparse | fused | rerank | web


class Citation(TypedDict, total=False):
    """一条溯源记录。字段与 `app.rag.source_tracing.SourceTrace` 对齐 ——
    存成 dict 是因为要过 LangGraph 的 checkpointer（需可 pickle）。"""

    chunk_id: str
    paper_id: str
    title: str
    section: str
    page: int
    quote: str
    verified: bool  # NLI 蕴含验证结果
    marker: int  # 答案正文里的编号 [n]
    answer_span: str  # 答案里引用它的那句话
    char_span: list[int] | None  # 该句在答案中的字符区间（list 而非 tuple，便于 JSON 往返）
    bbox: list[float] | None
    confidence: float
    attribution_method: str  # self_citation | nli | hybrid


class PlanStep(TypedDict, total=False):
    """计划中的一步。`dependencies` + `parallel_group` 让计划是 DAG 而非流水线。

    - `dependencies`：前置步骤的 `idx`。**只允许指向更小的 `idx`** —— 强制这一条
      就天然无环，不需要单独的环检测。
    - `parallel_group`：同一标签的步骤之间互不依赖，executor 会并发跑。
      空/缺省 = 只能串行。
    """

    idx: int
    goal: str
    tool: str
    dependencies: list[int]
    parallel_group: str
    status: Literal["pending", "running", "done", "failed", "replanned"]
    result: str


class Reflection(TypedDict, total=False):
    scores: dict[str, float]
    overall: float
    verdict: Literal["pass", "refine"]
    critique: str
    round: int


class AgentState(TypedDict, total=False):
    # ---- 输入 ----
    query: str
    session_id: str
    user_id: str
    history: Annotated[list[dict[str, Any]], operator.add]

    # ---- 意图识别 ----
    intent: Intent
    confidence: float
    target_papers: list[str]
    slot_filling: dict[str, Any]

    # ---- 计划与执行 ----
    plan: list[PlanStep]
    plan_round: int  # re-plan 轮次，上限 2
    current_step: int
    thoughts: Annotated[list[dict[str, Any]], operator.add]  # ReAct 轨迹

    # ---- 检索 ----
    retrieved: list[RetrievedDoc]
    crag_level: Literal["relevant", "ambiguous", "irrelevant"]
    rewrite_round: int  # Query Rewrite 轮次，上限 3
    rewritten_query: str
    reranked: list[RetrievedDoc]

    # ---- 生成与反思 ----
    draft: str
    citations: list[Citation]
    grounding_ratio: float
    reflection: Reflection
    refine_round: int  # refine 轮次，上限 2

    # ---- 出口 ----
    answer: str
    clarify_question: str
    guardrail_flags: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]  # Reflexion 可追踪日志

    # ---- 控制 ----
    error: str
    done: bool


def new_state(query: str, session_id: str = "default", user_id: str = "anon") -> AgentState:
    """构造初始状态。所有计数器归零，避免跨轮串味。"""
    return AgentState(
        query=query,
        session_id=session_id,
        user_id=user_id,
        history=[],
        target_papers=[],
        slot_filling={},
        plan=[],
        plan_round=0,
        current_step=0,
        thoughts=[],
        retrieved=[],
        rewrite_round=0,
        reranked=[],
        citations=[],
        grounding_ratio=0.0,
        refine_round=0,
        guardrail_flags=[],
        trace=[],
        done=False,
    )


__all__ = ["AgentState", "Citation", "Intent", "PlanStep", "Reflection", "RetrievedDoc", "new_state"]
