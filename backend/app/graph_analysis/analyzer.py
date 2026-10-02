"""引文网络分析 —— 对外的分析门面。

数据来源是 Postgres 里的 `citations` 边表（由解析阶段写入）+ 可选的
Semantic Scholar 补边。**不重复造图数据库**：Milvus 存向量，Postgres 存边，
NetworkX 在内存里算 —— 全库论文量级（万篇）下这样最省事，上亿才需要 Neo4j。

分工：
- 取数 → `builder.CitationGraphBuilder`
- 图论 → `analysis`（纯函数，可逐条断言）
- 让模型写综述/未来方向 → `survey` / `future_ideas`

本模块只做一件 `analysis` 名字 → 结果字典 的派发，以及给 Agent 的
`graph_analyze` 工具提供单一入口。
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from app.graph_analysis import analysis as A
from app.graph_analysis.builder import CitationGraphBuilder

ANALYSIS_KINDS = (
    "overview",
    "pagerank",
    "communities",
    "centrality",
    "keystones",
    "timeline",
    "evolution",
    "path",
)


class CitationGraphAnalyzer:
    def __init__(self, builder: CitationGraphBuilder | None = None) -> None:
        self.builder = builder or CitationGraphBuilder()

    async def build(self, paper_ids: list[int] | None = None, **kwargs: Any) -> nx.DiGraph:
        """构建有向图（edge = (source → target)，即"谁引了谁"）。"""
        return await self.builder.build(paper_ids, **kwargs)

    async def run(self, analysis: str = "overview", paper_ids: list[int] | None = None) -> dict[str, Any]:
        graph = await self.build(paper_ids)
        n_nodes, n_edges = graph.number_of_nodes(), graph.number_of_edges()

        if n_nodes == 0:
            return {
                "analysis": analysis,
                "result": None,
                "n_nodes": 0,
                "n_edges": 0,
                "note": "库中没有论文，无法做图分析。请先上传 PDF 或检索入库。",
            }

        result: Any
        if analysis == "pagerank":
            scores = A.pagerank_scores(graph)
            top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:20]
            result = [{"paper_id": pid, "title": _title(graph, pid), "score": round(s, 6)} for pid, s in top]
        elif analysis == "communities":
            assignment = A.detect_communities(graph)
            result = A.community_payload(graph, assignment)
        elif analysis == "centrality":
            bundle = A.centrality_bundle(graph)
            bundle.pop("scores", None)  # 逐节点分数是后端内部用的，别塞进响应
            result = bundle
        elif analysis == "keystones":
            result = A.identify_keystones(graph)
        elif analysis == "timeline":
            result = A.timeline(graph)
        elif analysis == "evolution":
            result = A.evolution_mainline(graph)
        elif analysis == "path" and paper_ids and len(paper_ids) >= 2:
            src, dst = str(paper_ids[0]), str(paper_ids[1])
            try:
                path = nx.shortest_path(graph, src, dst)
                result = {"source": src, "target": dst, "path": path, "length": len(path) - 1}
            except nx.NetworkXNoPath:
                result = {"source": src, "target": dst, "path": [], "note": "两篇之间不存在引用路径"}
        else:
            result = {
                "n_nodes": n_nodes,
                "n_edges": n_edges,
                "density": round(nx.density(graph), 6),
                "avg_degree": round(2 * n_edges / n_nodes, 3),
                "n_communities": len(set(A.detect_communities(graph).values())),
                "top_cited": self._top_cited(graph, limit=10),
            }

        return {"analysis": analysis, "result": result, "n_nodes": n_nodes, "n_edges": n_edges}

    @staticmethod
    def _top_cited(graph: nx.DiGraph, limit: int = 10) -> list[dict[str, Any]]:
        ranked = sorted(graph.in_degree(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [
            {"paper_id": str(pid), "title": _title(graph, str(pid)), "in_degree": deg} for pid, deg in ranked if deg > 0
        ]

    async def snapshot_data(self, paper_ids: list[int] | None = None, *, max_nodes: int = 300) -> dict[str, Any]:
        """把图导出成可落 `graph_snapshots.graph_data` 的纯 JSON 结构。"""
        graph = await self.build(paper_ids)
        return {
            "nodes": A.nodes_payload(graph, max_nodes=max_nodes),
            "edges": [
                {"source": str(u), "target": str(v), "context_snippet": graph[u][v].get("context_snippet") or ""}
                for u, v in graph.edges
            ],
        }


def _title(graph: nx.DiGraph, node: str) -> str:
    return str(graph.nodes[node].get("title") or node)


__all__ = ["ANALYSIS_KINDS", "CitationGraphAnalyzer"]
