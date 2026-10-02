"""阶段 7 验收：上传 / 解析流水线 / 语义去重。

逐条对照五条验收标准：
1. 上传 arXiv 论文 → 自动下载 + 解析（`TestArxivFetch`：工具落盘 + `%PDF` 守卫）
2. 双栏 PDF 正确提取段落顺序（`TestTwoColumnLayout`：真造一份双栏 PDF 再解析）
3. 公式正确转 LaTeX（`TestFormula`：符号 → 命令、上下标 → `^{}`/`_{}`）
4. 上传 v1 与 v7 → 同源不同版本（`TestDedupClassify`：版本判据必须**先于**相似度）
5. 两篇同一论文（不同来源）→ 去重合并（同上 + `resolve_duplicate`）

版面/公式/图表/OCR 都做成"块级纯函数"，所以这里的 PDF 用 PyMuPDF 现造 ——
真文件、真坐标、不依赖网络与模型权重。唯独嵌入模型与数据库换成桩：
加载 bge-m3 要 2GB 权重，单测不该有这个前置条件（那批用例标 `model`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ParseError
from app.parsers.chunker import chunk_document
from app.parsers.figure_parser import attach_images, caption_of, split_captions
from app.parsers.formula_parser import inline_math, is_formula_line, split_formulas, to_latex
from app.parsers.layout_parser import (
    build_heading_tree,
    detect_columns,
    join_lines,
    merge_paragraphs,
    order_blocks,
)
from app.parsers.ocr_parser import missing_text_pages, needs_ocr, ocr_document, text_layer_ratio
from app.parsers.pdf import Block, PdfPage, parse_pdf
from app.parsers.semantic_dedup import (
    DUPLICATE,
    NEW_VERSION,
    canonical_version,
    classify,
    normalize_arxiv_id,
    resolve_duplicate,
    semantic_fingerprint,
)

pytestmark = pytest.mark.unit


def _read_bytes(path: str) -> bytes:
    """落盘读回。抽成同步函数：用例是 async，直接读会被 ASYNC240 拦。"""
    return Path(path).read_bytes()


# ==================================================================== 造真 PDF
def _two_column_pdf(path: Path) -> Path:
    """一份双栏 PDF：跨栏大标题 + 左栏（正文/图注/公式）+ 右栏正文。

    用 insert_text 逐行放，坐标完全可控 —— 版面算法要的正是坐标，
    拿一份"现成论文"反而说不清期望值是怎么来的。
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # 美式 Letter
    page.insert_text((50, 80), "A Two Column Paper", fontsize=18)
    left = [
        "This is the first left column line",
        "and it continues without a period",
        "so the merger should join them together",
        "Figure 1: Overview of the proposed framework.",
        "E = mc²",  # 上标在 Latin-1 里（Base14 字体能直接渲），α/≤ 不在
        "Table 2. Results on the benchmark.",
    ]
    for i, line in enumerate(left):
        page.insert_text((50, 140 + i * 14), line, fontsize=10)
    for i in range(6):
        page.insert_text((330, 140 + i * 14), f"right column line {i}", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


# ==================================================================== 2. 版面
class TestTwoColumnLayout:
    def test_reading_order_puts_the_whole_left_column_before_the_right(self, tmp_path: Path):
        doc = parse_pdf(_two_column_pdf(tmp_path / "two-col.pdf"))
        text = doc.pages[0].text

        assert "left column" in text and "right column" in text
        # 左栏第 2 行必须排在右栏第 1 行之前 —— 这正是"内容流顺序"做不到的事
        assert text.index("first left column line") < text.index("right column line 0")
        assert text.index("merger should join") < text.index("right column line 0")
        assert text.index("right column line 4") < text.index("right column line 5")

    def test_detects_two_columns_from_block_centers(self):
        left = [Block("l", bbox=(0.05, 0.1 + i * 0.05, 0.45, 0.13 + i * 0.05)) for i in range(4)]
        right = [Block("r", bbox=(0.55, 0.1 + i * 0.05, 0.95, 0.13 + i * 0.05)) for i in range(4)]
        assert detect_columns(left + right) == 2
        assert detect_columns(left) == 1  # 只有一栏的块 → 不敢判双栏

    def test_a_full_width_block_counts_as_a_spanner_not_a_column(self):
        """跨栏标题的 x 中心就在 0.5 上，把它算进任何一栏都会把判据带偏。"""
        title = Block("spanning title", bbox=(0.05, 0.02, 0.95, 0.06))
        left = [Block(f"l{i}", bbox=(0.05, 0.1 + i * 0.05, 0.45, 0.13 + i * 0.05)) for i in range(4)]
        right = [Block(f"r{i}", bbox=(0.55, 0.1 + i * 0.05, 0.95, 0.13 + i * 0.05)) for i in range(4)]

        assert detect_columns([title, *left, *right]) == 2
        ordered = order_blocks([title, *right, *left])  # 打乱输入，考的是重排
        assert ordered[0] is title, "整幅块必须排在最前（它在所有栏的上方）"
        assert [b.text for b in ordered[1:5]] == ["l0", "l1", "l2", "l3"]
        assert [b.text for b in ordered[5:]] == ["r0", "r1", "r2", "r3"]

    def test_blocks_without_coordinates_are_left_alone(self):
        """没坐标就别乱动顺序 —— 猜错顺序比不排更糟。"""
        blocks = [Block("b"), Block("a")]
        assert [b.text for b in order_blocks(blocks)] == ["b", "a"]

    def test_hyphenated_line_break_is_restored(self):
        assert join_lines("informa-", "tion is key") == "information is key"
        assert join_lines("中文行", "紧接着") == "中文行紧接着"
        assert join_lines("latin", "words") == "latin words"

    def test_merge_joins_lines_of_one_paragraph_but_not_across_columns(self):
        same = [
            Block("first half", bbox=(0.05, 0.10, 0.45, 0.115)),
            Block("second half", bbox=(0.05, 0.118, 0.45, 0.133)),
            Block("other column", bbox=(0.55, 0.10, 0.95, 0.115)),
        ]
        merged = merge_paragraphs(same)
        assert len(merged) == 2
        assert merged[0].text == "first half second half"
        assert merged[1].text == "other column"

    def test_heading_tree_ranks_by_font_size_and_caps_at_h3(self):
        # 正文块要给足：中位数是"正文字号"的估计，块太少会被标题本身带偏
        body = [
            Block("正文句子就这样排下去。", bbox=(0.05, 0.2 + i * 0.02, 0.9, 0.22 + i * 0.02), size=10.0)
            for i in range(6)
        ]
        heads = [
            Block("Title", bbox=(0.05, 0.05, 0.9, 0.09), size=18.0),
            Block("Section", bbox=(0.05, 0.12, 0.5, 0.145), size=13.0),
            Block("Sub", bbox=(0.05, 0.16, 0.5, 0.18), size=11.5),
        ]
        leveled = build_heading_tree([*heads, *body])
        assert [b.kind for b in leveled[:3]] == ["title", "title", "title"]
        assert [b.level for b in leveled[:3]] == [1, 2, 3]
        assert all(b.kind == "text" and b.level == 0 for b in leveled[3:])


# ==================================================================== 3. 公式
class TestFormula:
    @pytest.mark.parametrize(
        "line",
        ["E = mc²", r"\sum_{i=1}^{n} x_i = 1", "α ≥ 0.9", "f(x) = x^2 + 1", "$x$ = 1"],
    )
    def test_formula_lines_are_recognized(self, line: str):
        assert is_formula_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "Figure 1: Overview of the proposed framework.",
            "The model reaches 91.2% accuracy on the benchmark.",
            "3.2 Method and Model",  # 章节标题
            "α 与 β 的关系见下一节",  # 中文正文里出现希腊字母是常态
            "",
        ],
    )
    def test_ordinary_text_is_not_mistaken_for_a_formula(self, line: str):
        assert is_formula_line(line) is False

    def test_unicode_math_becomes_latex_commands(self):
        assert to_latex("α ≤ β") == r"\alpha \leq \beta"
        assert to_latex("∫ f dx") == r"\int f dx"

    def test_command_is_separated_from_a_following_letter(self):
        """`\\alphabeta` 会被 LaTeX 当成一个不存在的命令 —— 必须留白。"""
        out = to_latex("αx + βy")
        assert "\\alpha x" in out and "\\beta y" in out

    def test_superscripts_and_subscripts_are_grouped(self):
        assert to_latex("x²³") == "x^{23}"
        assert to_latex("x₁₂") == "x_{12}"
        assert to_latex("E = mc²") == "E = mc^{2}"

    def test_split_moves_display_formulas_out_of_the_text_flow(self):
        blocks = [
            Block("正文一句话。", bbox=(0.05, 0.1, 0.9, 0.12)),
            Block("E = mc²", bbox=(0.3, 0.2, 0.7, 0.23)),
        ]
        kept, formulas = split_formulas(blocks)
        assert [b.text for b in kept] == ["正文一句话。"]
        assert len(formulas) == 1
        assert formulas[0].kind == "formula"
        assert formulas[0].text == "$$E = mc^{2}$$"

    def test_math_font_wins_over_the_heuristic(self):
        """版式事实（字体是 CMMI）比符号密度更可信，不必再猜。"""
        blocks = [Block("x = y", kind="formula", bbox=(0.3, 0.2, 0.7, 0.23))]
        _kept, formulas = split_formulas(blocks)
        assert formulas and formulas[0].kind == "formula"

    def test_inline_math_is_wrapped_but_cjk_runs_are_left_alone(self):
        assert inline_math("当 α 变大时") == "当 $\\alpha$ 变大时"
        assert inline_math("其中α是系数") == "其中α是系数"  # 中文无词间空格，整段是一个片段
        assert inline_math("准确率 91.2%") == "准确率 91.2%"

    def test_inline_latex_can_be_turned_off(self):
        blocks = [Block("当 α 变大时", bbox=(0.05, 0.1, 0.9, 0.12))]
        kept, _ = split_formulas(blocks, inline_latex=False)
        assert kept[0].text == "当 α 变大时"


