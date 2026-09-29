"""MinerU 适配器：纯函数 + 假的 CLI 进程。

不 shell out 真 MinerU —— 它要几 GB 模型权重，单测里跑不起来也没必要。
真正要守住的是**输出解析**：content_list.json 各版本目录层级不同、块类型不同，
折回本项目 `PdfPage` 的那段逻辑才是会出错的地方。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.parsers import mineru

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ 纯函数
def test_pages_from_blocks_groups_by_page_and_converts_to_1_based():
    blocks = [
        {"type": "title", "text": "Abstract", "page_idx": 0},
        {"type": "text", "text": "正文第一段。", "page_idx": 0},
        {"type": "text", "text": "第二页正文。", "page_idx": 1},
    ]
    pages = mineru.pages_from_blocks(blocks)
    assert [p.number for p in pages] == [1, 2]
    assert pages[0].text == "Abstract\n正文第一段。"
    assert pages[1].text == "第二页正文。"


def test_pages_from_blocks_keeps_missing_page_idx_on_page_1():
    """没有 page_idx 的块不能丢，否则正文会凭空少一段。"""
    pages = mineru.pages_from_blocks([{"type": "text", "text": "无页码的块"}])
    assert pages == [mineru.PdfPage(number=1, text="无页码的块")]


def test_pages_from_blocks_flattens_table_html_to_text():
    blocks = [
        {
            "type": "table",
            "table_body": "<table><tr><td>方法</td><td>91.2</td></tr></table>",
            "page_idx": 2,
        }
    ]
    text = mineru.pages_from_blocks(blocks)[0].text
    assert "<" not in text and ">" not in text
    assert "方法" in text and "91.2" in text


def test_pages_from_blocks_keeps_image_caption_drops_bare_image():
    """图自身没字，但图注有字 —— 图注要留下来，纯图片块不要。"""
    blocks = [
        {"type": "image", "img_caption": ["图 1：整体框架"], "page_idx": 0},
        {"type": "image", "page_idx": 0},
        {"type": "text", "text": "正文。", "page_idx": 0},
    ]
    assert mineru.pages_from_blocks(blocks)[0].text == "图 1：整体框架\n正文。"


def test_pages_from_blocks_rejects_empty_blocks():
    with pytest.raises(RuntimeError, match="没有任何可用文本块"):
        mineru.pages_from_blocks([{"type": "image"}, {"type": "text", "text": "   "}])


def test_block_text_unescapes_html_entities():
    text = mineru._block_text({"type": "table", "table_body": "<td>R&amp;D</td>"})
    assert text == "R&D"


# ------------------------------------------------------------------ resolve_cmd
def test_resolve_cmd_accepts_absolute_path(tmp_path: Path):
    exe = tmp_path / "mineru.exe"
    exe.write_text("", encoding="utf-8")
    assert mineru.resolve_cmd(str(exe)) == str(exe)


def test_resolve_cmd_returns_none_for_missing_command():
    assert mineru.resolve_cmd("definitely-not-a-real-binary-xyz") is None


# ------------------------------------------------------------------ 假 CLI 端到端
@pytest.fixture
def fake_cli(monkeypatch, tmp_path: Path, settings):
    """把 subprocess.run 换掉：按 mineru 的目录结构产出 content_list.json。

    `-o` 后面那层 `<stem>/auto/` 是刻意加的 —— 真 MinerU 就是多这一层，
    写死路径的实现在这里会失败。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLED", True)
    monkeypatch.setattr(settings, "MINERU_CMD", "mineru")
    monkeypatch.setattr(mineru, "resolve_cmd", lambda cmd=None: "mineru")

    seen: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        out = Path(argv[argv.index("-o") + 1]) / "paper" / "auto"
        out.mkdir(parents=True, exist_ok=True)
        (out / "paper_content_list.json").write_text(
            json.dumps(
                [
                    {"type": "title", "text": "Methods", "page_idx": 0},
                    {"type": "text", "text": "我们提出了一种方法。", "page_idx": 1},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mineru.subprocess, "run", fake_run)
    return seen


def test_parse_with_mineru_reads_nested_output(fake_cli, tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    pages, meta, title = mineru.parse_with_mineru(pdf)

    assert [p.number for p in pages] == [1, 2]
    assert title == ""
    assert meta["mineru_method"] == "auto"
    argv = fake_cli["argv"]
    assert argv[:4] == ["mineru", "-p", str(pdf), "-o"]
    assert "-m" in argv and "-d" in argv


def test_parse_with_mineru_raises_when_cli_missing(monkeypatch, settings, tmp_path: Path):
    monkeypatch.setattr(settings, "MINERU_ENABLED", True)
    monkeypatch.setattr(mineru, "resolve_cmd", lambda cmd=None: None)
    with pytest.raises(ImportError, match="找不到 mineru"):
        mineru.parse_with_mineru(tmp_path / "x.pdf")


def test_parse_with_mineru_disabled_is_import_error(monkeypatch, settings, tmp_path: Path):
    """走 ImportError 是为了让降级链的"未安装"分支一并接住它。"""
    monkeypatch.setattr(settings, "MINERU_ENABLED", False)
    with pytest.raises(ImportError, match="MINERU_ENABLED=false"):
        mineru.parse_with_mineru(tmp_path / "x.pdf")


def test_parse_with_mineru_reports_nonzero_exit(monkeypatch, settings, tmp_path: Path):
    monkeypatch.setattr(settings, "MINERU_ENABLED", True)
    monkeypatch.setattr(mineru, "resolve_cmd", lambda cmd=None: "mineru")
    monkeypatch.setattr(
        mineru.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 2, stdout="", stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="退出码 2"):
        mineru.parse_with_mineru(tmp_path / "x.pdf")


def test_parse_with_mineru_falls_back_to_markdown(monkeypatch, settings, tmp_path: Path):
    """拿不到 content_list 时退化为整篇 markdown：能检索，页码丢弃但要有日志。"""
    monkeypatch.setattr(settings, "MINERU_ENABLED", True)
    monkeypatch.setattr(mineru, "resolve_cmd", lambda cmd=None: "mineru")

    def fake_run(argv, **kwargs):
        out = Path(argv[argv.index("-o") + 1]) / "paper" / "auto"
        out.mkdir(parents=True, exist_ok=True)
        (out / "paper.md").write_text("# 标题\n\n正文内容。", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mineru.subprocess, "run", fake_run)
    pages = mineru.parse_with_mineru(tmp_path / "x.pdf")[0]
    assert len(pages) == 1 and "正文内容" in pages[0].text


# ------------------------------------------------------------------ 降级链顺序
def test_parse_pdf_puts_mineru_first_in_chain():
    from app.parsers.pdf import _BACKENDS

    assert [name for name, _ in _BACKENDS] == ["mineru", "pymupdf", "pdfplumber", "pypdf"]
