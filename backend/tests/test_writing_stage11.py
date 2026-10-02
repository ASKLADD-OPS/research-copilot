"""阶段 11 验收：学术写作辅助。

四条验收标准分别对应下面的四组用例：

1. Idea → 完整大纲（`Test 1`）—— 规划模型不可用时也必须给出标准六节；
2. 正文生成含真实引用（`Test 2`）—— 草稿里的 `[n]` 要能落回真实 chunk 并带上元数据；
3. 幻觉引用被检出并标记（`Test 3`）—— 越界编号在**返回前**就被摘掉，不是等前端标灰；
4. 4 种图表类型都能生成（`Test 4`）—— 缺渲染器时降级为"只给源码"，但绝不假装成功。

全部用例**不打网络、不连库、不加载 NLI 模型**：`get_tracer()` 默认会懒加载
`cross-encoder/nli-deberta-v3-base`，网络不通时不是抛错而是长时间挂起（见 conftest 里
`tracer` fixture 的说明），所以每一处都换成 `enable_nli=False` 的词法代理。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.rag.retriever import RetrievedChunk
from app.rag.source_tracing import Citation, SourceTracer, TraceReport
from app.schemas import (
    DiagramRequest,
    ExpandRequest,
    OutlineRequest,
    ReferenceRequest,
)
from app.writing import diagrams, outline, references

# ==================================================================== 测试替身
GLUE_EVIDENCE = "Our method improves accuracy by 12% on GLUE benchmark compared to BERT."
ATTENTION_EVIDENCE = "We use a sparse attention mechanism to reduce quadratic complexity."


class FakePaper:
    """只带 `ReferenceOut` 用到的字段 —— 参考文献表只认元数据，不认 ORM 的其他部分。"""

    def __init__(self, pid: int, title: str, authors: list[dict[str, str]], year: int, venue: str) -> None:
        self.id = pid
        self.title = title
        self.authors = authors
        self.year = year
        self.venue = venue
        self.doi = None
        self.arxiv_id = f"2401.{pid:05d}"
        self.source_url = f"https://arxiv.org/abs/2401.{pid:05d}"


PAPER = FakePaper(7, "Sparse Routing for Mixture of Experts", [{"name": "Zhang Y"}, {"name": "Li X"}], 2024, "NeurIPS")


class FakeLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 1
        self.total_tokens = 42

    @property
    def usage(self) -> Any:
        return self

    async def complete(self, *_: Any, **__: Any) -> str:
        return self.text


class FakeRetriever:
    def __init__(self, chunks: Sequence[RetrievedChunk]) -> None:
        self.chunks = list(chunks)

    async def retrieve(self, *_: Any, **__: Any) -> list[RetrievedChunk]:
        return self.chunks


def make_chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(id=101, paper_id=7, content=GLUE_EVIDENCE, section="Method", page=4),
        RetrievedChunk(id=102, paper_id=7, content=ATTENTION_EVIDENCE, section="Method", page=5),
    ]


@pytest.fixture
def lexical_tracer() -> SourceTracer:
    return SourceTracer(enable_nli=False)


@pytest.fixture
def patch_outline(monkeypatch: pytest.MonkeyPatch, lexical_tracer: SourceTracer):
    """把 `outline` 模块的外部依赖换成替身，返回一个"改 LLM 回复"的钩子。"""

    def install(llm_text: str, chunks: list[RetrievedChunk] | None = None) -> None:
        monkeypatch.setattr(outline, "HybridRetriever", lambda *a, **k: FakeRetriever(chunks or make_chunks()))
        monkeypatch.setattr(outline, "get_llm", lambda: FakeLLM(llm_text))
        monkeypatch.setattr(outline, "get_tracer", lambda: lexical_tracer)
        # 两处都要打：`build_outline` 自己补 title 用 outline 的那份，
        # 排参考文献表走的是 references 模块里同名的那个函数
        monkeypatch.setattr(outline, "load_paper_meta", _fake_load_paper_meta)
        monkeypatch.setattr(references, "load_paper_meta", _fake_load_paper_meta)

    return install


async def _fake_load_paper_meta(_session: Any, paper_ids: Any) -> dict[int, FakePaper]:
    return {7: PAPER} if 7 in {int(p) for p in paper_ids} else {}


# ==================================================================== marker 工具
def test_marker_remap_and_drop_keep_syntax_valid() -> None:
    # 全局重编号：局部 [1] → 全篇 [3]
    assert references.remap_markers("见 [1] 与 [2]。", {1: 3, 2: 4}) == "见 [3] 与 [4]。"
    # 恒等映射不动原文（含区间写法）
    assert references.remap_markers("a [3-5] b [1,2]", {}) == "a [3-5] b [1,2]"
    assert references.remap_markers("x [1]", {1: 1}) == "x [1]"
    # 摘编号：一块里的部分编号越界时只留合法的，不留 `[1,]` 这种残骸
    assert references.drop_markers("结论 [1,7] 成立。", [7]) == "结论 [1] 成立。"
    assert references.drop_markers("结论 [7] 成立。", [7]) == "结论  成立。"
    assert references.drop_markers("结论 [7] 成立。", [7], placeholder=True) == "结论 [citation needed] 成立。"
    assert references.drop_markers("无编号 [1]。", []) == "无编号 [1]。"


def test_reference_text_comes_from_metadata_only() -> None:
    line = references.format_reference(PAPER, marker=3, language="zh")
    assert line.startswith("[3] Zhang Y, Li X")
    assert "Sparse Routing for Mixture of Experts" in line
    assert "NeurIPS, 2024" in line  # 会议与年份都取自元数据
    assert "arXiv:2401.00007" in line
    # 元数据三项全空 → 不该被排进参考文献表
    blank = FakePaper(9, "", [], None, None)  # type: ignore[arg-type]
    assert references.metadata_ok(blank) is False
    assert references.metadata_ok(PAPER) is True


# ==================================================================== 验收 1：Idea → 完整大纲
async def test_planner_failure_still_yields_the_six_standard_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(outline, "complete_structured", boom)
    plan = await outline.plan_outline("用稀疏路由降低 MoE 推理开销")
    assert [s.key for s in plan.sections] == list(outline.DEFAULT_SECTIONS)
    assert all(s.title and s.brief for s in plan.sections)


async def test_user_requested_sections_win_over_planner_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.writing.outline import OutlinePlan, SectionPlan

    async def plan_with_wrong_keys(*_: Any, **__: Any) -> OutlinePlan:
        return OutlinePlan(
            title="t",
            sections=[SectionPlan(key="method", title="方法"), SectionPlan(key="intro", title="引言")],
        )

    monkeypatch.setattr(outline, "complete_structured", plan_with_wrong_keys)
    plan = await outline.plan_outline("idea", sections=["introduction", "method", "conclusion"])
    # 用户点了三节就必须有三节（模型漏掉的用兜底标题补上），顺序也按用户的来
    assert [s.key for s in plan.sections] == ["introduction", "method", "conclusion"]
    assert plan.sections[0].title == "引言"


async def test_build_outline_returns_each_section_with_its_own_evidence(
    patch_outline: Any, lexical_tracer: SourceTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.writing.outline import OutlinePlan, SectionPlan

    async def plan_two_sections(*_: Any, **__: Any) -> OutlinePlan:
        return OutlinePlan(
            title="稀疏路由",
            sections=[SectionPlan(key="method", title="方法"), SectionPlan(key="experiment", title="实验")],
        )

    monkeypatch.setattr(outline, "complete_structured", plan_two_sections)
    patch_outline("Our method improves accuracy by 12% on GLUE [1].")
    result = await outline.build_outline(None, OutlineRequest(idea="稀疏路由", evidence_per_section=2))  # type: ignore[arg-type]

    assert [s.key for s in result.sections] == ["method", "experiment"]
    assert all(s.draft and s.evidence_count == 2 for s in result.sections)
    # 两节引的是同一块证据 → 全篇只该有一条参考文献（不是两节各一条）
    assert [r.marker for r in result.references] == [1]
    assert result.references[0].title == PAPER.title
    assert result.references[0].authors == ["Zhang Y", "Li X"]


async def test_outline_without_draft_skips_retrieval_entirely(
    monkeypatch: pytest.MonkeyPatch, lexical_tracer: SourceTracer
) -> None:
    from app.writing.outline import OutlinePlan, SectionPlan

    async def plan_one(*_: Any, **__: Any) -> OutlinePlan:
        return OutlinePlan(sections=[SectionPlan(key="method", title="方法")])

    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError("draft=false 不该去检索")

    monkeypatch.setattr(outline, "complete_structured", plan_one)
    monkeypatch.setattr(outline, "HybridRetriever", explode)
    result = await outline.build_outline(None, OutlineRequest(idea="x", draft=False))  # type: ignore[arg-type]
    assert result.sections[0].draft == ""
    assert result.references == []


def test_global_markers_are_shared_across_sections() -> None:
    """同一块证据在 Method 和 Experiment 都被引 → 必须是同一个全篇编号。"""

    def report(marker: int, chunk_id: str) -> TraceReport:
        rep = TraceReport()
        rep.citations = [Citation(marker=marker, chunk_id=chunk_id, paper_id="7", supported=True)]
        rep.terms_total, rep.terms_supported = 10, 8
        return rep

    drafts = [
        outline.SectionDraft(
            plan=outline.SectionPlan(key="method", title="方法"), text="a [1]", report=report(1, "101")
        ),
        outline.SectionDraft(
            plan=outline.SectionPlan(key="experiment", title="实验"), text="b [1] c [2]", report=report(1, "102")
        ),
    ]
    drafts[1].report.citations.append(Citation(marker=2, chunk_id="101", paper_id="7", supported=True))
    per_section, chunk_marker = outline.assign_global_markers(drafts)
    assert per_section[0] == {1: 1}
    assert per_section[1] == {1: 2, 2: 1}  # 局部 [1] 是块 102 → 全篇 2；局部 [2] 是块 101 → 全篇 1
    assert chunk_marker == {101: 1, 102: 2}


# ==================================================================== 验收 2：正文含真实引用
async def test_draft_section_marks_verified_citations(patch_outline: Any) -> None:
    patch_outline("Our method improves accuracy by 12% on GLUE [1].")
    draft = await outline.draft_section(
        "稀疏路由", outline.SectionPlan(key="method", title="方法", brief="说明方法"), top_k=2
    )
    assert len(draft.report.citations) == 1
    citation = draft.report.citations[0]
    assert citation.chunk_id == 101
    assert citation.supported is True  # 词法蕴含 + 数字一致
    assert draft.report.grounding_ratio > 0
    assert draft.dropped == []


async def test_expand_offsets_markers_so_they_do_not_collide(patch_outline: Any) -> None:
    patch_outline("Our method improves accuracy by 12% on GLUE [1].")
    result = await outline.expand_paragraph(
        None,  # type: ignore[arg-type]
        ExpandRequest(text="先说一句。", section="method", marker_offset=5, evidence_top_k=2),
    )
    assert "[6]" in result.text  # 局部 [1] + 偏移 5
    assert [c.marker for c in result.citations] == [6]
    assert result.citations[0].title == PAPER.title


# ==================================================================== 验收 3：幻觉引用被检出
def test_phantom_markers_are_detected_even_without_evidence() -> None:
    text = "凭空写一个 [4] 与 [9]。"
    # 没有证据时 `trace()` 会提前返回、不填 phantom_markers（它把这种情况归到 uncited_claims），
    # 但那恰恰是最该拦的一支 —— 没有检索到任何材料，正文里的每个编号都是编的。
    assert outline.phantoms_of(text, [], TraceReport()) == [4, 9]
    chunk = make_chunks()[0]
    report = SourceTracer(enable_nli=False).trace(text, [chunk])
    assert outline.phantoms_of(text, [chunk], report) == [4, 9]


async def test_phantom_citations_are_stripped_before_returning(
    patch_outline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.writing.outline import OutlinePlan, SectionPlan

    async def plan_one(*_: Any, **__: Any) -> OutlinePlan:
        return OutlinePlan(sections=[SectionPlan(key="method", title="方法")])

    monkeypatch.setattr(outline, "complete_structured", plan_one)
    patch_outline("Our method improves accuracy by 12% on GLUE [1]. 另外该方向已有定论 [7]。")
    result = await outline.build_outline(None, OutlineRequest(idea="x"))  # type: ignore[arg-type]

    section = result.sections[0]
    assert section.removed_markers == [7]
    assert "[7]" not in section.draft  # 越界编号在返回前就被摘掉
    assert "[1]" in section.draft
    assert result.phantom_markers == [7]
    assert [c.marker for c in section.citations] == [1]


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """按 `select(...)` 的目标实体分派结果 —— 够 `build_references` 用，别的不装。"""

    def __init__(self, chunks: list[Any], papers: list[Any]) -> None:
        self.chunks = chunks
        self.papers = papers

    async def execute(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        return _FakeResult(self.chunks if entity.__name__ == "Chunk" else self.papers)


def _chunk_row(cid: int, content: str, paper_id: int = 7) -> Any:
    from app.models import Chunk

    return Chunk(id=cid, paper_id=paper_id, content=content, section="Method", page=4)


@pytest.fixture
def patch_references(monkeypatch: pytest.MonkeyPatch, lexical_tracer: SourceTracer):
    def install(chunks: list[Any], papers: list[Any]) -> _FakeSession:
        session = _FakeSession(chunks, papers)
        monkeypatch.setattr(references, "get_tracer", lambda: lexical_tracer)
        return session

    return install


async def test_references_endpoint_flags_each_failure_mode(patch_references: Any) -> None:
    session = patch_references([_chunk_row(101, GLUE_EVIDENCE)], [PAPER])
    content = (
        "Our method improves accuracy by 12% on GLUE [1]. "  # 三关全过
        "The method improves accuracy by 21% on GLUE [1]. "  # 数字被篡改 → weak
        "A claim whose evidence was deleted [2]. "  # chunk 不在库里 → chunk_missing
        "A completely fabricated statement [9]."  # 编号越界 → phantom
    )
    citations = [
        {"marker": 1, "chunk_id": 101, "paper_id": 7},
        {"marker": 2, "chunk_id": 999, "paper_id": 7},
    ]
    result = await references.build_references(
        session,
        ReferenceRequest(content=content, citations=citations, remove_invalid=True),  # type: ignore[arg-type]
    )

    status = {c.marker: c.status for c in result.checks}
    assert status == {1: "weak", 2: "chunk_missing", 9: "phantom"}
    assert result.removed_markers == [2, 9]
    assert result.flagged_markers == [1]  # 近义改写保留下来，由人核对
    assert "[2]" not in result.content and "[9]" not in result.content
    assert "[1]" in result.content
    # 待核对的那条数字对不上，理由里必须写清楚是哪一关没过
    weak = next(c for c in result.checks if c.marker == 1)
    assert weak.numbers_ok is False and "数字" in weak.reason


async def test_references_keeps_invalid_markers_when_asked(patch_references: Any) -> None:
    session = patch_references([_chunk_row(101, GLUE_EVIDENCE)], [PAPER])
    result = await references.build_references(
        session,  # type: ignore[arg-type]
        ReferenceRequest(content="编的一句话 [9]。", citations=[{"marker": 1, "chunk_id": 101}], remove_invalid=False),
    )
    assert "[citation needed]" in result.content  # 就地留痕，方便回头补文献
    assert result.invalid_count == 1


async def test_references_without_metadata_is_a_hard_failure(patch_references: Any) -> None:
    session = patch_references([_chunk_row(101, GLUE_EVIDENCE)], [])  # 论文不在库里
    result = await references.build_references(
        session,  # type: ignore[arg-type]
        ReferenceRequest(
            content="Our method improves accuracy by 12% on GLUE [1].", citations=[{"marker": 1, "chunk_id": 101}]
        ),
    )
    assert result.checks[0].status == "metadata_missing"
    assert result.references == []  # 排不进参考文献表，就不该出现在里面


async def test_references_reretrieves_when_citations_are_not_echoed(
    patch_references: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = patch_references([_chunk_row(101, GLUE_EVIDENCE)], [PAPER])
    monkeypatch.setattr(references, "HybridRetriever", lambda *a, **k: FakeRetriever(make_chunks()))
    result = await references.build_references(
        session, ReferenceRequest(content="Our method improves accuracy by 12% on GLUE [1].", query="GLUE")
    )  # type: ignore[arg-type]
    assert result.checks[0].status == "ok"
    assert result.bibliography and result.bibliography[0].startswith("[1] Zhang Y")


# ==================================================================== 验收 4：四种图表
def test_matplotlib_chart_renders_a_real_png() -> None:
    spec = diagrams.ChartSpec(
        chart_type="bar",
        title="各模块延迟",
        categories=["解析", "索引", "检索"],
        series=[diagrams.ChartSeries(name="P50", data=[120, 340, 88])],
    )
    png = diagrams._chart_png(spec)  # noqa: SLF001 - 直接验证渲染管线
    assert png[:4] == b"\x89PNG"
    assert len(png) > 1000


def test_dot_topology_reads_edges_and_labels() -> None:
    dot = 'digraph G {\n rankdir=LR;\n node [shape=box];\n a [label="上传"];\n b [label="解析"];\n a -> b [label="读入"];\n b -> c;\n}'
    edges, labels = diagrams.dot_topology(dot)
    assert edges == [("a", "b"), ("b", "c")]
    # 带属性的边不能把 "a -> b" 整个当成节点名（这是踩过的坑）
    assert labels == {"a": "上传", "b": "解析", "c": "c"}


def test_graphviz_falls_back_to_a_topology_sketch_without_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagrams, "find_dot", lambda: None)
    image, renderer, warning = diagrams.render_graphviz("digraph G { a -> b; }")
    assert renderer == "networkx-fallback"
    assert image and "未安装 Graphviz" in warning


def test_mermaid_renders_png_when_the_cli_is_available() -> None:
    if not (diagrams.find_mmdc() and diagrams.find_chrome()):
        pytest.skip("mmdc / Chrome 不可用")
    image, renderer, warning = diagrams.render_mermaid("flowchart LR\n  A[上传] --> B[解析]\n")
    assert renderer == "mmdc" and warning == ""
    assert image


async def test_all_four_diagram_kinds_produce_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """验收第 4 条：四种类型都能生成 —— 能出图就出图，不能就只给源码，但都得有产出。"""

    async def fake_source(kind: str, *_: Any, **__: Any) -> Any:
        if kind == "matplotlib":
            return diagrams.ChartSpec(
                chart_type="line",
                title="准确率",
                categories=["0", "1"],
                series=[diagrams.ChartSeries(name="ours", data=[0.8, 0.9])],
                caption="Figure 1: accuracy",
            )
        body = {
            "tikz": "\\begin{tikzpicture}\\node {A};\\end{tikzpicture}",
            "graphviz": "digraph G { a -> b; }",
            "mermaid": "```mermaid\nflowchart LR\n A --> B\n```",  # 顺带验证围栏会被剥掉
        }[kind]
        return diagrams.DiagramCode(source=body, caption="Figure 1: pipeline")

    monkeypatch.setattr(diagrams, "_generate_source", fake_source)
    monkeypatch.setattr(diagrams, "find_dot", lambda: None)  # 强制走兜底，避免依赖本机装没装

    seen: dict[str, Any] = {}
    for kind in ("tikz", "graphviz", "mermaid", "matplotlib"):
        result = await diagrams.generate_diagram(DiagramRequest(kind=kind, instruction="画个架构"))  # type: ignore[arg-type]
        assert result.kind == kind
        assert result.source.strip(), f"{kind} 必须至少给出源码"
        assert not result.source.startswith("```"), f"{kind} 的源码不该带 markdown 围栏"
        assert result.renderer, f"{kind} 必须说明用的是什么渲染器"
        seen[kind] = result

    # TikZ 刻意不装 TeX，只给源码 —— 但必须如实告知，不能假装出图了
    assert seen["tikz"].image is None and "TikZ" in seen["tikz"].warning
    # Graphviz 无 dot 时退化为拓扑草图（仍是真图）
    assert seen["graphviz"].image and seen["graphviz"].renderer == "networkx-fallback"
    assert seen["matplotlib"].image and seen["matplotlib"].renderer == "matplotlib"


async def test_matplotlib_refuses_to_invent_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(*_: Any, **__: Any) -> Any:
        return diagrams.ChartSpec(chart_type="bar", title="没有数据")

    monkeypatch.setattr(diagrams, "_generate_source", empty)
    result = await diagrams.generate_diagram(DiagramRequest(kind="matplotlib", instruction="画个趋势图"))  # type: ignore[arg-type]
    assert result.image is None
    assert "不编造" in result.warning