# ==================================================================== 图表
class TestFigureCaption:
    @pytest.mark.parametrize(
        "line",
        ["Figure 3: Overview", "Fig. 2 Results", "TABLE 1. Ablation", "图 3 整体框架", "表2：消融实验"],
    )
    def test_captions_are_recognized(self, line: str):
        assert caption_of(line) == " ".join(line.split())

    @pytest.mark.parametrize(
        "line",
        [
            "Table 1 shows that our method outperforms every baseline on all three datasets.",
            "图 1 展示了我们的方法。",  # 中文正文 + 句末标点
            "普通正文，什么标签都没有。",
        ],
    )
    def test_prose_is_not_a_caption(self, line: str):
        assert caption_of(line) is None

    def test_captions_leave_the_text_flow(self):
        blocks = [
            Block("正文。", bbox=(0.05, 0.1, 0.9, 0.12)),
            Block("Figure 1: Framework.", bbox=(0.05, 0.6, 0.45, 0.62)),
        ]
        kept, captions = split_captions(blocks)
        assert [b.text for b in kept] == ["正文。"]
        assert captions[0].kind == "figure_caption"

    def test_caption_is_attached_to_the_nearest_image(self):
        caption = Block("Figure 1: Framework.", page=1, bbox=(0.05, 0.60, 0.45, 0.62), kind="figure_caption")
        above = (0.05, 0.30, 0.45, 0.58)
        far_below = (0.55, 0.90, 0.95, 0.98)
        out = attach_images([caption], [far_below, above], 1)
        assert out[0].bbox == (0.05, 0.30, 0.45, 0.62)  # 图 + 图注的并集

    def test_no_image_in_range_keeps_the_caption_box(self):
        caption = Block("Figure 1: Framework.", page=1, bbox=(0.05, 0.60, 0.45, 0.62), kind="figure_caption")
        out = attach_images([caption], [(0.05, 0.01, 0.45, 0.05)], 1)
        assert out[0].bbox == (0.05, 0.60, 0.45, 0.62)


