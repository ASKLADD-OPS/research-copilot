"""图论分析 —— 全部是 `nx.DiGraph → dict` 的纯函数。

刻意不碰数据库、不碰 LLM：引文图的分析结论要能被逐条钉死在测试里
（"这三篇是核心基石"是一个可复现的断言，不是一次模型抽样的产物）。
取数在 `builder.py`，让模型写综述在 `survey.py`。

指标选型
--------
| 指标 | 回答的问题 | 为什么需要它 |
|---|---|---|
| degree_centrality | 连接度 | 谁和最多工作直接相关（入度+出度，综述类节点靠出度上榜） |
| betweenness_centrality | 桥接作用 | 谁是把两个子领域接起来的那座桥 |
| pagerank | 综合影响力 | 被高影响力工作引用，权重比"被引次数"更接近真实地位 |
| louvain | 主题聚类 | 图上有几个子领域，各自是谁 |

社区检测用 **Louvain**（`networkx.community.louvain_communities`），不是 Leiden：
Leiden 要额外装 `igraph` + `leidenalg` 两个 C 扩展依赖，而本项目要的只是
"把 20~300 个节点分成几团" —— Louvain 在这个规模下的划分质量与 Leiden 无实质差别。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import networkx as nx

# 超过这个节点数就用带采样的近似中介中心性：精确算法是 O(n·m)，
# 300 节点 ~ 万条边时单次就要秒级，而引文图里这个指标的用途是"找桥"，
# 采样 200 个源点足够把桥排进前列。
BETWEENNESS_SAMPLE_THRESHOLD = 80
BETWEENNESS_SAMPLES = 200
SEED = 42  # 采样与 Louvain 都固定种子：同一张图必须给出同一个社区划分


# ------------------------------------------------------------------ 中心性
def degree_centrality(graph: nx.DiGraph) -> dict[str, float]:
    """连接度 = (入度 + 出度) / (n-1)。"""
    return {str(n): float(v) for n, v in nx.degree_centrality(graph).items()}


def betweenness_centrality(graph: nx.DiGraph) -> dict[str, float]:
    """中介中心性 = 有多少最短路经过它。

    大图上自动降级为带种子的采样近似（见 `BETWEENNESS_SAMPLE_THRESHOLD`），
    所以结果在同一张图上可复现 —— 不会出现"刷新一次桥换了一条"。
    """
    n = graph.number_of_nodes()
    if n <= 2:
        return {str(node): 0.0 for node in graph.nodes}
    k = None if n <= BETWEENNESS_SAMPLE_THRESHOLD else min(BETWEENNESS_SAMPLES, n)
    raw = nx.betweenness_centrality(graph, k=k, seed=SEED, normalized=True)
    return {str(n): float(v) for n, v in raw.items()}


def pagerank_scores(graph: nx.DiGraph, *, max_iter: int = 200) -> dict[str, float]:
    """PageRank。空边图退化成均匀分布 —— `nx.pagerank` 在无边时会直接抛。"""
    if graph.number_of_nodes() == 0:
        return {}
    if graph.number_of_edges() == 0:
        return {str(n): 1.0 / graph.number_of_nodes() for n in graph.nodes}
    return {str(n): float(v) for n, v in nx.pagerank(graph, max_iter=max_iter).items()}


def centrality_bundle(graph: nx.DiGraph, *, limit: int = 15) -> dict[str, Any]:
    """三种中心性一次算完，并各给一份 top-N 榜单（前端直接渲染）。"""
    degree = degree_centrality(graph)
    between = betweenness_centrality(graph)
    page = pagerank_scores(graph)

    def top(scores: dict[str, float]) -> list[dict[str, Any]]:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [
            {"paper_id": pid, "title": _title(graph, pid), "score": round(score, 6)}
            for pid, score in ranked
            if score > 0
        ]

    return {
        "degree_centrality": top(degree),
        "betweenness_centrality": top(between),
        "pagerank": top(page),
        "scores": {"degree": degree, "betweenness": between, "pagerank": page},
    }


# ------------------------------------------------------------------ 社区
def detect_communities(graph: nx.DiGraph) -> dict[str, int]:
    """Louvain 社区划分 → `{paper_id: 社区序号}`。

    在**无向化**之后的图上跑：引文方向对"是不是同一个子领域"没有意义，
    而且 Louvain 本身只定义在无向图上（networkx 对 DiGraph 会直接报错）。
    社区序号按规模从大到小重排，保证"社区 0 永远是最大的那团" ——
    否则同一张图换个跑法颜色就全变了，用户会以为数据变了。
    """
    nodes = [str(n) for n in graph.nodes]
    if not nodes:
        return {}
    if graph.number_of_edges() == 0:  # 无边：每个孤立点自成一社区，没有可划分的结构
        return dict.fromkeys(nodes, 0)

    communities = nx.community.louvain_communities(graph.to_undirected(), seed=SEED)
    ordered = sorted(communities, key=len, reverse=True)
    return {str(node): i for i, group in enumerate(ordered) for node in group}


def community_groups(assignment: dict[str, int]) -> dict[int, list[str]]:
    groups: dict[int, list[str]] = defaultdict(list)
    for pid, cid in assignment.items():
        groups[cid].append(pid)
    return dict(groups)


def community_payload(graph: nx.DiGraph, assignment: dict[str, int], *, top_n: int = 8) -> list[dict[str, Any]]:
    """社区列表（按规模降序），每个社区带内部度数最高的几篇当代表。"""
    out: list[dict[str, Any]] = []
    for cid, members in sorted(community_groups(assignment).items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]:
        ranked = sorted(members, key=lambda p: graph.degree(p), reverse=True)
        out.append(
            {
                "community": cid,
                "size": len(members),
                "papers": [
                    {
                        "paper_id": pid,
                        "title": _title(graph, pid),
                        "year": graph.nodes[pid].get("year"),
                        "degree": graph.degree(pid),
                    }
                    for pid in ranked[:10]
                ],
            }
        )
    return out


# ------------------------------------------------------------------ 核心基石
def identify_keystones(graph: nx.DiGraph, *, top_k: int = 3, per_community: int = 1) -> list[dict[str, Any]]:
    """核心基石 = PageRank top-k **并上** 每个社区内部度数 top-1。

    两个准则各自会漏：只看 PageRank 会漏掉小而紧凑的子领域里的奠基工作
    （社区规模小 → 全局排名进不了前 k）；只看社区内度数会漏掉跨社区的枢纽。
    取并集，并按 PageRank 降序排（并列时度数高的在前），保证结果稳定。
    """
    if graph.number_of_nodes() == 0:
        return []

    page = pagerank_scores(graph)
    assignment = detect_communities(graph)
    reason: dict[str, str] = {}

    for pid in sorted(page, key=lambda p: page[p], reverse=True)[:top_k]:
        reason[pid] = f"PageRank 前 {top_k}"
    for _, members in sorted(community_groups(assignment).items(), key=lambda kv: len(kv[1]), reverse=True):
        for pid in sorted(members, key=lambda p: graph.degree(p), reverse=True)[:per_community]:
            reason.setdefault(pid, f"社区 #{assignment[pid]} 内部度数第一")

    ranked = sorted(reason, key=lambda p: (page.get(p, 0.0), graph.degree(p)), reverse=True)
    return [
        {
            "paper_id": pid,
            "title": _title(graph, pid),
            "year": graph.nodes[pid].get("year"),
            "community": assignment.get(pid),
            "pagerank": round(page.get(pid, 0.0), 6),
            "degree": graph.degree(pid),
            "in_degree": graph.in_degree(pid),
            "reason": reason[pid],
        }
        for pid in ranked
    ]


def core_subgraph(graph: nx.DiGraph, seeds: list[str], *, hops: int = 1) -> nx.DiGraph:
    """以基石为中心、向外扩 `hops` 跳的核心子图 —— 综述的输入。

    取**无向邻域**：只沿引用方向扩会只看到"参考文献"那一侧，
    而综述要讲的是这些工作被谁继承 —— 那在出边方向。
    """
    if not seeds:
        return graph.copy()
    undirected = graph.to_undirected()
    keep: set[str] = set()
    for seed in seeds:
        if seed in undirected:
            keep |= nx.single_source_shortest_path_length(undirected, seed, cutoff=hops).keys()
    return graph.subgraph(keep).copy()


# ------------------------------------------------------------------ 时间与主线
def timeline(graph: nx.DiGraph, *, top_per_year: int = 8) -> list[dict[str, Any]]:
    """按年份分组的时间线（升序）。没有年份的节点不进 —— 不要造一个"未知年"桶
    混在时间线里，那会让"演进"看起来像在某个年份卡住了。"""
    buckets: dict[int, list[str]] = defaultdict(list)
    for node in graph.nodes:
        year = graph.nodes[node].get("year")
        if isinstance(year, int) and 1900 <= year <= 2100:
            buckets[year].append(str(node))

    page = pagerank_scores(graph)
    return [
        {
            "year": year,
            "count": len(pids),
            "papers": [
                {
                    "paper_id": pid,
                    "title": _title(graph, pid),
                    "pagerank": round(page.get(pid, 0.0), 6),
                }
                for pid in sorted(pids, key=lambda p: page.get(p, 0.0), reverse=True)[:top_per_year]
            ],
        }
        for year, pids in sorted(buckets.items())
    ]


def evolution_mainline(graph: nx.DiGraph, *, max_len: int = 40) -> list[dict[str, Any]]:
    """领域演化主线 = **时间序 DAG** 上的最长路径。

    为什么不直接在原图上找最长路径：引用图必然有环（互引、综述引遍全场），
    而 `nx.dag_longest_path` 遇到环会直接抛。这里先按年份把边剪成 DAG ——
    只保留"从更早的工作指向更晚的工作"的边，再取最长链。这既是算法上
    能算的，也正好是"演化主线"的语义：一条从旧到新、不断被继承的链条。

    年份未知的节点不参与（无法判断先后）；`max_len` 是输出护栏，
    领域里的主线通常十几跳，多出来的只是同一年内的平行并列。
    """
    temporal = nx.DiGraph()
    for node in graph.nodes:
        year = graph.nodes[node].get("year")
        if isinstance(year, int):
            temporal.add_node(str(node), year=year)

    for u, v in graph.edges:
        su, sv = str(u), str(v)
        if su in temporal and sv in temporal:
            yu, yv = temporal.nodes[su]["year"], temporal.nodes[sv]["year"]
            # 边是 citing → cited，所以"更早"的是 v；时间序方向上 v 在前。
            # **必须严格早于**：同年互引（`yv == yu`）会连成双向边，
            # 而 `dag_longest_path` 遇到环直接抛 —— 同年谁先谁后本来就无从判断，
            # 干脆把同年的边排除在主线之外。
            if yv < yu:
                temporal.add_edge(sv, su)

    if temporal.number_of_edges() == 0:
        return []

    chain: list[str] = nx.dag_longest_path(temporal)  # type: ignore[assignment]
    page = pagerank_scores(graph)
    return [
        {
            "paper_id": pid,
            "title": _title(graph, pid),
            "year": graph.nodes[pid].get("year") if pid in graph else temporal.nodes[pid].get("year"),
            "pagerank": round(page.get(pid, 0.0), 6),
        }
        for pid in chain[:max_len]
    ]


# ------------------------------------------------------------------ 节点载荷
def nodes_payload(
    graph: nx.DiGraph,
    *,
    assignment: dict[str, int] | None = None,
    pagerank: dict[str, float] | None = None,
    max_nodes: int = 300,
) -> list[dict[str, Any]]:
    """节点列表（JSON 可序列化）。超量时按 PageRank 取核心子图。"""
    assignment = assignment if assignment is not None else detect_communities(graph)
    page = pagerank if pagerank is not None else pagerank_scores(graph)
    nodes = sorted(graph.nodes, key=lambda n: page.get(str(n), 0.0), reverse=True)[:max_nodes]
    return [
        {
            "id": str(n),
            "title": _title(graph, str(n)),
            "year": graph.nodes[n].get("year"),
            "venue": graph.nodes[n].get("venue") or "",
            "citation_count": graph.nodes[n].get("citation_count"),
            "abstract": graph.nodes[n].get("abstract") or "",
            "in_degree": graph.in_degree(n),
            "out_degree": graph.out_degree(n),
            "degree": graph.degree(n),
            "community": assignment.get(str(n)),
            "pagerank": round(page.get(str(n), 0.0), 6),
        }
        for n in nodes
    ]


def _title(graph: nx.DiGraph, node: str) -> str:
    try:
        return str(graph.nodes[node].get("title") or node)
    except KeyError:  # 时间序子图里的节点可能不在原图上（不会发生，兜底而已）
        return node


__all__ = [
    "BETWEENNESS_SAMPLE_THRESHOLD",
    "betweenness_centrality",
    "centrality_bundle",
    "community_groups",
    "community_payload",
    "core_subgraph",
    "degree_centrality",
    "detect_communities",
    "evolution_mainline",
    "identify_keystones",
    "nodes_payload",
    "pagerank_scores",
    "timeline",
]
