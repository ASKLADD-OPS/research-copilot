"""引文图谱：取图、做图分析、重建引文边、快照。

`paper_ids` 一律是 int64（与 `papers.id` 对齐）。重建引文边不再建任务行 ——
NetworkX 全图计算在万篇量级是秒级操作，为它维护一张任务表不划算。
真要异步化的信号是"重建耗时超过前端超时"，那时再引入队列。
"""

from __future__ import annotations

from typing import Annotated, Any

import networkx as nx
from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.bootstrap import ensure_default_user
from app.models import GraphSnapshot
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
    paper_ids: Annotated[list[int] | None, Query(description="限定子图范围")] = None,
) -> ApiResponse[GraphOut]:
    from app.graph_analysis import CitationGraphAnalyzer

    graph = await CitationGraphAnalyzer().build(paper_ids)
    return ApiResponse.ok(_to_schema(graph))


@router.post("/analyze", response_model=ApiResponse[GraphAnalysisResult], summary="图谱分析")
async def analyze(payload: GraphAnalysisRequest, session: SessionDep) -> ApiResponse[GraphAnalysisResult]:
    from app.graph_analysis import CitationGraphAnalyzer

    analyzer = CitationGraphAnalyzer()
    paper_ids = [int(p) for p in (payload.paper_ids or [])]

    if payload.analysis == "paths":
        if not payload.source or not payload.target:
            return ApiResponse.fail(1001, "paths 分析需要同时给出 source 与 target")
        graph = await analyzer.build(paper_ids or None)
        result: Any
        try:
            path = nx.shortest_path(graph, str(payload.source), str(payload.target))
            result = {"path": path, "length": len(path) - 1}
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            result = {"path": [], "error": f"无路径: {exc}"}
        return ApiResponse.ok(
            GraphAnalysisResult(
                analysis="paths", n_nodes=graph.number_of_nodes(), n_edges=graph.number_of_edges(), result=result
            )
        )

    raw = await analyzer.run(payload.analysis, paper_ids or None)
    graph_result = GraphAnalysisResult.model_validate(raw)

    # 顺手存一份快照：图分析是秒级计算，"昨天那份综述里引的那张社区图"必须能原样复现。
    # 存快照失败不该让分析结果作废 —— 所以吞掉异常只记 warning。
    try:
        await _save_snapshot(session, analyzer, graph_result, paper_ids)
    except Exception:  # noqa: BLE001
        from loguru import logger

        logger.warning("图谱快照保存失败（分析结果已返回）", exc_info=True)

    return ApiResponse.ok(graph_result)


async def _save_snapshot(session: Any, analyzer: Any, result: GraphAnalysisResult, paper_ids: list[int]) -> None:
    user_id = await ensure_default_user(session)
    graph_data = await analyzer.snapshot_data(paper_ids or None)
    insights = [
        {
            "kind": result.analysis,
            "n_nodes": result.n_nodes,
            "n_edges": result.n_edges,
            "payload": result.result,
        }
    ]
    session.add(GraphSnapshot(user_id=user_id, paper_ids=paper_ids, graph_data=graph_data, insights=insights))
    await session.commit()


@router.get("/snapshots", response_model=ApiResponse[list[dict[str, Any]]], summary="图谱快照列表")
async def list_snapshots(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[list[dict[str, Any]]]:
    rows = (
        (await session.execute(select(GraphSnapshot).order_by(GraphSnapshot.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        [
            {
                "id": r.id,
                "paper_ids": list(r.paper_ids or []),
                "n_nodes": len((r.graph_data or {}).get("nodes") or []),
                "n_edges": len((r.graph_data or {}).get("edges") or []),
                "insights": list(r.insights or []),
                "created_at": r.created_at,
            }
            for r in rows
        ]
    )


@router.get("/snapshots/{snapshot_id}", response_model=ApiResponse[dict[str, Any]], summary="图谱快照详情")
async def get_snapshot(snapshot_id: int, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    row = await session.get(GraphSnapshot, snapshot_id)
    if row is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"快照不存在: {snapshot_id}")
    return ApiResponse.ok(
        {
            "id": row.id,
            "paper_ids": list(row.paper_ids or []),
            "graph_data": row.graph_data or {},
            "insights": list(row.insights or []),
            "created_at": row.created_at,
        }
    )


@router.post("/rebuild", response_model=ApiResponse[dict[str, Any]], summary="重建引文边")
async def rebuild(
    paper_ids: Annotated[list[int] | None, Query(description="限定范围")] = None,
) -> ApiResponse[dict[str, Any]]:
    from app.indexing.runner import spawn_rebuild_edges

    spawn_rebuild_edges(paper_ids)
    return ApiResponse.ok({"started": True, "paper_ids": paper_ids or []}, message="引文边重建已在后台启动")