# ==================================================================== OCR
class TestOcr:
    def test_text_layer_ratio_and_needs_ocr(self):
        mixed = [PdfPage(number=1, text="有字"), PdfPage(number=2, text="")]
        assert text_layer_ratio(mixed) == 0.5
        assert needs_ocr(mixed, threshold=0.6) is True
        assert needs_ocr(mixed, threshold=0.4) is False
        assert text_layer_ratio([]) == 0.0  # 没有页 → 宁可去试 OCR
        assert missing_text_pages(mixed) == [2]

    def test_without_a_backend_pages_come_back_untouched(self, monkeypatch):
        """OCR 没装上不该是致命的 —— 降级链里的异常会让整篇论文判失败。"""
        import app.parsers.ocr_parser as ocr

        monkeypatch.setattr(ocr, "resolve_backend", lambda: None)
        pages = [PdfPage(number=1, text="")]
        assert ocr_document("whatever.pdf", pages) == pages

    def test_fake_backend_fills_the_page_and_keeps_normalized_boxes(self, monkeypatch):
        import app.parsers.ocr_parser as ocr

        monkeypatch.setattr(ocr, "resolve_backend", lambda: "paddleocr")
        monkeypatch.setattr(ocr, "_render_page", lambda path, number: (b"png", 612, 792))
        monkeypatch.setitem(
            ocr._BACKENDS,
            "paddleocr",
            lambda png: [("扫描出来的正文", [[0, 0], [122, 0], [122, 40], [0, 40]])],
        )

        pages = ocr_document("scanned.pdf", [PdfPage(number=1, text="")])
        assert pages[0].text == "扫描出来的正文"
        assert pages[0].blocks[0].bbox == (0.0, 0.0, 122 / 612, 40 / 792)

    def test_a_scanned_pdf_without_ocr_fails_with_a_readable_reason(self, tmp_path: Path):
        import fitz

        path = tmp_path / "blank.pdf"
        doc = fitz.open()
        doc.new_page(width=612, height=792)  # 一个字都没有 = 扫描件
        doc.save(str(path))
        doc.close()

        with pytest.raises(ParseError) as exc:
            parse_pdf(path)
        assert "文本层占比" in str(exc.value)


