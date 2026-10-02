"""引文图谱 —— 图构建 / 图论分析 / 领域综述。

三个端点的分工：

| 端点 | 干什么 | 花多少钱 |
|---|---|---|
| `POST /graph/build` | 建图 + 图论分析 + 落快照 | 秒级；`enrich=true` 才打外网 |
| `GET /graph/snapshot/{id}` | 取回冻下来的那张图与它的分析 | 一次主键查询 |
| `POST /graph/insights` | 在图上跑 LLM：领域综述 + 未来方向 | 两次模型调用 |

图论部分（中心性 / 社区 / 基石 / 主线）**不进 LLM** —— 这些结论必须可复现，
同一个 `paper_ids` 两次请求必须给出同一份基石清单。

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
from app.models import GraphSnapshot, Paper
from app.schemas import (
    ApiResponse,
    GraphAnalysisRequest,
    GraphAnalysisResult,
    GraphBuildRequest,
    GraphBuildResult,
    GraphEdge,
    GraphInsightRequest,
    GraphInsightResult,
    GraphNode,
    GraphOut,
)

router = APIRouter(prefix="/graph", tags=["图谱"])

MAX_NODES = 300  # 前端 ECharts 超过这个量就该换布局/聚合了，先截断保可用


def _to_schema(graph: nx.DiGraph, *, max_nodes: int = MAX_NODES) -> GraphOut:
    """nx 图 → 前端载荷。**在这里一次性算好 community / pagerank**。

    之前这两项读的是节点属性 `community` / `pagerank`，而建图时从没写过它们 ——
    于是前端"按社区着色"永远只有一种颜色、"节点大小 = PageRank"永远一样大。
    着色与尺寸是这张图的主要信息量，必须在出图前算出来。
    """
    from app.graph_analysis import analysis as A

    truncated = graph.number_of_nodes() > max_nodes
    if truncated:
        # 按度数取核心子图：孤立的低连接节点先舍
        keep = sorted(graph.nodes, key=lambda n: graph.degree(n), reverse=True)[:max_nodes]
        graph = graph.subgraph(keep).copy()

    assignment = A.detect_communities(graph)
    page = A.pagerank_scores(graph)

    nodes = [
        GraphNode(
            id=str(n),
            title=str(graph.nodes[n].get("title", "") or n),
            year=graph.nodes[n].get("year"),
            venue=graph.nodes[n].get("venue") or "",
            citation_count=graph.nodes[n].get("citation_count"),
            abstract=graph.nodes[n].get("abstract") or "",
            in_degree=graph.in_degree(n),
            out_degree=graph.out_degree(n),
            degree=graph.degree(n),
            community=assignment.get(str(n)),
            pagerank=round(page.get(str(n), 0.0), 6),
        )
        for n in graph.nodes
    ]
    edges = [
        GraphEdge(source=str(u), target=str(v), context_snippet=graph[u][v].get("context_snippet") or "")
        for u, v in graph.edges
    ]
    return GraphOut(nodes=nodes, edges=edges, n_nodes=len(nodes), n_edges=len(edges), truncated=truncated)


async def _resolve_scope(session: Any, paper_ids: list[int] | None, limit: int) -> list[int]:
    """定下这次建图用哪批论文。空 = 库里最新的一批（不是全库）。

    默认取最新 N 篇而不是全库：全库建图在论文多了以后要几十秒，
    而"看看我最近收的这批文献之间什么关系"才是这个面板的主场景。
    """
    if paper_ids:
        return [int(p) for p in paper_ids]
    rows = (await session.execute(select(Paper.id).order_by(Paper.id.desc()).limit(max(1, min(limit, 200))))).all()
    return [int(r[0]) for r in rows]


async def _save_snapshot(
    session: Any, graph_data: dict[str, Any], insights: list[dict[str, Any]], paper_ids: list[int]
) -> int:
    user_id = await ensure_default_user(session)
    row = GraphSnapshot(user_id=user_id, paper_ids=paper_ids, graph_data=graph_data, insights=insights)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return int(row.id)


@router.get("", response_model=ApiResponse[GraphOut], summary="获取引文图", operation_id="get_citation_graph")
async def get_graph(
    paper_ids: Annotated[list[int] | None, Query(description="限定子图范围；空=全库")] = None,
) -> ApiResponse[GraphOut]:
    from app.graph_analysis import CitationGraphAnalyzer

    graph = await CitationGraphAnalyzer().build(paper_ids)
    return ApiResponse.ok(_to_schema(graph))


@router.post(
    "/analyze", response_model=ApiResponse[GraphAnalysisResult], summary="图谱分析", operation_id="analyze_graph"
)
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
        await _save_snapshot(
            session,
            await analyzer.snapshot_data(paper_ids or None),
            [{"kind": result.analysis, "n_nodes": result.n_nodes, "n_edges": result.n_edges, "payload": result.result}],
            paper_ids,
        )
    except Exception:  # noqa: BLE001
        from loguru import logger

        logger.warning("图谱快照保存失败（分析结果已返回）", exc_info=True)

    return ApiResponse.ok(graph_result)


@router.post("/build", response_model=ApiResponse[GraphBuildResult], summary="构建引文图谱")
async def build_graph(payload: GraphBuildRequest, session: SessionDep) -> ApiResponse[GraphBuildResult]:
    """建图 → 图论分析 → 落快照，一次给全。"""
    from app.graph_analysis import analysis as A
    from app.graph_analysis.builder import CitationGraphBuilder

    builder = CitationGraphBuilder()
    paper_ids = await _resolve_scope(session, payload.paper_ids, payload.limit)
    if not paper_ids:
        return ApiResponse.ok(
            GraphBuildResult(
                snapshot_id=0,
                graph=GraphOut(),
                note="库里还没有论文。先上传 PDF 或从检索结果入库，再来建图。",
            )
        )

    enrich_stats: dict[str, int] = {}
    if payload.enrich:
        try:
            enrich_stats = await builder.enrich(paper_ids)
        except Exception:  # noqa: BLE001 - 补边失败不阻断建图
            from loguru import logger

            logger.warning("Semantic Scholar 补边失败，改用本地数据", exc_info=True)

    graph = await builder.build(paper_ids)
    assignment = A.detect_communities(graph)
    keystones = A.identify_keystones(graph)
    mainline = A.evolution_mainline(graph)
    timeline = A.timeline(graph)
    centrality = A.centrality_bundle(graph)
    centrality.pop("scores", None)
    schema_graph = _to_schema(graph, max_nodes=payload.max_nodes)

    insights: list[dict[str, Any]] = [
        {"kind": "keystones", "title": "核心基石", "payload": keystones},
        {"kind": "communities", "title": "主题社区", "payload": A.community_payload(graph, assignment)},
        {"kind": "evolution", "title": "领域演化主线", "payload": mainline},
        {"kind": "timeline", "title": "时间线", "payload": timeline},
        {"kind": "centrality", "title": "中心性", "payload": centrality},
    ]
    snapshot_id = await _save_snapshot(session, schema_graph.model_dump(), insights, paper_ids)

    return ApiResponse.ok(
        GraphBuildResult(
            snapshot_id=snapshot_id,
            graph=schema_graph,
            keystones=keystones,
            communities=insights[1]["payload"],
            mainline=mainline,
            timeline=timeline,
            centrality=centrality,
            enrich_stats=enrich_stats,
        ),
        message=f"已建图：{schema_graph.n_nodes} 节点 / {schema_graph.n_edges} 边",
    )


@router.post("/insights", response_model=ApiResponse[GraphInsightResult], summary="领域综述与未来方向")
async def insights(payload: GraphInsightRequest, session: SessionDep) -> ApiResponse[GraphInsightResult]:
    """在核心子图上跑模型：结构化综述（时间线 / 社区方法 / 核心贡献）+ 未来方向。

    未来方向的过滤是**机械的**（见 `future_ideas.filter_ideas`）：
    已在核心论文贡献里出现过、或与 `resolved_ideas` 重合的方向会被丢掉，
    并在 `dropped_ideas` 里说明原因 —— 用户要能看懂"为什么这个方向没了"。
    """
    from app.graph_analysis import analysis as A
    from app.graph_analysis.builder import CitationGraphBuilder
    from app.graph_analysis.future_ideas import suggest_future_directions
    from app.graph_analysis.survey import build_survey

    paper_ids = [int(p) for p in (payload.paper_ids or [])]
    snapshot: GraphSnapshot | None = None
    if payload.snapshot_id is not None:
        snapshot = await session.get(GraphSnapshot, payload.snapshot_id)
        if snapshot is None:
            from app.core.errors import NotFoundError

            raise NotFoundError(f"快照不存在: {payload.snapshot_id}")
        if not paper_ids:
            paper_ids = [int(p) for p in (snapshot.paper_ids or [])]

    if not paper_ids:
        return ApiResponse.ok(GraphInsightResult(note="没有指定论文范围，也没给快照 id。"))

    graph = await CitationGraphBuilder().build(paper_ids)
    if graph.number_of_nodes() == 0:
        return ApiResponse.ok(GraphInsightResult(note="这批论文里没有可用的引用关系。"))

    assignment = A.detect_communities(graph)
    keystones = A.identify_keystones(graph)
    seeds = [k["paper_id"] for k in keystones]
    core = A.core_subgraph(graph, seeds, hops=1)  # 综述只看核心子图，全图会把模型淹掉

    survey = await build_survey(
        core, assignment={k: v for k, v in assignment.items() if k in core}, keystones=keystones
    )
    survey_dump = survey.model_dump()

    ideas: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    note = ""
    if payload.with_future:
        result = await suggest_future_directions(
            survey_dump,
            resolved_ideas=payload.resolved_ideas,
            valid_ids={str(n) for n in graph.nodes},
        )
        ideas, dropped, note = result["ideas"], result["dropped"], result["note"]

    # 综述回写进快照：下次 GET /graph/snapshot/{id} 能直接看到当时那份结论
    if snapshot is not None:
        snapshot.insights = [
            *(snapshot.insights or []),
            {"kind": "survey", "title": survey.title, "payload": survey_dump},
            {"kind": "future_ideas", "title": "未来方向", "payload": ideas, "dropped": dropped},
        ]
        await session.commit()

    return ApiResponse.ok(
        GraphInsightResult(
            snapshot_id=snapshot.id if snapshot is not None else None,
            n_nodes=core.number_of_nodes(),
            n_edges=core.number_of_edges(),
            keystones=keystones,
            survey=survey_dump,
            future_ideas=ideas,
            dropped_ideas=dropped,
            note=note,
        )
    )


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
@router.get("/snapshot/{snapshot_id}", response_model=ApiResponse[dict[str, Any]], summary="图谱快照详情（单数别名）")
async def get_snapshot(snapshot_id: int, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    """两个路径是同一份数据。

    单数 `/snapshot/{id}` 是补充的别名：早期调用方（以及外部脚本）按
    "一个快照"的直觉写的单数，而列表端点是复数 `/snapshots` —— 两个都留，
    免得为了路径美观在客户端做一次字符串替换。
    """
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


@router.post("/rebuild", response_model=ApiResponse[dict[str, Any]], summary="重建引文边", operation_id="rebuild_graph")
async def rebuild(
    paper_ids: Annotated[list[int] | None, Query(description="限定范围")] = None,
) -> ApiResponse[dict[str, Any]]:
    from app.indexing.runner import spawn_rebuild_edges

    spawn_rebuild_edges(paper_ids)
    return ApiResponse.ok({"started": True, "paper_ids": paper_ids or []}, message="引文边重建已在后台启动")
