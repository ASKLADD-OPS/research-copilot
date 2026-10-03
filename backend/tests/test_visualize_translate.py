"""CSV 可视化 + 学术翻译验收。

四条验收标准对应下面四组：

1. 上传 CSV → 自动出图 + 图注（`Test 1`）—— 六种图型逐个跑通，PNG 真的画出来；
2. 图注是 `Figure N: …` 的学术形状（`Test 1` 里逐条断言）；
3. 术语一致（`Test 3`）—— 术语表真的进了 prompt、`unused_terms` 能被核对；
4. 段落 ID 映射（`Test 3`）—— `p1/p2/…` 与原文序号一一对应，前端才对得齐两栏。

全部**不连库、不打网络**：`complete_structured` / `get_llm` 一律换替身，
matplotlib 走进程内 Agg（真渲染，但不弹窗口、不拉网络字体）。
"""

from __future__ import annotations

import base64
import csv
import io
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.v1 import translate as translate_api
from app.schemas import GlossaryEntry, TranslateParagraphsRequest
from app.schemas.translate import SourceBlock
from app.visualize import csv_charts
from app.writing.diagrams import _chart_png

# ==================================================================== 测试数据
CSV_TEXT = """month,region,revenue,cost,users,note
2024-01,华东,120,80,12,ok
2024-01,华南,90,60,9,ok
2024-02,华东,150,95,15,ok
2024-02,华南,110,70,11,ok
2024-03,华东,180,100,18,ok
2024-03,华南,,75,13,缺数据
2024-04,华东,210,120,21,ok
2024-04,华南,160,110,16,ok
"""