# ==================================================================== 端到端分块
class TestChunkingWithLayout:
    def test_chunks_carry_type_page_and_normalized_bbox(self, tmp_path: Path):
        doc = parse_pdf(_two_column_pdf(tmp_path / "two-col.pdf"))
        chunks = chunk_document(doc)

        kinds = {c.chunk_type for c in chunks}
        assert {"text", "formula", "figure_caption"} <= kinds

        formula = next(c for c in chunks if c.chunk_type == "formula")
        assert formula.content.startswith("$$") and "mc^{2}" in formula.content
        assert formula.page_start == 1

        caption = next(c for c in chunks if c.chunk_type == "figure_caption")
        assert caption.content.startswith("Figure 1:")

        for chunk in chunks:
            if chunk.bbox is None:
                continue
            assert all(0.0 <= v <= 1.0 for v in chunk.bbox), f"bbox 未归一化: {chunk.bbox}"

    def test_chunk_bbox_starts_at_the_left_column_margin(self, tmp_path: Path):
        chunks = chunk_document(parse_pdf(_two_column_pdf(tmp_path / "two-col.pdf")))
        assert [c.index for c in chunks] == list(range(len(chunks)))
        # 含"图注/公式"的 chunk 之外，正文 chunk 的左边界应当落在左栏页边（≈0.08）。
        # 不断言右边界：一个 chunk 会跨栏把左右两栏的段落拼在一起（按字符数聚合），
        # 右边界本来就会到右栏去。
        text_chunk = next(c for c in chunks if c.chunk_type == "text" and "left column" in c.content)
        assert text_chunk.bbox is not None
        assert text_chunk.bbox[0] <= 0.1
        assert all(0.0 <= v <= 1.0 for v in text_chunk.bbox)


