"""关键路径集成测试 —— 钉的是**阶段之间**的接缝，不是阶段内部。

为什么单独一个文件
------------------
四个阶段的内部都很瓷实，但它们是四份互不相干的测试：

| 阶段 | 现有用例 |
|---|---|
| PDF 上传 / 解析 / 分块 | `test_parsers_stage7.py`（47 条） |
| 混合检索 / RRF 融合 | `test_retriever_chain.py`、`test_fusion.py` |
| RAG 问答 / SSE 规格事件 | `test_qa_stream_api.py`、`test_qa_sources_api.py` |
| 溯源（Self-Citation + 蕴含验证） | `test_source_tracing.py`（41 条） |

**没有一个用例跨过「解析产物 → 检索结果」这条接缝**：`chunk_document()` 吐出的
`Chunk` 没有 `paper_id`、页码字段叫 `page_start` 而不是 `page`，谁把它装配成
`RetrievedChunk` 一直没人测。这条装配线一旦写错（字段名、bbox 丢、页码取错），
溯源面板就会指到错误的页 —— 而四个阶段各自的测试全绿。

另外三条关键路径**已经由既有用例整条走通**，这里刻意不重复实现（重复实现等于
维护两份真相，改一处漏一处的经典来源）。对照表与逐条可跑的命令见
`docs/提交材料与验收.md`：

- 意图识别 → 计划 → 执行 → 反思 → 重规划：`test_agent_graph_e2e.py`（12 条 `e2e`）
- MCP 装载 → Function Calling → 结果回传：`test_mcp_tools.py`（21 条）
- 图谱构建 → 分析 → 综述：`test_graph_analysis.py`（22 条）

跑法
----
    pytest tests/test_key_paths.py -v

不连库、不拉模型、不发外网：PDF 用 PyMuPDF 现造，模型那一步由确定性字符串替代
（本仓既有约定 —— 模型输出不是被测对象，接缝才是）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parsers import chunk_document, parse_pdf
from app.rag.fusion import rrf_fuse
from app.rag.retriever import RetrievedChunk
from app.rag.source_tracing import SourceTracer, split_sentences

pytestmark = pytest.mark.e2e

PAPER_ID = 1
SECTION = "Method"


# ==================================================================== 造真 PDF
def _paper_pdf(path: Path) -> Path:
    """一份真实 PDF。逐行 insert_text，坐标可控（版面算法吃的就是坐标）。

    正文刻意含两处数字（60 / 0.82）—— 溯源引擎有一条"答案里的数字必须在原文里"
    的硬规则，让它真的被走到。
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 80), "Hybrid Retrieval for Scientific QA", fontsize=16)
    body = [
        "The hybrid retriever fuses dense and sparse rankings with RRF.",
        "The fusion constant k is set to 60 in all of our experiments.",
        "We report a grounding ratio of 0.82 on the held out set.",
    ]
    for i, line in enumerate(body):
        page.insert_text((50, 140 + i * 16), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _as_retrieved(chunks) -> list[RetrievedChunk]:
    """★ 这就是被测的那条接缝：解析产物 → 检索结果。

    三件事必须同时做对，否则下游全错：
    1. `paper_id` 补上（`Chunk` 里根本没有这个字段，分块时还不知道是谁的块）；
    2. `page_start` → `page`（`RetrievedChunk` 用 `page`，`slots` dataclass 没有兜底属性，
       取错名就是 `AttributeError`）；
    3. `bbox` 原样带过（丢了它，前端溯源徽章就没法在 PDF 上定位）。
    """
    return [
        RetrievedChunk(
            id=c.index,
            paper_id=PAPER_ID,
            content=c.content,
            section=SECTION,
            page=c.page_start,
            bbox=list(c.bbox) if c.bbox else None,
            score=0.0,
            sources=[],
        )
        for c in chunks
        if c.bbox is not None  # 检索链路只索引带坐标的块
    ]


def _hybrid_rank(retrieved: list[RetrievedChunk]) -> list[tuple[RetrievedChunk, float]]:
    """两路假召回 → 真 RRF。第一路顺序倒过来，模拟"语义序"与"关键词序"不一致。"""
    dense = [retrieved[0], *reversed(retrieved[1:])]
    sparse = [retrieved[0], *retrieved[1:]]
    return rrf_fuse(dense, sparse)


# ==================================================================== 关键路径 1
@pytest.mark.e2e
async def test_chain_pdf_to_citation_keeps_page_and_bbox(tmp_path: Path):
    """PDF → 解析 → 分块 → 混合检索(RRF) → 生成 → 溯源，一次走通。

    断言的是**端到端不变式**：溯源给出的页码 / bbox / chunk_id 必须等于 PDF 里
    那一块的真实坐标 —— 中间任何一环把页码或坐标弄丢，这里就红。
    """
    # ---- 1. 真实 PDF 落盘 → 解析 ----
    pdf = _paper_pdf(tmp_path / "paper.pdf")
    doc = parse_pdf(pdf)
    assert doc.pages, "解析器没吐出任何页"
    assert "RRF" in doc.pages[0].text, "正文没被解析出来"

    # ---- 2. 分块：块上带 chunk_type / page / 归一化 bbox ----
    chunks = chunk_document(doc)
    assert chunks, "分块结果为空"
    for c in chunks:
        if c.bbox is not None:
            assert all(0.0 <= v <= 1.0 for v in c.bbox), f"bbox 未归一化: {c.bbox}"

    # ---- 3. 接缝：解析产物 → 检索结果 ----
    retrieved = _as_retrieved(chunks)
    assert retrieved, "带坐标的块一个都没有，接缝断了"
    assert all(r.paper_id == PAPER_ID for r in retrieved)
    assert all(isinstance(r.page, int) for r in retrieved)

    # ---- 4. 混合检索：RRF 融合并标注来源 ----
    fused = _hybrid_rank(retrieved)
    top, _score = fused[0]
    assert top.sources == ["dense", "sparse"], (
        f"两路都召回的块应标 both，实际 {top.sources} —— RRF 的来源标注丢了"
    )

    # ---- 5. 生成（确定性替代模型；模型输出不是被测对象）----
    # 每句都挂 [1]，否则"无编号的句子"会被判为无据声明，grounding 直接被拉低 ——
    # 那是**故意**的严谨，不是 bug。
    answer = " ".join(f"{s.rstrip()} [1]" for s in split_sentences(top.content))

    # ---- 6. 溯源：Self-Citation + 词法蕴含（enable_nli=False，不拉权重）----
    report = SourceTracer(enable_nli=False).trace(answer, [r for r, _ in fused])

    # 编号没有越界（白名单校验通过）
    assert report.phantom_markers == []
    assert report.citations, "一条引用都没抽出来，答句里的 [1] 没被识别"

    cite = report.citations[0]
    # ★ 端到端不变式：溯源指回的坐标，必须是 PDF 里那一块的真实坐标
    assert cite.chunk_id == top.id, f"溯源指到 {cite.chunk_id}，答句引用的是 {top.id}"
    assert cite.page_start == top.page, f"页码漂了：溯源 {cite.page_start} vs 原文 {top.page}"
    assert cite.bbox == top.bbox, "bbox 没从解析一路带到溯源 —— 前端无法定位高亮"
    assert cite.paper_id == PAPER_ID
    assert cite.supported is True, f"引用未被判为支持：{cite.answer_span!r}"
    assert cite.nli_score >= 0.8

    # 实测的原始数字仍然来自 PDF 正文（不是模型编的）
    assert "60" in cite.quote and "0.82" in cite.quote

    assert report.grounding_ratio >= 0.8, f"有据率 {report.grounding_ratio} 低于门限"
    assert report.passed is True


# ==================================================================== 反例守卫
@pytest.mark.e2e
async def test_chain_trace_rejects_a_number_the_pdf_never_said(tmp_path: Path):
    """引用同一块、但把原文的 60 说成 600 → 必须被判无据。

    正向链路绿还不够：这条守卫保证"有据率"不是恒真的装饰 —— 数字硬规则真的在拦。
    """
    pdf = _paper_pdf(tmp_path / "paper.pdf")
    retrieved = _as_retrieved(chunk_document(parse_pdf(pdf)))
    fused = _hybrid_rank(retrieved)
    top, _ = fused[0]
    assert "60" in top.content, "构造前提不成立：原文里没有 60"

    # 只说一句、只引 [1]，把被篡改的数字关进这一句里。
    # （整段拼接再挂一个 [1] 是无效构造：编号只作用于最后一句，篡改句会漏检。）
    claim = "The fusion constant k is set to 600 in all of our experiments. [1]"
    report = SourceTracer(enable_nli=False).trace(claim, [r for r, _ in fused])

    assert report.number_mismatches, "数字从 60 被改成 600，却没有记进 number_mismatches"
    assert report.citations and report.citations[0].supported is False
    assert report.passed is False
