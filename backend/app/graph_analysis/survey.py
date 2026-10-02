"""领域综述 —— 把核心子图 + 社区划分交给模型，产出结构化综述。

为什么是"结构化"而不是一段自由文本：综述的三个部分（时间线 / 每个社区的方法 /
核心论文贡献）在前端是三块独立渲染的区域，而且要能被 `future_ideas.py`
当作**可过滤的结构**再消费（"这个方向是不是已经被某篇核心论文解决了"只能
拿贡献句去比，比不了整篇散文）。

**只允许引用图里存在的 paper_id**：模型很擅长顺着领域常识补出几篇"应该被引"
的经典工作 —— 那种引用在本系统里无法溯源，等于幻觉。`validate_citations`
在返回前把这类 id 全部剔除，宁可让时间线少一条，也不让综述里出现图上没有的论文。
"""

from __future__ import annotations

from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from app.core.logging import logger
from app.graph_analysis import analysis as A
from app.llm.client import LLMClient, Message, Role, get_llm
from app.llm.structured import complete_structured


class TimelineEntry(BaseModel):
    """时间线上的一个里程碑。"""

    year: int = Field(description="该里程碑的年份")
    milestone: str = Field(description="这一年领域发生了什么（一句话）")
    paper_ids: list[str] = Field(default_factory=list, description="支撑这一条的论文 id，必须取自给定清单")


class CommunitySummary(BaseModel):
    """一个主题社区（子领域）的方法总结。"""

    community: int = Field(description="社区序号，取自给定清单")
    label: str = Field(description="这个子领域的名称（4-12 字）")
    method: str = Field(description="这一支的方法路线总结（2-3 句）")
    paper_ids: list[str] = Field(default_factory=list, description="代表论文 id，必须取自该社区的清单")


class CoreContribution(BaseModel):
    paper_id: str = Field(description="论文 id，必须取自核心基石清单")
    contribution: str = Field(description="它的核心贡献（1-2 句，具体到机制/方法，不要「很重要」这类空话）")


class Survey(BaseModel):
    title: str = Field(description="综述标题")
    overview: str = Field(description="领域总览（3-5 句）")
    timeline: list[TimelineEntry] = Field(default_factory=list)
    communities: list[CommunitySummary] = Field(default_factory=list)
    core_papers: list[CoreContribution] = Field(default_factory=list)
    open_problems: list[str] = Field(default_factory=list, description="图中论文明确提出、且尚未解决的开放问题")


#: 综述是系统里**最大的结构化输出**（时间线 + N 个社区方法 + N 篇核心贡献 + 开放问题）。
#: 默认的 `LLM_MAX_TOKENS=4096` 不够：实测会被截断成半个 JSON，报
#: `Unterminated string starting at ...`，然后 `complete_structured` 拿着一段残缺
#: 文本去修复（修一次还是残缺），白烧两轮 token 才失败。给这一类输出单独放大上限。
SURVEY_MAX_TOKENS = 8192


def graph_digest(
    graph: nx.DiGraph,
    *,
    assignment: dict[str, int],
    keystones: list[dict[str, Any]] | None = None,
    max_papers: int = 60,
) -> str:
    """把图压缩成给模型看的紧凑清单（id + 标题 + 年份 + 度数）。

    只喂清单不喂邻接矩阵：模型从"谁被谁引"的边表里读不出结构，
    而被引数/度数已经把"谁重要"说清楚了；边列表是 token 黑洞。
    """
    page = A.pagerank_scores(graph)
    order = sorted(graph.nodes, key=lambda n: page.get(str(n), 0.0), reverse=True)[:max_papers]
    lines: list[str] = []
    for n in order:
        pid = str(n)
        attrs = graph.nodes[n]
        lines.append(
            f"- id={pid} | {attrs.get('title') or pid} | 年份={attrs.get('year') or '?'} | "
            f"社区={assignment.get(pid, '?')} | 被引={graph.in_degree(n)} | 引用={graph.out_degree(n)}"
        )
    if keystones:
        lines.append("")
        lines.append("核心基石（已由 PageRank + 社区内度数算出，请重点覆盖）：")
        for k in keystones:
            lines.append(f"- id={k['paper_id']} | {k['title']} | 入选理由：{k['reason']}")
    return "\n".join(lines)


