"""引文图谱构建与分析的验收测试（阶段 10）。

**不连库、不拉模型、不发外网。** 建图那一段用假 session 顶掉 `session_scope`
（只提供 `execute(...).all()` 这一个能力），模型那两次调用用 monkeypatch 顶掉
`complete_structured` —— 于是这里断言的是**我们自己的控制流与判定规则**：
节点属性怎么装配、基石怎么选、主线怎么剪、幻觉引用怎么剔、未来方向怎么过滤。

验收标准对应的用例：

| 标准 | 用例 |
|---|---|
| 1. 构建 20 篇论文的引用图 | `test_build_twenty_paper_graph` |
| 2. 中心性识别出 3 个核心基石 | `test_three_communities_yield_exactly_three_keystones` |
| 3. 综述含时间线与社区划分 | `test_survey_keeps_timeline_and_communities` |
| 4. 未来方向过滤准确 | `test_filter_drops_solved_and_duplicate_ideas` 等 |
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import networkx as nx
import pytest

from app.graph_analysis import analysis as A
from app.graph_analysis import future_ideas as F
from app.graph_analysis import survey as S
from app.graph_analysis.builder import CitationGraphBuilder, LibraryIndex, match_candidates, norm_title, s2_ref

COMMUNITY_SIZES = (7, 7, 6)  # 7 + 7 + 6 = 20 篇


# ================================================================== 造数据
def make_graph() -> nx.DiGraph:
    """20 篇论文 / 3 个主题社区 / 2016→2022 的引用网络。

    每个社区是一颗"星 + 链"：所有人引社区里最早的那篇（星，造出明确的枢纽），
    并且依次引前一篇（链，让演化主线有足够深度）。跨社区只连 2 条边，
    保证 Louvain 能稳定看出 3 团。
    """
    graph = nx.DiGraph()
    idx = 0
    for cid, size in enumerate(COMMUNITY_SIZES):
        ids = [f"p{idx + i}" for i in range(size)]
        idx += size
        hub = ids[0]
        for i, pid in enumerate(ids):
            graph.add_node(
                pid,
                title=f"社区{cid} 论文{i}",
                year=2016 + i,
                venue=("NeurIPS", "ICML", "ACL")[cid],
                citation_count=120 - i * 5,
                abstract=f"{pid} 的摘要",
            )
        for i, pid in enumerate(ids[1:], start=1):
            graph.add_edge(pid, hub, context_snippet=f"{pid} 借鉴了 {hub} 的方法")
            if i > 1:
                graph.add_edge(pid, ids[i - 1])
    graph.add_edge("p13", "p0", context_snippet="跨社区的桥")
    graph.add_edge("p19", "p0")
    return graph


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _FakeSession:
    """只实现 builder 用到的能力：按顺序吐出预置的结果集。"""

    def __init__(self, batches: list[list[tuple]]) -> None:
        self._batches = list(batches)
        self.added: list[object] = []

    async def execute(self, _stmt: object) -> _Rows:
        return _Rows(self._batches.pop(0) if self._batches else [])

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def get(self, _model: object, _pk: object) -> None:
        return None


def fake_session_scope(batches: list[list[tuple]]):
    @asynccontextmanager
    async def _scope():
        yield _FakeSession(batches)

    # 每次调用都新建一个：builder 一轮里会开多次 session
    return lambda: _scope()


# ================================================================== 验收 1
@pytest.mark.unit
async def test_build_twenty_paper_graph(monkeypatch):
    """20 篇论文 + 引用边 → DiGraph，节点带 title/year/venue/citation_count，边带 context_snippet。"""
    graph = make_graph()
    meta = [
        (
            int(n[1:]) + 1,
            graph.nodes[n]["title"],
            graph.nodes[n]["year"],
            graph.nodes[n]["venue"],
            graph.nodes[n]["citation_count"],
            graph.nodes[n]["abstract"],
        )
        for n in graph.nodes
    ]
    edges = [(int(u[1:]) + 1, int(v[1:]) + 1, graph[u][v].get("context_snippet")) for u, v in graph.edges]

    monkeypatch.setattr("app.graph_analysis.builder.session_scope", fake_session_scope([edges, meta]))
    built = await CitationGraphBuilder().build(list(range(1, 21)))

    assert built.number_of_nodes() == 20
    assert built.number_of_edges() == len(edges)

    sample = built.nodes["1"]  # p0 → paper_id 1
    assert sample["title"] == "社区0 论文0"
    assert sample["year"] == 2016
    assert sample["venue"] == "NeurIPS"
    assert sample["citation_count"] == 120
    assert built["7"]["1"]["context_snippet"] == "p6 借鉴了 p0 的方法"

    # 节点属性同时进图分析载荷（前端 hover 卡片用）
    payload = A.nodes_payload(built)
    assert len(payload) == 20
    assert {"id", "title", "year", "venue", "citation_count", "community", "pagerank"} <= set(payload[0])


@pytest.mark.unit
async def test_duplicate_edges_keep_the_first_snippet(monkeypatch):
    """同一对论文被引两次时保留第一条正文片段，不要被后一条空片段覆盖成 None。"""
    edges = [(3, 1, "第一次引用的上下文"), (3, 1, None)]
    meta = [(i, f"论文{i}", 2020, "ACL", 1, "") for i in (1, 3)]
    monkeypatch.setattr("app.graph_analysis.builder.session_scope", fake_session_scope([edges, meta]))

    built = await CitationGraphBuilder().build([1, 3])
    assert built["3"]["1"]["context_snippet"] == "第一次引用的上下文"


@pytest.mark.unit
def test_library_index_prefers_doi_over_title():
    index = LibraryIndex.from_rows([(1, "10.1/x", None), (2, None, "2401.00001")])
    index.add_title(1, "A Great Paper")
    index.add_title(2, "Another Paper")

    assert index.resolve(doi="10.1/X", title="随便") == 1  # DOI 大小写无关
    assert index.resolve(arxiv_id="2401.00001") == 2
    assert index.resolve(title="  ANOTHER   paper ") == 2  # 标题归一：压空白 + 小写
    assert index.resolve(title="库外论文") is None


@pytest.mark.unit
def test_match_candidates_drops_outside_and_self_citations():
    """库外论文与自引都不能变成边 —— 库里没有节点属性的论文会变成幽灵节点。"""
    index = LibraryIndex.from_rows([(1, "10.1/x", None)])
    index.add_title(1, "Inside")
    candidates = [
        {"title": "Inside", "doi": "10.1/x", "contexts": ["引用上下文"]},  # 命中本库
        {"title": "Outside", "doi": "10.9/z", "contexts": ["库外"]},  # 库外 → 丢
    ]
    # 1 号论文引自己 → 自环，丢掉（自环会让 PageRank 与最长路径都变怪）
    assert match_candidates(1, index, candidates) == []
    # 2 号论文引 1 号 → 命中本库，库外的那条被丢
    matched = match_candidates(2, index, candidates)
    assert [m[0] for m in matched] == [1]
    assert matched[0][1] == "引用上下文"


@pytest.mark.unit
def test_s2_ref_needs_a_doi_or_an_arxiv_id():
    assert s2_ref("10.1/x", None) == "DOI:10.1/x"
    assert s2_ref(None, "2401.00001") == "ARXIV:2401.00001"
    assert s2_ref(None, None) is None
    assert norm_title("  A  Big   Title  ") == "a big title"


# ================================================================== 中心性与基石
@pytest.mark.unit
def test_three_centralities_cover_every_node():
    graph = make_graph()
    bundle = A.centrality_bundle(graph)

    assert bundle["scores"].keys() == {"degree", "betweenness", "pagerank"}
    for scores_key, top_key in (
        ("degree", "degree_centrality"),
        ("betweenness", "betweenness_centrality"),
        ("pagerank", "pagerank"),
    ):
        assert bundle["scores"][scores_key].keys() == {str(n) for n in graph.nodes}
        assert bundle[top_key], f"{top_key} 的 top 榜单不该为空"

    # 度数与 PageRank 都该把三个社区的枢纽排在最前
    assert {row["paper_id"] for row in bundle["degree_centrality"][:3]} == {"p0", "p7", "p14"}
    assert {row["paper_id"] for row in bundle["pagerank"][:3]} == {"p0", "p7", "p14"}


@pytest.mark.unit
def test_three_communities_yield_exactly_three_keystones():
    """验收 2：PageRank top-3 与三个社区的度数第一重合 → 恰好 3 个核心基石。"""
    graph = make_graph()
    assignment = A.detect_communities(graph)

    assert len(set(assignment.values())) == 3, "Louvain 应认出 3 个主题社区"
    assert len(assignment) == 20

    keystones = A.identify_keystones(graph, top_k=3)
    assert len(keystones) == 3
    assert {k["paper_id"] for k in keystones} == {"p0", "p7", "p14"}
    assert all(k["reason"] for k in keystones)  # 每块基石都要说清"为什么是它"


@pytest.mark.unit
def test_keystones_include_small_community_champion_not_in_pagerank_top():
    """只看 PageRank 会漏掉小而紧凑的子领域 —— 社区内度数第一必须能补进来。"""
    graph = nx.DiGraph()
    # 一个 10 节点的强社区
    for i in range(10):
        graph.add_node(f"a{i}", year=2016 + i, title=f"a{i}")
    for i in range(1, 10):
        graph.add_edge(f"a{i}", "a0")
    # 一个 5 节点的弱社区，枢纽在全局 PageRank 排不进前 1
    for i in range(5):
        graph.add_node(f"b{i}", year=2016 + i, title=f"b{i}")
    for i in range(1, 5):
        graph.add_edge(f"b{i}", "b0")

    keystones = A.identify_keystones(graph, top_k=1)
    assert "a0" in {k["paper_id"] for k in keystones}
    assert "b0" in {k["paper_id"] for k in keystones}, "社区内部的度数第一必须被补进来"


@pytest.mark.unit
def test_empty_graph_analyses_do_not_raise():
    empty = nx.DiGraph()
    assert A.pagerank_scores(empty) == {}
    assert A.detect_communities(empty) == {}
    assert A.identify_keystones(empty) == []
    assert A.evolution_mainline(empty) == []
    assert A.timeline(empty) == []


# ================================================================== 时间线与演化主线
@pytest.mark.unit
def test_timeline_skips_unknown_years():
    graph = make_graph()
    graph.nodes["p3"]["year"] = None  # 年份没解析出来的论文
    buckets = A.timeline(graph)

    assert [b["year"] for b in buckets] == sorted(b["year"] for b in buckets)
    assert sum(b["count"] for b in buckets) == 19  # 年份未知的那篇不进时间线
    assert all("p3" not in {p["paper_id"] for p in b["papers"]} for b in buckets)


@pytest.mark.unit
def test_evolution_mainline_is_a_real_old_to_new_chain():
    """验收：主线 = 时间序 DAG 上的最长路径，方向上必须从旧到新。"""
    graph = make_graph()
    mainline = A.evolution_mainline(graph)

    assert len(mainline) >= 5, "20 篇的图里主线不该只有一两跳"
    years = [row["year"] for row in mainline]
    assert years == sorted(years), "主线必须按年份单调不减"
    for older, newer in zip(mainline, mainline[1:], strict=False):
        assert graph.has_edge(newer["paper_id"], older["paper_id"]), "相邻两跳之间必须真的存在引用边"


@pytest.mark.unit
def test_evolution_mainline_survives_mutual_citations():
    """同年互引会造出环，`dag_longest_path` 遇到环直接抛 —— 剪枝必须挡住这种情况。"""
    cycle = nx.DiGraph()
    for pid in ("x", "y"):
        cycle.add_node(pid, year=2020, title=pid)
    cycle.add_edge("x", "y")
    cycle.add_edge("y", "x")

    assert A.evolution_mainline(cycle) == []  # 同年的边全部排除 → 没有时间序 DAG

    mixed = nx.DiGraph()
    mixed.add_node("old", year=2015, title="old")
    mixed.add_node("a", year=2020, title="a")
    mixed.add_node("b", year=2020, title="b")
    mixed.add_edge("a", "old")
    mixed.add_edge("b", "old")
    mixed.add_edge("b", "a")
    mixed.add_edge("a", "b")  # 同年互引
    chain = [row["paper_id"] for row in A.evolution_mainline(mixed)]
    assert chain[0] == "old" and set(chain) <= {"old", "a", "b"}


@pytest.mark.unit
def test_core_subgraph_expands_around_seeds():
    graph = make_graph()
    core = A.core_subgraph(graph, ["p0"], hops=1)
    assert "p0" in core
    assert core.number_of_nodes() < graph.number_of_nodes()


@pytest.mark.unit
def test_nodes_payload_truncates_by_pagerank():
    graph = make_graph()
    payload = A.nodes_payload(graph, max_nodes=5)
    assert len(payload) == 5
    # 截断按 PageRank 取前 N，最大的枢纽必须留下
    assert payload[0]["id"] == "p0"


# ================================================================== 验收 3：综述
@pytest.mark.unit
async def test_survey_keeps_timeline_and_communities(monkeypatch):
    """验收 3：综述必须同时带时间线与社区划分，且幻觉引用被剔除。"""
    graph = make_graph()
    assignment = A.detect_communities(graph)
    captured: dict[str, str] = {}

    fake = S.Survey(
        title="检索增强生成的演进",
        overview="从稀疏检索走到混合检索。",
        timeline=[
            S.TimelineEntry(year=2016, milestone="提出基础检索框架", paper_ids=["p0", "p7"]),
            S.TimelineEntry(year=2020, milestone="引入重排", paper_ids=["p5"]),
            S.TimelineEntry(year=2021, milestone="编造的里程碑", paper_ids=["ghost-1"]),  # 图上没有 → 整条丢
        ],
        communities=[
            S.CommunitySummary(community=0, label="检索", method="混合检索 + RRF", paper_ids=["p0", "p1"]),
            S.CommunitySummary(community=1, label="生成", method="解码约束", paper_ids=["ghost-2"]),  # 丢
        ],
        core_papers=[
            S.CoreContribution(paper_id="p0", contribution="提出 RRF 融合"),
            S.CoreContribution(paper_id="ghost-3", contribution="编造"),
        ],
        open_problems=["长文档下的检索粒度仍未定论"],
    )

    async def fake_complete(schema, messages, **kwargs):  # noqa: ARG001
        captured["prompt"] = messages[0]["content"]
        return fake

    monkeypatch.setattr(S, "complete_structured", fake_complete)
    out = await S.build_survey(graph, assignment=assignment, keystones=A.identify_keystones(graph))

    assert [e.year for e in out.timeline] == [2016, 2020]  # 只有引用了图内论文的里程碑留下
    assert len(out.communities) == 1
    assert [c.paper_id for c in out.core_papers] == ["p0"]
    assert out.open_problems == ["长文档下的检索粒度仍未定论"]

    # 提示词里必须把"只许引用清单内的 id"写成硬约束，并真的带上论文清单
    assert "不许自造" in captured["prompt"]
    assert "id=p0 |" in captured["prompt"]


@pytest.mark.unit
def test_graph_digest_reports_size_and_keystones():
    graph = make_graph()
    digest = S.graph_digest(
        graph, assignment=A.detect_communities(graph), keystones=A.identify_keystones(graph), max_papers=5
    )
    assert digest.count("- id=") == 5 + 3  # 5 条论文 + 3 条核心基石
    assert "核心基石" in digest


@pytest.mark.unit
def test_validate_citations_counts_every_drop():
    survey = S.Survey(
        title="t",
        overview="o",
        timeline=[S.TimelineEntry(year=2020, milestone="m", paper_ids=["ok", "bad", "bad2"])],
        core_papers=[S.CoreContribution(paper_id="bad3", contribution="c")],
    )
    cleaned, dropped = S.validate_citations(survey, {"ok"})
    assert dropped == 3  # bad + bad2（时间线）+ bad3（核心贡献）
    assert cleaned.timeline[0].paper_ids == ["ok"]
    assert cleaned.core_papers == []


# ================================================================== 验收 4：未来方向过滤
@pytest.mark.unit
def test_filter_drops_solved_and_duplicate_ideas():
    """验收 4：已被核心论文解决 / 与已有方向重复的，必须被剔掉并说明原因。"""
    ideas = [
        F.FutureIdea(title="用对比学习改进检索表示", rationale="可以提升召回", based_on=["p1"]),
        F.FutureIdea(title="跨语言检索的表示对齐", rationale="中文语料仍缺", based_on=["p2"]),
        F.FutureIdea(title="面向长文档的层次化检索", rationale="粒度未定", based_on=["p3"]),
    ]
    kept, dropped = F.filter_ideas(
        ideas,
        solved=["本文提出用对比学习改进检索表示的方法，显著提升召回率"],
        resolved=["跨语言检索的表示对齐研究"],
        valid_ids={"p1", "p2", "p3"},
    )

    titles = [k.title for k in kept]
    assert titles == ["面向长文档的层次化检索"]
    assert len(dropped) == 2
    assert any("核心论文" in d["reason"] for d in dropped)
    assert any("已有方向" in d["reason"] for d in dropped)


@pytest.mark.unit
def test_filter_drops_ideas_without_traceable_papers():
    ideas = [F.FutureIdea(title="全新的方向", rationale="理由", based_on=["ghost"])]
    kept, dropped = F.filter_ideas(ideas, valid_ids={"p1"})
    assert kept == []
    assert dropped[0]["reason"].startswith("没有可溯源")


@pytest.mark.unit
def test_filter_marks_and_prioritises_open_problem_ideas():
    ideas = [
        F.FutureIdea(title="任意方向 A", rationale="无关理由", based_on=["p1"]),
        F.FutureIdea(title="长文档下的检索粒度仍未定论", rationale="开放问题原文", based_on=["p2"]),
    ]
    kept, _ = F.filter_ideas(ideas, open_problems=["长文档下的检索粒度仍未定论"], valid_ids={"p1", "p2"})
    assert kept[0].from_open_problems is True, "来自开放问题的方向必须排到最前"
    assert kept[1].from_open_problems is False


@pytest.mark.unit
def test_similarity_is_symmetric_and_safe_on_empty():
    assert F.similarity("", "任意") == 0.0
    assert F.similarity("混合检索", "") == 0.0
    a, b = "稀疏检索与稠密检索的融合", "稠密检索与稀疏检索的融合"
    assert F.similarity(a, b) == F.similarity(b, a) > 0.5


@pytest.mark.unit
async def test_suggest_future_directions_filters_the_model_output(monkeypatch):
    """端到端（模型被顶掉）：模型给 4 个方向，其中 2 个已解决/不可溯源 → 只剩 2 个。"""
    graph = make_graph()
    survey = S.Survey(
        title="t",
        overview="o",
        timeline=[S.TimelineEntry(year=2020, milestone="m", paper_ids=["p0"])],
        core_papers=[S.CoreContribution(paper_id="p0", contribution="提出对比学习改进检索表示")],
        open_problems=["长文档检索粒度未定论"],
    )

    async def fake_complete(schema, messages, **kwargs):  # noqa: ARG001
        return F.IdeaList(
            ideas=[
                F.FutureIdea(title="用对比学习改进检索表示", rationale="x", based_on=["p0"]),  # 已解决 → 丢
                F.FutureIdea(title="图外的方向", rationale="y", based_on=["ghost"]),  # 不可溯源 → 丢
                F.FutureIdea(title="长文档检索粒度未定论", rationale="z", based_on=["p1"]),
                F.FutureIdea(title="多模态检索的联合训练", rationale="w", based_on=["p2"]),
            ]
        )

    monkeypatch.setattr(F, "complete_structured", fake_complete)
    out = await F.suggest_future_directions(survey, valid_ids={str(n) for n in graph.nodes})

    assert out["count"] == 2
    assert out["ideas"][0]["from_open_problems"] is True
    assert {d["idea"] for d in out["dropped"]} == {"用对比学习改进检索表示", "图外的方向"}
    assert "只剩 2 个方向" in out["note"]  # 不足 3 条要明说，而不是悄悄凑数
