"""引文网络分析 —— NetworkX。

数据来源是 Postgres 里的 `paper_citations` 边表（由解析阶段写入）。
**不重复造图数据库**：Milvus 存向量，Postgres 存边，NetworkX 在内存里算 ——
全库论文量级（万篇）下这样最省事，上亿才需要 Neo4j。
"""

from __future__ import annotations

from typing import Any

import networkx as nx
from loguru import logger
from sqlalchemy import select

from app.db.session import session_scope
from app.models.paper import Paper
from app.models.paper_citation import PaperCitation


class CitationGraphAnalyzer:
    async def build(self, paper_ids: list[str] | None = None) -> nx.DiGraph:
        """从数据库构建有向图：edge = (citing → cited)。"""
        graph = nx.DiGraph()
        async with session_scope() as session:
            stmt = select(PaperCitation.citing_paper_id, PaperCitation.cited_paper_id)
            if paper_ids:
                stmt = stmt.where(
                    PaperCitation.citing_paper_id.in_(paper_ids) | PaperCitation.cited_paper_id.in_(paper_ids)
                )
            rows = (await session.execute(stmt)).all()
            meta = (await session.execute(select(Paper.id, Paper.title))).all()

        titles = {str(pid): (title or str(pid)) for pid, title in meta}
        for citing, cited in rows:
            graph.add_edge(str(citing), str(cited))

        # 让孤立节点也进图（无引用关系的论文同样是节点）
        for pid in titles:
            if pid not in graph:
                graph.add_node(pid)
        nx.set_node_attributes(graph, titles, "title")
        logger.info("引文图构建完成：{} 节点 {} 边", graph.number_of_nodes(), graph.number_of_edges())
        return graph

    async def run(self, analysis: str = "overview", paper_ids: list[str] | None = None) -> dict[str, Any]:
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
            src, dst = paper_ids[0], paper_ids[1]
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


__all__ = ["CitationGraphAnalyzer"]
