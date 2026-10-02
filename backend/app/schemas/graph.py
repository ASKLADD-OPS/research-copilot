"""引文图谱相关模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AnalysisKind = Literal[
    "overview",
    "pagerank",
    "communities",
    "centrality",
    "keystones",
    "timeline",
    "evolution",
    "paths",
]


class GraphNode(BaseModel):
    id: str
    title: str = ""
    year: int | None = None
    venue: str = ""
    citation_count: int | None = None
    abstract: str = ""
    in_degree: int = 0
    out_degree: int = 0
    community: int | None = None
    pagerank: float | None = None
    degree: int = 0


class GraphEdge(BaseModel):
    source: str
    target: str
    weight: float = 1.0
    context_snippet: str = ""


class GraphOut(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    n_nodes: int = 0
    n_edges: int = 0
    truncated: bool = Field(default=False, description="节点过多时只返回核心子图")


class GraphAnalysisRequest(BaseModel):
    analysis: AnalysisKind = "overview"
    paper_ids: list[int] = Field(default_factory=list, description="限定子图范围；空=全库")
    source: str | None = Field(default=None, description="paths 分析用：起点论文 id")
    target: str | None = Field(default=None, description="paths 分析用：终点论文 id")


class GraphAnalysisResult(BaseModel):
    analysis: str
    n_nodes: int = 0
    n_edges: int = 0
    result: Any = None
    note: str = ""


# ---------------------------------------------------------------- 阶段 10：构建与分析
class GraphBuildRequest(BaseModel):
    """`POST /graph/build` 的入参。"""

    paper_ids: list[int] = Field(default_factory=list, description="要建图的论文；空 = 取库里最新的一批（见 limit）")
    limit: int = Field(default=20, ge=1, le=200, description="paper_ids 为空时，取最新多少篇")
    enrich: bool = Field(default=False, description="是否用 Semantic Scholar 补边（需要外网与配额，慢）")
    max_nodes: int = Field(default=300, ge=10, le=2000)


class GraphBuildResult(BaseModel):
    """建图 + 图论分析的一次性产出（同时已落成快照）。"""

    snapshot_id: int
    graph: GraphOut
    keystones: list[dict[str, Any]] = Field(
        default_factory=list, description="核心基石（PageRank top-k ∪ 社区内度数 top-1）"
    )
    communities: list[dict[str, Any]] = Field(default_factory=list)
    mainline: list[dict[str, Any]] = Field(default_factory=list, description="领域演化主线（时间序 DAG 最长路径）")
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    centrality: dict[str, Any] = Field(default_factory=dict, description="三种中心性的 top 榜单")
    enrich_stats: dict[str, int] = Field(default_factory=dict, description="Semantic Scholar 补边计数")
    note: str = ""


class GraphInsightRequest(BaseModel):
    """`POST /graph/insights` 的入参。"""

    snapshot_id: int | None = Field(default=None, description="基于哪份快照的论文集合；与 paper_ids 二选一")
    paper_ids: list[int] = Field(default_factory=list, description="不在快照里时直接指定范围")
    resolved_ideas: list[str] = Field(
        default_factory=list, description="已知被解决/已做过的旧方向，未来方向会被它们过滤"
    )
    with_future: bool = Field(default=True, description="是否顺带产出未来方向（多一次 LLM 调用）")


class GraphInsightResult(BaseModel):
    snapshot_id: int | None = None
    n_nodes: int = 0
    n_edges: int = 0
    keystones: list[dict[str, Any]] = Field(default_factory=list)
    survey: dict[str, Any] = Field(
        default_factory=dict, description="结构化综述：时间线 / 社区方法 / 核心贡献 / 开放问题"
    )
    future_ideas: list[dict[str, Any]] = Field(default_factory=list)
    dropped_ideas: list[dict[str, str]] = Field(default_factory=list, description="被过滤掉的方向及原因")
    note: str = ""