# ==================================================================== 4/5. 去重
class TestDedupIdentity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://arxiv.org/abs/2401.12345v7", ("2401.12345", "v7")),
            ("arxiv.org/pdf/2401.12345v7.pdf", ("2401.12345", "v7")),
            ("arXiv:2401.12345", ("2401.12345", None)),
            ("2401.12345V3", ("2401.12345", "v3")),
            ("cs.CL/0701001v2", ("cs.CL/0701001", "v2")),
            ("", (None, None)),
            ("not-a-paper", (None, None)),
        ],
    )
    def test_arxiv_ref_is_normalized(self, raw: str, expected: tuple[str | None, str | None]):
        assert normalize_arxiv_id(raw) == expected

    def test_version_is_canonicalized(self):
        assert canonical_version(None) == "v1"
        assert canonical_version("7") == "v7"
        assert canonical_version("V07") == "v7"

    def test_fingerprint_is_stable_and_author_sensitive(self):
        def dense(texts: list[str]) -> list[list[float]]:
            return [[float(len(t)), 1.0] for t in texts]

        a = semantic_fingerprint("T", "A", [{"name": "Ada"}], dense_fn=dense)
        assert a == semantic_fingerprint("T", "A", [{"name": "Ada"}], dense_fn=dense)
        # 同标题同摘要、不同作者 → 必须是两个指纹
        assert a != semantic_fingerprint("T", "A", [{"name": "Bob"}], dense_fn=dense)

    def test_fingerprint_falls_back_to_text_without_an_embedder(self):
        first = semantic_fingerprint("T", "A", "Ada")
        assert first == semantic_fingerprint("T", "A", "Ada")
        assert first != semantic_fingerprint("T", "A", "Bob")


class TestDedupClassify:
    def test_empty_library_is_distinct(self):
        assert classify({"paper_id": 1, "arxiv_id": "2401.1", "version": "v1"}, []).kind not in {
            DUPLICATE,
            NEW_VERSION,
        }

    def test_same_arxiv_and_same_version_is_a_duplicate(self):
        verdict = classify(
            {"paper_id": 2, "arxiv_id": "2401.12345", "version": "v7"},
            [{"paper_id": 1, "arxiv_id": "2401.12345v7", "version": "v7", "score": 0.99}],
        )
        assert verdict.kind == DUPLICATE and verdict.canonical_paper_id == 1

    def test_v1_and_v7_are_the_same_paper_in_two_versions(self):
        """★ 验收 4：相似度 1.0 也必须只记版本谱系，**不能**合并。"""
        verdict = classify(
            {"paper_id": 2, "arxiv_id": "2401.12345", "version": "v7"},
            [{"paper_id": 1, "arxiv_id": "2401.12345", "version": "v1", "score": 1.0}],
        )
        assert verdict.kind == NEW_VERSION
        assert verdict.canonical_paper_id == 1

    def test_version_rule_beats_the_similarity_rule(self):
        """同时命中"同编号不同版本"与"跨库高相似"时，必须以版本为准。"""
        verdict = classify(
            {"paper_id": 3, "arxiv_id": "2401.12345", "version": "v7"},
            [
                {"paper_id": 2, "arxiv_id": None, "version": None, "score": 1.0},
                {"paper_id": 1, "arxiv_id": "2401.12345", "version": "v1", "score": 0.97},
            ],
        )
        assert verdict.kind == NEW_VERSION and verdict.canonical_paper_id == 1

    def test_cross_library_duplicate_by_similarity(self):
        """★ 验收 5：没有可比编号，靠摘要向量 ≥ 0.95 判合并。"""
        verdict = classify(
            {"paper_id": 2, "arxiv_id": None, "version": None},
            [{"paper_id": 1, "arxiv_id": None, "version": None, "score": 0.96}],
        )
        assert verdict.kind == DUPLICATE and verdict.canonical_paper_id == 1

    def test_similar_but_distinct_stays_distinct(self):
        verdict = classify(
            {"paper_id": 2, "arxiv_id": None, "version": None},
            [{"paper_id": 1, "arxiv_id": None, "version": None, "score": 0.93}],
        )
        assert verdict.kind == "distinct" and verdict.canonical_paper_id is None

    def test_a_hit_on_itself_is_ignored(self):
        """重新索引时自己的向量还在库里 —— 不能把自己判成自己的重复。"""
        verdict = classify(
            {"paper_id": 7, "arxiv_id": None, "version": None},
            [{"paper_id": 7, "arxiv_id": None, "version": None, "score": 1.0}],
        )
        assert verdict.kind == "distinct"

    def test_threshold_is_configurable(self):
        hits = [{"paper_id": 1, "arxiv_id": None, "version": None, "score": 0.80}]
        args = {"paper_id": 2, "arxiv_id": None, "version": None}
        assert classify(args, hits).kind == "distinct"
        assert classify(args, hits, threshold=0.75).kind == DUPLICATE

    def test_resolve_duplicate_accepts_injected_hits(self):
        verdict = resolve_duplicate(
            2,
            None,
            arxiv_id="2401.12345",
            version="v7",
            hits=[{"paper_id": 1, "arxiv_id": "2401.12345", "version": "v1", "score": 1.0}],
        )
        assert verdict.kind == NEW_VERSION

    def test_resolve_duplicate_without_a_vector_does_not_guess(self):
        verdict = resolve_duplicate(2, None)
        assert verdict.kind == "distinct" and "没有摘要向量" in verdict.reason


