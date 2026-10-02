"""5 个 MCP Server 的独立单测 —— 对照验收标准 1（各自有单测、mock 外部 API）与 4（沙箱逃逸）。

外部 API 一律 monkeypatch 掉 `_common.get_json` / `get_text` 或 Server 内的发送函数：
单测不发网络请求，也不依赖任何 key。唯一真的起子进程的是 python_exec —— 它本身就是
被测对象，mock 掉就没得测了。
"""

from __future__ import annotations

import os

import pytest

# ==================================================================== arXiv
_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query Results</title>
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <published>2024-01-22T18:00:00Z</published>
    <title>Mixture of Experts  Routing</title>
    <summary>We study &lt;b&gt;routing&lt;/b&gt; in MoE.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.99999v1</id>
    <published>2024-02-01T00:00:00Z</published>
    <title>Second Paper</title>
    <summary>Another abstract.</summary>
    <author><name>Grace Hopper</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>"""


class TestArxivServer:
    @pytest.mark.unit
    async def test_search_parses_atom_feed_into_structured_results(self, monkeypatch):
        import app.mcp_servers.arxiv_server as m

        seen: dict[str, object] = {}

        async def fake_get_text(url: str, **kw: object) -> str:
            seen["url"] = url
            seen["params"] = kw.get("params")
            return _ARXIV_XML

        monkeypatch.setattr(m, "get_text", fake_get_text)
        out = await m.arxiv_search('ti:"mixture of experts"', max_results=3)

        assert out["total"] == 2
        first = out["results"][0]
        assert first["arxiv_id"] == "2401.12345v2"  # 带版本号，不能只剩数字
        assert first["title"] == "Mixture of Experts Routing"  # 连续空白被折叠
        assert first["authors"] == ["Ada Lovelace", "Alan Turing"]
        assert first["categories"] == ["cs.LG"]
        assert first["published"] == "2024-01-22"
        assert first["url"] == "https://arxiv.org/abs/2401.12345v2"
        assert "<b>" not in first["abstract"] and "routing" in first["abstract"]  # 去 HTML 标签

        assert str(seen["url"]).startswith("https://export.arxiv.org/api/query")
        assert seen["params"]["max_results"] == 3  # type: ignore[index]

    @pytest.mark.unit
    async def test_out_of_range_args_are_clamped_not_rejected(self, monkeypatch):
        """模型给 999 条不该报错 —— 夹到上限，工具仍要能用。"""
        import app.mcp_servers.arxiv_server as m

        seen: dict[str, object] = {}

        async def fake_get_text(url: str, **kw: object) -> str:
            seen["params"] = kw.get("params")
            return "<feed></feed>"

        monkeypatch.setattr(m, "get_text", fake_get_text)
        out = await m.arxiv_search("x", max_results=999, sort_by="nonsense")

        assert out["total"] == 0
        assert seen["params"]["max_results"] == 50  # type: ignore[index]
        assert seen["params"]["sortBy"] == "relevance"  # type: ignore[index]


# ==================================================================== PubMed
_ESEARCH_JSON = {"esearchresult": {"idlist": ["111", "222"]}}
_ESUMMARY_JSON = {
    "result": {
        "111": {
            "title": "CRISPR in the clinic",
            "authors": [{"name": "Zhang San"}],
            "fulljournalname": "Nature Medicine",
            "pubdate": "2023 May",
        },
        "222": {"title": "No author paper"},
    }
}


class TestPubmedServer:
    @pytest.mark.unit
    async def test_search_chains_esearch_then_esummary(self, monkeypatch):
        import app.mcp_servers.pubmed_server as m

        urls: list[str] = []

        async def fake_get_json(url: str, **kw: object) -> object:
            urls.append(url)
            return _ESEARCH_JSON if url.endswith("esearch.fcgi") else _ESUMMARY_JSON

        monkeypatch.setattr(m, "get_json", fake_get_json)
        out = await m.pubmed_search("CRISPR", max_results=2)

        assert len(urls) == 2, "先 esearch 拿 id，再 esummary 拿详情"
        assert out["total"] == 2
        assert out["results"][0]["pmid"] == "111"
        assert out["results"][0]["authors"] == ["Zhang San"]
        assert out["results"][0]["journal"] == "Nature Medicine"
        assert out["results"][0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/111/"
        # 缺字段的记录不能炸，要给出空串
        assert out["results"][1]["authors"] == []
        assert out["results"][1]["journal"] == ""

    @pytest.mark.unit
    async def test_empty_id_list_short_circuits(self, monkeypatch):
        """没搜到就不该再发第二次请求 —— 省一次外网往返。"""
        import app.mcp_servers.pubmed_server as m

        calls: list[str] = []

        async def fake_get_json(url: str, **kw: object) -> object:
            calls.append(url)
            return {"esearchresult": {"idlist": []}}

        monkeypatch.setattr(m, "get_json", fake_get_json)
        out = await m.pubmed_search("nothing-matches")

        assert out == {"total": 0, "query": "nothing-matches", "results": []}
        assert len(calls) == 1

    @pytest.mark.unit
    async def test_year_from_is_pushed_into_the_query(self, monkeypatch):
        import app.mcp_servers.pubmed_server as m

        seen: dict[str, object] = {}

        async def fake_get_json(url: str, **kw: object) -> object:
            seen["params"] = kw.get("params")
            return {"esearchresult": {"idlist": []}}

        monkeypatch.setattr(m, "get_json", fake_get_json)
        await m.pubmed_search("CRISPR", year_from=2020)

        assert "2020:3000[dp]" in seen["params"]["term"]  # type: ignore[index]


# ============================================================ Semantic Scholar
_S2_JSON = {
    "data": [
        {"paperId": "low", "title": "Low impact", "citationCount": 3, "year": 2019, "authors": []},
        {
            "paperId": "high",
            "title": "Foundational work",
            "citationCount": 99,
            "influentialCitationCount": 12,
            "year": 2017,
            "venue": "ICML",
            "authors": [{"name": "Y. Bengio"}],
            "abstract": "A" * 2000,
            "openAccessPdf": {"url": "https://ex.org/p.pdf"},
            "url": "https://s2.org/high",
        },
    ]
}


class TestSemanticScholarServer:
    @pytest.mark.unit
    async def test_results_sorted_by_citation_count(self, monkeypatch):
        import app.mcp_servers.semantic_scholar_server as m

        async def fake_get_json(url: str, **kw: object) -> object:
            return _S2_JSON

        monkeypatch.setattr(m, "get_json", fake_get_json)
        out = await m.semantic_scholar_search("deep learning")

        assert [r["paper_id"] for r in out["results"]] == ["high", "low"], "引用数高的必须排前面"
        top = out["results"][0]
        assert top["citation_count"] == 99
        assert top["influential_citation_count"] == 12
        assert top["pdf_url"] == "https://ex.org/p.pdf"
        assert len(top["abstract"]) == 1500, "摘要要截断，别把整个 context 撑爆"

    @pytest.mark.unit
    async def test_min_citations_filters(self, monkeypatch):
        import app.mcp_servers.semantic_scholar_server as m

        async def fake_get_json(url: str, **kw: object) -> object:
            return _S2_JSON

        monkeypatch.setattr(m, "get_json", fake_get_json)
        out = await m.semantic_scholar_search("deep learning", min_citations=10)

        assert out["total"] == 1
        assert out["results"][0]["paper_id"] == "high"


# ==================================================================== Web Search
class TestWebSearchServer:
    @pytest.mark.unit
    async def test_missing_key_reports_configuration_not_zero_results(self, monkeypatch):
        """没配 key 必须说"没配"，不能返回空结果让模型以为"网上没有"。"""
        import app.mcp_servers.web_search_server as m

        monkeypatch.setattr(m.settings, "TAVILY_API_KEY", "")
        out = await m.web_search("anything")

        assert out["total"] == 0 and out["results"] == []
        assert "TAVILY_API_KEY" in out["note"]

    @pytest.mark.unit
    async def test_results_mapped_to_stable_shape(self, monkeypatch):
        import app.mcp_servers.web_search_server as m

        monkeypatch.setattr(m.settings, "TAVILY_API_KEY", "tvly-test-key")
        seen: dict[str, object] = {}

        async def fake_post(query: str, max_results: int, topic: str) -> dict[str, object]:
            seen["args"] = (query, max_results, topic)
            return {"results": [{"title": "T", "url": "https://x", "content": "C", "score": 0.9}]}

        monkeypatch.setattr(m, "_post", fake_post)
        out = await m.web_search("latest", max_results=99, topic="news")

        assert seen["args"] == ("latest", 10, "news")  # 99 夹到 10
        assert out["results"] == [{"title": "T", "url": "https://x", "snippet": "C", "score": 0.9}]
        assert "外部来源" in out["note"]


# ============================================================== Python 沙箱
class TestPythonExecSandbox:
    @pytest.mark.unit
    async def test_pure_computation_works(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("import math\nprint(round(math.pi, 4))")
        assert out["ok"] is True
        assert out["stdout"].strip() == "3.1416"
        assert out["returncode"] == 0

    @pytest.mark.unit
    async def test_runtime_error_is_reported_not_raised(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("print(1/0)")
        assert out["ok"] is False
        assert "ZeroDivisionError" in out["stderr"]

    @pytest.mark.unit
    async def test_syntax_error_is_rejected_by_the_guard(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("1 +")
        assert out["ok"] is False and out["stderr"].startswith("拒绝执行：语法错误")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("code", "needle"),
        [
            # ---- 导入白名单：进程 / 网络 / 文件 / 反射一律不给
            ("import os\nprint(os.getcwd())", "禁止导入 `os`"),
            ("import subprocess", "禁止导入 `subprocess`"),
            ("import socket", "禁止导入 `socket`"),
            ("import ctypes", "禁止导入 `ctypes`"),
            ("from pathlib import Path", "禁止从 `pathlib` 导入"),
            # ---- 动态导入 / 求值。注意第三条：老的正则黑名单挡不住字符串拼接，
            #      AST 看的是结构，所以 `'o'+'s'` 这种也照样撞在 `__import__` 上。
            ("__import__('os').system('dir')", "禁止使用 `__import__`"),
            ("eval('1+1')", "禁止使用 `eval`"),
            ("__import__('o' + 's').system('dir')", "禁止使用 `__import__`"),
            # ---- 文件与反射
            ("open('/etc/passwd').read()", "禁止使用 `open`"),
            ("getattr(object, '__subclasses__')()", "禁止使用 `getattr`"),
            # ---- 逃逸经典：顺着 dunder 往上爬
            ("print(().__class__.__bases__)", "禁止访问 `.__bases__`"),
            ("print('__subclasses__')", "禁止出现魔术属性名字符串"),
        ],
    )
    async def test_escape_attempts_are_refused(self, code: str, needle: str):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec(code)
        assert out["ok"] is False, f"{code!r} 竟然被执行了"
        assert out["stderr"].startswith("拒绝执行")
        assert needle in out["stderr"], out["stderr"]
        assert out["stdout"] == ""

    @pytest.mark.unit
    async def test_guard_allows_whitelisted_stdlib_and_dunder_name(self):
        """守卫不能把正常计算代码一并误杀。"""
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec(
            "import json, statistics\nprint(json.dumps(statistics.mean([1, 2, 3])))\nprint(__name__)\n"
        )
        assert out["ok"] is True
        assert out["stdout"].split() == ["2", "__main__"]

    @pytest.mark.unit
    async def test_child_env_has_no_secrets(self, monkeypatch):
        """子进程环境是从白名单重建的 —— 任何 API key 都不能漏进去。"""
        import app.mcp_servers.python_exec_server as m

        monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-me")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-leak-me")
        env = m.child_env()

        assert "OPENAI_API_KEY" not in env
        assert "TAVILY_API_KEY" not in env
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert set(env) <= set(m._ENV_ALLOW) | {"PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"}

    @pytest.mark.unit
    async def test_infinite_loop_hits_the_timeout(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("while True:\n    pass", timeout=1)
        assert out["ok"] is False
        assert out["timeout"] is True
        assert "超时" in out["stderr"]

    @pytest.mark.unit
    async def test_empty_code_is_rejected(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("   \n")
        assert out["ok"] is False and out["stderr"] == "代码为空"

    @pytest.mark.unit
    @pytest.mark.skipif(os.name != "posix", reason="Windows 无 RLIMIT_AS（内存上限要靠容器）")
    async def test_memory_cap_is_enforced(self):
        import app.mcp_servers.python_exec_server as m

        out = await m.python_exec("x = bytearray(900 * 1024 * 1024)\nprint(len(x))", timeout=30)
        assert out["ok"] is False
        assert "MemoryError" in out["stderr"]


# ======================================================== 五个 Server 的一致性契约
@pytest.mark.unit
def test_every_server_module_exposes_a_fastmcp_named_after_its_registry_key():
    """注册表里的 key、模块里的 FastMCP 名、模块路径三者必须对得上。

    错位的表现是"工具装载完成但一个都没有"，而日志看起来一切正常。
    """
    import importlib

    from app.mcp_servers import SERVER_MODULES

    assert set(SERVER_MODULES) == {"arxiv", "pubmed", "semantic-scholar", "python-exec", "web-search"}
    for key, module_path in SERVER_MODULES.items():
        module = importlib.import_module(module_path)
        assert module.mcp.name == key, f"{module_path} 的 FastMCP 名应为 {key}"
        assert callable(module.mcp.run)