@pytest.fixture
def dataset(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把落盘目录指到 tmp_path —— 测试不许往仓库的 data/ 里写东西。"""
    monkeypatch.setattr(csv_charts, "csv_dir", lambda: tmp_path)
    dataset_id, _ = csv_charts.save_csv("sales.csv", CSV_TEXT.encode("utf-8"))
    return csv_charts.load_dataset(dataset_id)


def _plan(**kw: Any) -> Any:
    return csv_charts._ChartPlan(**kw)


# ==================================================================== 读 + 判类型
@pytest.mark.unit
def test_delimiter_and_column_types_are_detected() -> None:
    header, rows, delimiter, total, truncated, warning = csv_charts.parse_csv(CSV_TEXT)
    assert header == ["month", "region", "revenue", "cost", "users", "note"]
    assert delimiter == ","
    assert total == 8 and not truncated and warning == ""

    profile = csv_charts.profile_dataset("d1", "sales.csv", header, rows, delimiter, total, truncated, warning)
    assert {c.name: c.dtype for c in profile.columns} == {
        "month": "datetime",
        "region": "text",
        "revenue": "numeric",
        "cost": "numeric",
        "users": "numeric",
        "note": "text",
    }
    assert profile.numeric_columns == ["revenue", "cost", "users"]
    assert "region" in profile.categorical_columns
    # 一个空格子要被算成缺失，不是 0；min/max 也不能把它当 0 读进去
    revenue = next(c for c in profile.columns if c.name == "revenue")
    assert revenue.missing == 1 and revenue.min == 90 and revenue.max == 210


@pytest.mark.unit
def test_delimiter_sniff_falls_back_to_tab(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_charts, "csv_dir", lambda: tmp_path)
    dataset_id, _ = csv_charts.save_csv("t.tsv", b"a\tb\n1\t2\n3\t4\n")
    header, rows, delimiter, *_ = csv_charts.read_csv(csv_charts.dataset_path(dataset_id))
    assert header == ["a", "b"] and delimiter == "\t" and len(rows) == 2


# ==================================================================== Test 1：六种图型
@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "plan_kwargs"),
    [
        ("line", {"x": "month", "y": ["revenue", "cost"]}),
        ("bar", {"x": "region", "y": ["revenue"]}),
        ("scatter", {"x": "revenue", "y": ["cost"]}),
        ("heatmap", {"x": "region", "y": ["revenue", "cost"]}),
        ("boxplot", {"x": "region", "y": ["revenue"]}),
        ("radar", {"x": "region", "y": ["revenue", "cost", "users"]}),
    ],
)
def test_every_chart_type_builds_a_renderable_spec(dataset: Any, kind: str, plan_kwargs: dict[str, Any]) -> None:
    """六种图型都要：规格成立、真渲染出 PNG、图注是 Figure N 的形状。"""
    profile, rows = dataset
    spec, warning = csv_charts.build_spec(_plan(chart_type=kind, caption="", **plan_kwargs), profile, rows)

    assert spec.chart_type == kind, f"{kind} 退化了：{warning}"
    assert spec.series and any(s.data for s in spec.series), f"{kind} 没构建出任何系列（warning={warning}）"
    assert spec.caption.startswith("Figure 1: ")
    assert _chart_png(spec)[:4] == b"\x89PNG"


@pytest.mark.unit
def test_caption_is_normalized_to_figure_n() -> None:
    assert csv_charts._caption("Figure 3: accuracy by model.", "x") == "Figure 3: accuracy by model."
    assert csv_charts._caption("accuracy by model", "x") == "Figure 1: accuracy by model"
    assert csv_charts._caption("", "各地区收入对比") == "Figure 1: 各地区收入对比"
    # 中文冒号也要认出来是"已经有前缀了"，不要拼成 "Figure 1: Figure 2：…"
    assert csv_charts._caption("Figure 2：中文冒号。", "x") == "Figure 2：中文冒号。"


@pytest.mark.unit
def test_no_llm_means_caption_must_still_read_as_a_sentence(dataset: Any) -> None:
    """指定图型时**不调模型**，于是兜底标题会直接变成图注正文。

    真跑出来的样子：caption 写成 "Figure 1: month 上的 revenue, cost, users" ——
    列名拼接读不通。图注是验收标准里唯一有格式要求的交付物，不能是机器味儿的串。
    """
    profile, rows = dataset
    spec, _ = csv_charts.build_spec(_plan(chart_type="bar", x="month", y=["revenue", "cost"]), profile, rows)
    assert " 上的 " not in spec.caption
    assert spec.caption == "Figure 1: 各 month 的 revenue, cost"
    assert _chart_png(spec)[:4] == b"\x89PNG"


@pytest.mark.unit
def test_scatter_without_a_chosen_pair_keeps_one_series(dataset: Any) -> None:
    """没人指定 x 时 x 是我们挑的，那就只画一条 y。

    真跑出来的样子：revenue(120) / cost(75) / users(1100) 挤在同一根 y 轴上，
    量纲差两个数量级，图注写成 "latency_ms vs revenue, cost, users" —— 那张图没有含义。
    """
    profile, rows = dataset
    profile.numeric_columns = ["revenue", "cost", "users"]
    spec, _ = csv_charts.build_spec(_plan(chart_type="scatter", x="", y=[]), profile, rows)
    assert spec.chart_type == "scatter"
    assert len(spec.series) == 1
    assert spec.xlabel != spec.series[0].name  # x 与 y 不能是同一列

    # 有人明确指了配对关系，就照他说的画
    named, _ = csv_charts.build_spec(_plan(chart_type="scatter", x="revenue", y=["cost", "users"]), profile, rows)
    assert {s.name for s in named.series} == {"cost", "users"}


@pytest.mark.unit
def test_hallucinated_column_names_are_dropped_not_rendered(dataset: Any) -> None:
    """模型编出来的列名必须回表核对 —— 否则图表就是一张空白，还看不出为什么。"""
    profile, rows = dataset
    spec, _ = csv_charts.build_spec(_plan(chart_type="bar", x="不存在的列", y=["幻觉数值列"]), profile, rows)
    assert spec.xlabel in {c.name for c in profile.columns}
    assert all(s.name in profile.numeric_columns for s in spec.series)


@pytest.mark.unit
def test_chart_type_unsuitable_for_data_degrades_with_a_reason(dataset: Any) -> None:
    """没有分类列时热力图无解 —— 退回柱状图并说明，而不是抛 500 或画张空图。"""
    profile, rows = dataset
    profile.categorical_columns = []  # 模拟"整张表全是数值"
    spec, warning = csv_charts.build_spec(_plan(chart_type="heatmap", x="", y=["revenue"]), profile, rows)
    assert "heatmap" in warning
    # 提示里的图型必须与真画出来的那个一致 —— 说"已改用折线图"却给出柱状图比不降级更误导
    assert f"已改用{spec.chart_type}图" in warning
    assert spec.series


@pytest.mark.unit
def test_radar_needs_three_axes_and_says_so_instead_of_drawing_two(tmp_path, monkeypatch) -> None:
    """只有 2 个数值列时雷达图是废的（两条线分不出形状）—— 退化并说明。"""
    monkeypatch.setattr(csv_charts, "csv_dir", lambda: tmp_path)
    dataset_id, _ = csv_charts.save_csv("two.csv", b"g,a,b\nx,1,2\ny,3,4\n")
    profile, rows = csv_charts.load_dataset(dataset_id)
    spec, warning = csv_charts.build_spec(_plan(chart_type="radar", x="g", y=["a", "b"]), profile, rows)
    assert spec.chart_type == "bar" and "radar" in warning


@pytest.mark.unit
def test_read_csv_truncates_beyond_the_row_cap() -> None:
    body = io.StringIO()
    writer = csv.writer(body)
    writer.writerow(["x", "y"])
    for i in range(9):
        writer.writerow([i, i * 2])

    header, rows, _, total, truncated, warning = csv_charts.parse_csv(body.getvalue(), max_rows=5)
    assert len(rows) == 5 and total == 9 and truncated
    assert "前 5 行" in warning
    profile = csv_charts.profile_dataset("d", "big.csv", header, rows, ",", total, truncated, warning)
    assert profile.truncated and profile.total_rows == 9


# ==================================================================== Test 3：翻译
@pytest.mark.unit
def test_paragraphs_are_split_and_numbered_stably() -> None:
    assert translate_api.split_paragraphs("第一段。\n\n第二段。\n\n\n第三段。") == ["第一段。", "第二段。", "第三段。"]
    # 没有空行的"一行一段"文本同样要切得开，否则左右两栏会变成一整块对一整块
    assert translate_api.split_paragraphs("a\nb\nc") == ["a", "b", "c"]
    assert translate_api.split_paragraphs("   ") == []


@pytest.mark.unit
def test_glossary_parsing_accepts_the_common_formats() -> None:
    text = (
        "source,target\n"
        "注意力机制,attention mechanism\n"
        "稀疏路由 → sparse routing\n"
        "# 注释行\n"
        "MoE = 混合专家\n"
        "只有一列\n"
    )
    entries, skipped = translate_api.parse_glossary(text)
    pairs = {e.source: e.target for e in entries}
    # 表头行被认出来剔掉，不是当成"source→target"这种词条
    assert "source" not in pairs and "target" not in pairs
    assert pairs["注意力机制"] == "attention mechanism"
    assert pairs["稀疏路由"] == "sparse routing"
    assert pairs["MoE"] == "混合专家"
    assert skipped == ["只有一列"]


@pytest.mark.unit
def test_glossary_keeps_commas_inside_the_translation() -> None:
    entries, _ = translate_api.parse_glossary("卷积神经网络,convolutional neural network (CNN), 又称 CNN\n")
    # 只在**第一个**分隔符处切，右半边的逗号属于译名本身
    assert entries[0].target == "convolutional neural network (CNN), 又称 CNN"


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """把翻译用的 LLM 换成固定回复，并记下真正发给模型的 prompt。"""
    from app.agents.mcp import local_tools

    prompts: list[str] = []

    class FakeLLM:
        async def complete(self, *_a: Any, **__: Any) -> str:
            prompts.append(str(_a))
            return "[TRANSLATED]"

    monkeypatch.setattr(local_tools, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr("app.llm.client.usage_snapshot", lambda: None)
    monkeypatch.setattr("app.llm.client.usage_delta", lambda _base: {})
    return prompts


async def test_translate_endpoint_maps_paragraph_ids_and_reports_term_usage(fake_llm: list[str]) -> None:
    payload = TranslateParagraphsRequest(
        text="This paper proposes sparse routing.\n\n注意 [1] 中的公式 $E=mc^2$ 保持不变。",
        target="zh",
        glossary=[GlossaryEntry(source="sparse routing", target="稀疏路由")],
        passive=True,
    )
    res = await translate_api.translate_paragraphs(payload)

    assert res.code == 0
    data = res.data
    assert [p.id for p in data.paragraphs] == ["p1", "p2"]
    assert [p.index for p in data.paragraphs] == [0, 1]
    assert data.paragraphs[0].source.startswith("This paper")
    assert data.target_text.count("[TRANSLATED]") == 2
    assert data.unused_terms == []
    # 术语表与被动语态必须真的写进 prompt，而不是"设置了但没用"
    assert any("稀疏路由" in p for p in fake_llm), "术语表没进 prompt"
    assert any("被动语态" in p for p in fake_llm), "被动语态要求没进 prompt"


async def test_translate_reports_glossary_terms_that_never_appear(fake_llm: list[str]) -> None:
    """术语表配错（拼写、大小写）时 `unused_terms` 是唯一线索，不能静默。"""
    res = await translate_api.translate_paragraphs(
        TranslateParagraphsRequest(
            text="Attention is all you need.",
            glossary=[
                GlossaryEntry(source="attention", target="注意力"),
                GlossaryEntry(source="Routing", target="路由"),
            ],
        )
    )
    assert res.data.unused_terms == ["Routing"]


async def test_blocks_keep_their_own_ids_and_pdf_anchors(fake_llm: list[str]) -> None:
    """带 `blocks` 时 id/page/bbox 必须**原样**回传。

    这是 PDF 与译文左右联动唯一的地基：一旦这里被重新编号成 p1/p2，
    前端就会把译文挂到错误的 PDF 位置上，而且看起来一切正常（只是错位）。
    """
    res = await translate_api.translate_paragraphs(
        TranslateParagraphsRequest(
            blocks=[
                SourceBlock(id="chunk-12", text="First block.", page=3, bbox=[0.1, 0.2, 0.9, 0.25]),
                SourceBlock(id="chunk-13", text="Second block.", page=7, bbox=[0.1, 0.3, 0.9, 0.4]),
            ],
            glossary=[GlossaryEntry(source="First", target="第一")],
        )
    )
    assert [p.id for p in res.data.paragraphs] == ["chunk-12", "chunk-13"]
    assert [p.index for p in res.data.paragraphs] == [0, 1]
    assert [p.page for p in res.data.paragraphs] == [3, 7]
    assert res.data.paragraphs[0].bbox == [0.1, 0.2, 0.9, 0.25]
    assert res.data.target_text.count("[TRANSLATED]") == 2
    # blocks 走的是同一条 unused_terms 逻辑 —— 拼接后的原文里 "First" 确实出现过
    assert res.data.unused_terms == []


@pytest.mark.unit
def test_request_without_any_text_is_rejected() -> None:
    """`text` 与 `blocks` 都不给要报 422，而不是静默返回 0 段译文。"""
    with pytest.raises(ValidationError):
        TranslateParagraphsRequest(text="   ")


# ==================================================================== Test 4：端点接线
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.unit
def test_visualize_upload_then_generate_returns_a_png(client: Any, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(csv_charts, "csv_dir", lambda: tmp_path)

    async def fake_structured(_schema: Any, _messages: Any, **_kw: Any) -> Any:
        # 模型只负责选型；数据由后端从 CSV 现取
        return csv_charts._ChartPlan(
            chart_type="bar", x="region", y=["revenue"], title="各地区收入", caption="Figure 1: 各地区收入。"
        )

    monkeypatch.setattr(csv_charts, "complete_structured", fake_structured)

    up = client.post("/api/v1/visualize/upload", files={"file": ("sales.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    assert up.status_code == 200, up.text
    dataset_id = up.json()["data"]["dataset_id"]
    assert up.json()["data"]["numeric_columns"] == ["revenue", "cost", "users"]

    gen = client.post("/api/v1/visualize/generate", json={"dataset_id": dataset_id, "chart_type": "auto"})
    assert gen.status_code == 200, gen.text
    data = gen.json()["data"]
    assert data["chart_type"] == "bar"
    assert data["caption"].startswith("Figure 1: ")
    assert base64.b64decode(data["image"])[:4] == b"\x89PNG"
    assert data["renderer"] == "matplotlib"


@pytest.mark.unit
def test_generate_with_unknown_dataset_is_a_clean_400(client: Any) -> None:
    res = client.post("/api/v1/visualize/generate", json={"dataset_id": "../etc/passwd"})
    assert res.status_code == 400
    assert res.json()["code"] != 0


@pytest.mark.unit
def test_glossary_upload_endpoint(client: Any) -> None:
    res = client.post(
        "/api/v1/translate/glossary",
        files={"file": ("terms.csv", "注意力机制,attention\n".encode("utf-8"), "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["entries"] == [{"source": "注意力机制", "target": "attention"}]


@pytest.mark.unit
def test_planner_failure_still_draws_a_chart(client: Any, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """选型模型挂了也要出图（退到规则选型）。一张说明白的图 > 一个 500。"""
    monkeypatch.setattr(csv_charts, "csv_dir", lambda: tmp_path)

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(csv_charts, "complete_structured", boom)

    up = client.post("/api/v1/visualize/upload", files={"file": ("s.csv", CSV_TEXT.encode("utf-8"), "text/csv")})
    gen = client.post("/api/v1/visualize/generate", json={"dataset_id": up.json()["data"]["dataset_id"]})
    assert gen.status_code == 200, gen.text
    data = gen.json()["data"]
    assert data["image"] and "选型模型调用失败" in data["warning"]