# ==================================================================== 1. arXiv 取件
class TestArxivFetch:
    def test_ref_parsing_accepts_links_and_bare_ids(self):
        from app.mcp_servers.arxiv_server import parse_arxiv_ref

        assert parse_arxiv_ref("https://arxiv.org/abs/2401.12345v7") == ("2401.12345", "v7")
        assert parse_arxiv_ref("2401.12345") == ("2401.12345", "v1")  # 没写版本 → 首版
        with pytest.raises(ValueError):
            parse_arxiv_ref("https://example.com/paper.pdf")

    async def test_fetch_downloads_metadata_and_pdf(self, monkeypatch, tmp_path: Path):
        import app.mcp_servers.arxiv_server as arxiv

        feed = (
            "<feed><entry><id>http://arxiv.org/abs/2401.12345v7</id>"
            "<title>Mixture of Experts Routing</title><summary>We study routing.</summary>"
            "<published>2024-01-22T18:00:00Z</published>"
            "<author><name>Ada Lovelace</name></author><category term='cs.LG'/></entry></feed>"
        )

        async def fake_get_text(url, **kw):
            return feed

        async def fake_get_bytes(url, **kw):
            assert url.endswith("/2401.12345v7")
            assert kw["max_bytes"] > 0, "体积极限必须传下去，否则坏链会把内存吃光"
            return b"%PDF-1.7 fake", "application/pdf"

        monkeypatch.setattr(arxiv, "get_text", fake_get_text)
        monkeypatch.setattr(arxiv, "get_bytes", fake_get_bytes)

        out = await arxiv.arxiv_fetch("2401.12345v7", dest_dir=str(tmp_path))
        assert out["arxiv_id"] == "2401.12345" and out["version"] == "v7"
        assert out["authors"] == ["Ada Lovelace"]
        assert _read_bytes(out["pdf_path"]).startswith(b"%PDF")
        assert out["sha256"] and len(out["sha256"]) == 64

    async def test_html_error_page_is_rejected_before_it_becomes_a_broken_pdf(self, monkeypatch, tmp_path: Path):
        """arXiv 对不存在的编号返回 HTML（HTTP 200）—— 落成 .pdf 后报错位置离病因很远。"""
        import app.mcp_servers.arxiv_server as arxiv

        async def fake_get_text(url, **kw):
            return "<feed></feed>"

        async def fake_get_bytes(url, **kw):
            return b"<!DOCTYPE html><html>not found</html>", "text/html"

        monkeypatch.setattr(arxiv, "get_text", fake_get_text)
        monkeypatch.setattr(arxiv, "get_bytes", fake_get_bytes)

        with pytest.raises(ValueError, match="未返回 PDF"):
            await arxiv.arxiv_fetch("2401.99999", dest_dir=str(tmp_path))


# ==================================================================== API 边界
class TestUploadApiGuards:
    """只验不需要数据库的分支（连不上库时的行为已被 test_api_smoke 覆盖）。"""

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_upload_without_file_or_arxiv_url_is_a_400(self, client):
        res = client.post("/api/v1/papers/upload", data={"title": "x"})
        assert res.status_code == 400
        assert "arxiv_url" in res.json()["message"]

    def test_batch_upload_rejects_an_empty_list(self, client):
        res = client.post("/api/v1/papers/batch-upload", data={})
        assert res.status_code in {400, 422}

    def test_arxiv_url_is_validated_before_any_download(self, client):
        """链接不是 arXiv 就该在下载之前拒掉 —— 别等外网往返一趟才发现。"""
        res = client.post("/api/v1/papers/upload", data={"arxiv_url": "https://example.com/x"})
        assert res.status_code == 400
        assert "arXiv" in res.json()["message"]
