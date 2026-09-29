"""引文图谱相关模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AnalysisKind = Literal["overview", "pagerank", "communities", "centrality", "paths", "timeline"]


class GraphNode(BaseModel):
    id: str
    title: str = ""
    year: int | None = None
    in_degree: int = 0
    out_degree: int = 0
    community: int | None = None
    pagerank: float | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    weight: float = 1.0


class GraphOut(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    n_nodes: int = 0
    n_edges: int = 0
    truncated: bool = Field(default=False, description="节点过多时只返回核心子图")


class GraphAnalysisRequest(BaseModel):
    analysis: AnalysisKind = "overview"
    paper_ids: list[str] = Field(default_factory=list)
    source: str | None = Field(default=None, description="paths 分析用：起点论文 id")
    target: str | None = Field(default=None, description="paths 分析用：终点论文 id")


class GraphAnalysisResult(BaseModel):
    analysis: str
    n_nodes: int = 0
    n_edges: int = 0
    result: Any = None
    note: str = ""
