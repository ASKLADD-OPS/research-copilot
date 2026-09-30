"""引文网络分析 —— NetworkX。

数据来源是 Postgres 里的 `citations` 边表（由解析阶段写入）。
**不重复造图数据库**：Milvus 存向量，Postgres 存边，NetworkX 在内存里算 ——
全库论文量级（万篇）下这样最省事，上亿才需要 Neo4j。

节点 id 统一转成 **str**：NetworkX 的节点键可以是任意可哈希对象，
但 JSON 序列化、ECharts 前端、以及 `nx.shortest_path` 的入参最终都要落到字符串上，
在入口处一次性归一，比在五个出口各转一次可靠。
"""

from __future__ import annotations

from typing import Any

import networkx as nx
from loguru import logger
from sqlalchemy import select

from app.db.session import session_scope
from app.models import Citation, Paper


class CitationGraphAnalyzer:
    async def build(self, paper_ids: list[int] | None = None) -> nx.DiGraph:
        """从数据库构建有向图：edge = (source → target)，即"谁引了谁"。"""
        graph = nx.DiGraph()
        async with session_scope() as session:
            stmt = select(Citation.source_paper_id, Citation.target_paper_id)
            if paper_ids:
                stmt = stmt.where(Citation.source_paper_id.in_(paper_ids) | Citation.target_paper_id.in_(paper_ids))
            rows = (await session.execute(stmt)).all()
            meta = (await session.execute(select(Paper.id, Paper.title))).all()

        titles = {str(pid): (title or str(pid)) for pid, title in meta}
        for source, target in rows:
            if target is None:  # 外部引用（被引论文不在库内）不进图，否则会造出无标题的幽灵节点
                continue
            graph.add_edge(str(source), str(target))

        # 让孤立节点也进图（无引用关系的论文同样是节点）
        for pid in titles:
            if pid not in graph:
                graph.add_node(pid)
        nx.set_node_attributes(graph, titles, "title")
        logger.info("引文图构建完成：{} 节点 {} 边", graph.number_of_nodes(), graph.number_of_edges())
        return graph

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
            scores = nx.pagerank(graph, max_iter=200) if n_edges else dict.fromkeys(graph, 1.0 / n_nodes)
            top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:20]
            result = [
                {"paper_id": pid, "title": graph.nodes[pid].get("title", pid), "score": round(s, 6)} for pid, s in top
            ]
        elif analysis == "communities":
            undirected = graph.to_undirected()
            communities = nx.community.louvain_communities(undirected, seed=42) if n_edges else [{n} for n in graph]
            result = [
                {
                    "size": len(c),
                    "papers": [{"paper_id": pid, "title": graph.nodes[pid].get("title", pid)} for pid in list(c)[:10]],
                }
                for c in sorted(communities, key=len, reverse=True)[:10]
            ]
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
                "top_cited": self._top_cited(graph, limit=10),
            }

        return {"analysis": analysis, "result": result, "n_nodes": n_nodes, "n_edges": n_edges}

    @staticmethod
    def _top_cited(graph: nx.DiGraph, limit: int = 10) -> list[dict[str, Any]]:
        ranked = sorted(graph.in_degree(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [
            {"paper_id": pid, "title": graph.nodes[pid].get("title", pid), "in_degree": deg}
            for pid, deg in ranked
            if deg > 0
        ]

    async def snapshot_data(self, paper_ids: list[int] | None = None, *, max_nodes: int = 300) -> dict[str, Any]:
        """把图导出成可落 `graph_snapshots.graph_data` 的纯 JSON 结构。

        超量时按度数取核心子图：孤立的低连接节点先舍，否则 ECharts 前端会卡死。
        """
        graph = await self.build(paper_ids)
        nodes = [
            {
                "id": str(n),
                "title": graph.nodes[n].get("title", str(n)),
                "in_degree": graph.in_degree(n),
                "out_degree": graph.out_degree(n),
            }
            for n in graph.nodes
        ]
        edges = [{"source": str(u), "target": str(v)} for u, v in graph.edges]
        return {"nodes": nodes[:max_nodes], "edges": edges}


__all__ = ["CitationGraphAnalyzer"]