def validate_citations(survey: Survey, valid_ids: set[str]) -> tuple[Survey, int]:
    """剔除图上不存在的 paper_id。返回 `(清洗后的综述, 被剔除的数量)`。

    空壳条目（剔除后没有任何论文支撑的时间线/社区）一并丢掉 ——
    一条"某年发生了某事"但指不出是哪篇论文的里程碑，在溯源面板里点不开，
    留着只会让人误以为系统在编。
    """
    dropped = 0

    def keep(ids: list[str]) -> list[str]:
        nonlocal dropped
        kept = [i for i in ids if str(i) in valid_ids]
        dropped += len(ids) - len(kept)
        return kept

    timeline = []
    for entry in survey.timeline:
        ids = keep(list(entry.paper_ids))
        if ids:
            timeline.append(entry.model_copy(update={"paper_ids": ids}))

    communities = []
    for comm in survey.communities:
        ids = keep(list(comm.paper_ids))
        if ids:
            communities.append(comm.model_copy(update={"paper_ids": ids}))

    core = []
    for item in survey.core_papers:
        if str(item.paper_id) in valid_ids:
            core.append(item)
        else:
            dropped += 1

    return survey.model_copy(update={"timeline": timeline, "communities": communities, "core_papers": core}), dropped


def _prompt(graph: nx.DiGraph, assignment: dict[str, int], keystones: list[dict[str, Any]], digest: str) -> str:
    return (
        "你是学术综述专家。下面是某个研究方向的引文网络（节点=论文，社区=主题聚类，"
        "度数来自引用关系）。请基于它写一份**结构化领域综述**。\n\n"
        "硬约束（违反即视为无效输出）：\n"
        "1. 每一个 paper_id 都必须**逐字复制**上面清单里的 id，不许自造、不许改写、"
        "不许引用清单外的论文 —— 即使你确信某篇经典工作应该出现。\n"
        "2. 时间线要体现**方法演进**（谁解决了上一阶段遗留的什么问题），"
        "不要写成「某年发表了几篇」的流水账。\n"
        "3. 每个社区给一个方法路线总结，并点名该社区的代表论文。\n"
        "4. 核心论文的贡献要具体到机制（做了什么、为什么有效），不要空话。\n"
        "5. open_problems 只写图中论文自己明确提出、且**看起来还没被解决**的问题；"
        "没有就给空数组，不要为了凑数编。\n\n"
        f"【图规模】{graph.number_of_nodes()} 篇论文 · {graph.number_of_edges()} 条引用边 · "
        f"{len(set(assignment.values()))} 个社区\n\n【论文清单】\n{digest}"
    )


async def build_survey(
    graph: nx.DiGraph,
    *,
    assignment: dict[str, int] | None = None,
    keystones: list[dict[str, Any]] | None = None,
    llm: LLMClient | None = None,
    max_papers: int = 60,
    temperature: float = 0.3,
) -> Survey:
    """核心子图 + 社区划分 → 结构化综述。

    `assignment` 与 `keystones` 由调用方传入（`analysis.py` 已经算过一遍，
    在这里重算一遍既慢又可能因为随机种子之外的原因不一致）。
    """
    if graph.number_of_nodes() == 0:
        return Survey(title="（无数据）", overview="论文库为空，无法生成综述。")

    assignment = assignment if assignment is not None else A.detect_communities(graph)
    keystones = keystones if keystones is not None else A.identify_keystones(graph)
    digest = graph_digest(graph, assignment=assignment, keystones=keystones, max_papers=max_papers)

    messages: list[Message] = [{"role": "user", "content": _prompt(graph, assignment, keystones, digest)}]
    survey = await complete_structured(
        Survey,
        messages,
        role=Role.PLANNER,
        llm=llm or get_llm(),
        temperature=temperature,
        max_tokens=SURVEY_MAX_TOKENS,
    )

    cleaned, dropped = validate_citations(survey, {str(n) for n in graph.nodes})
    if dropped:
        logger.warning("综述引用了图上不存在的论文，已剔除 {} 处", dropped)
    return cleaned


__all__ = [
    "CommunitySummary",
    "CoreContribution",
    "Survey",
    "TimelineEntry",
    "build_survey",
    "graph_digest",
    "validate_citations",
]
