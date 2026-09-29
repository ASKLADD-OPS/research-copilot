"""引文图谱：取图、做图分析、重建引文边。"""

from __future__ import annotations

from typing import Any

import networkx as nx
from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas import ApiResponse, GraphAnalysisRequest, GraphAnalysisResult, GraphEdge, GraphNode, GraphOut

router = APIRouter(prefix="/graph", tags=["图谱"])

MAX_NODES = 300  # 前端 ECharts 超过这个量就该换布局/聚合了，先截断保可用


def _to_schema(graph: nx.DiGraph, *, max_nodes: int = MAX_NODES) -> GraphOut:
    truncated = graph.number_of_nodes() > max_nodes
    if truncated:
        # 按度数取核心子图：孤立的低连接节点先舍
        keep = sorted(graph.nodes, key=lambda n: graph.degree(n), reverse=True)[:max_nodes]
        graph = graph.subgraph(keep).copy()

    nodes = [
        GraphNode(
            id=str(n),
            title=str(graph.nodes[n].get("title", "") or n),
            year=graph.nodes[n].get("year"),
            in_degree=graph.in_degree(n),
            out_degree=graph.out_degree(n),
            community=graph.nodes[n].get("community"),
            pagerank=graph.nodes[n].get("pagerank"),
        )
        for n in graph.nodes
    ]
    edges = [GraphEdge(source=str(u), target=str(v)) for u, v in graph.edges]
    return GraphOut(nodes=nodes, edges=edges, n_nodes=len(nodes), n_edges=len(edges), truncated=truncated)


@router.get("", response_model=ApiResponse[GraphOut], summary="获取引文图")
async def get_graph(
    paper_ids: list[str] | None = Query(default=None, description="限定子图范围"),
) -> ApiResponse[GraphOut]:
    from app.graph_analysis import CitationGraphAnalyzer

    graph = await CitationGraphAnalyzer().build(paper_ids)
    return ApiResponse.ok(_to_schema(graph))


@router.post("/analyze", response_model=ApiResponse[GraphAnalysisResult], summary="图谱分析")
async def analyze(payload: GraphAnalysisRequest) -> ApiResponse[GraphAnalysisResult]:
    from app.graph_analysis import CitationGraphAnalyzer

    analyzer = CitationGraphAnalyzer()
    if payload.analysis == "paths":
        if not payload.source or not payload.target:
            return ApiResponse.fail(1001, "paths 分析需要同时给出 source 与 target")
        graph = await analyzer.build(payload.paper_ids or None)
        result: Any
        try:
            path = nx.shortest_path(graph, payload.source, payload.target)
            result = {"path": path, "length": len(path) - 1}
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            result = {"path": [], "error": f"无路径: {exc}"}
        return ApiResponse.ok(
            GraphAnalysisResult(
                analysis="paths",
                n_nodes=graph.number_of_nodes(),
                n_edges=graph.number_of_edges(),
                result=result,
            )
        )

    raw = await analyzer.run(payload.analysis, payload.paper_ids or None)
    return ApiResponse.ok(GraphAnalysisResult.model_validate(raw))


@router.post("/rebuild", response_model=ApiResponse[dict[str, Any]], summary="重建引文边")
async def rebuild(
    session: SessionDep,
    paper_ids: list[str] | None = Query(default=None),
) -> ApiResponse[dict[str, Any]]:
    from app.indexing.runner import spawn_rebuild_edges
    from app.models import Task

    task = Task(kind="rebuild_edges", status="pending", payload={"paper_ids": paper_ids or []})
    session.add(task)
    await session.commit()
    await session.refresh(task)

    spawn_rebuild_edges(task.id, paper_ids)
    return ApiResponse.ok({"task_id": task.id})
