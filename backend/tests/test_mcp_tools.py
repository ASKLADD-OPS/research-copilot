"""MCP 工具链路单测 —— 对照验收标准 2 / 3 / 5 / 6。

- 2：`load_all_mcp_tools()` 返回全部工具且格式正确（BaseTool + 参数 schema + 真能调通）
- 3：工具描述足以让模型选对工具（判别力 + 两两不相似）
- 5：工具失败 → Executor 步骤 failed → 交到 Replanner 重规划
- 6：fastapi-mcp 暴露的端点在 MCP 客户端里可见

验收 1 / 4 在 `test_mcp_servers.py`。这里**只**用已经装好的真实工具，
不 mock 工具本身 —— mock 掉就测不出"描述写得好不好"了。
"""

from __future__ import annotations

from typing import Any

import pytest

# ================================================================ 公共夹具
ALL_FLAGS = (
    "MCP_ARXIV_ENABLED",
    "MCP_PUBMED_ENABLED",
    "MCP_SEMANTIC_SCHOLAR_ENABLED",
    "MCP_PYTHON_EXEC_ENABLED",
    "MCP_WEB_SEARCH_ENABLED",
)


@pytest.fixture
def mcp_client(monkeypatch):
    """把所有 Server 打开，并把全局单例重置掉。

    conftest 默认把所有 Server 关掉（单测不该起子进程 / 发外网），所以想测装载
    就必须在这里显式打开，否则 `load_all_mcp_tools()` 永远返回空列表。
    """
    from app.agents.mcp import client as c

    for flag in ALL_FLAGS:
        monkeypatch.setattr(c.settings, flag, True)
    monkeypatch.setattr(c, "_toolbox", None)
    return c


# ==================================================== 验收 2：装载全部工具
class TestLoadAllTools:
    @pytest.mark.unit
    async def test_returns_every_tool_from_every_server(self, mcp_client):
        tools = await mcp_client.load_all_mcp_tools()

        assert {t.name for t in tools} == {
            "arxiv_search",
            "pubmed_search",
            "semantic_scholar_search",
            "python_exec",
            "web_search",
        }

    @pytest.mark.unit
    async def test_every_tool_is_a_well_formed_langchain_tool(self, mcp_client):
        from langchain_core.tools import BaseTool

        for tool in await mcp_client.load_all_mcp_tools():
            assert isinstance(tool, BaseTool), f"{tool} 不是 BaseTool，喂给 create_react_agent 会炸"
            assert tool.name.isidentifier(), f"工具名 {tool.name!r} 不是合法标识符"
            assert tool.description.strip(), f"{tool.name} 没有描述，模型只能靠名字猜"
            schema = tool.args_schema.model_json_schema()
            props = schema.get("properties") or {}
            assert props, f"{tool.name} 的参数 schema 是空的，模型没法构造 Action Input"
            assert "code" in props if tool.name == "python_exec" else "query" in props

    @pytest.mark.unit
    async def test_loaded_tool_actually_executes_through_the_mcp_layer(self, mcp_client):
        """格式对还不够 —— 通过 MCP 层真调一次，确认拿到的不是空壳。"""
        tools = await mcp_client.load_all_mcp_tools()
        toolbox = mcp_client.get_toolbox()
        assert toolbox.get("python_exec") is not None

        out = await toolbox.ainvoke("python_exec", {"code": "print(2 ** 10)"})
        assert out["ok"] is True and out["stdout"].strip() == "1024"
        assert len(tools) == 5

    @pytest.mark.unit
    async def test_loading_is_cached(self, mcp_client):
        first = await mcp_client.load_all_mcp_tools()
        second = await mcp_client.load_all_mcp_tools()
        assert first[0] is second[0], "第二次装载走了重复初始化"

    @pytest.mark.unit
    async def test_disabled_servers_are_not_loaded(self, monkeypatch):
        from app.agents.mcp import client as c

        for flag in ALL_FLAGS:
            monkeypatch.setattr(c.settings, flag, False)
        monkeypatch.setattr(c, "_toolbox", None)

        assert await c.load_all_mcp_tools() == []
        assert c.enabled_servers() == []


# ============================================ 调用侧超时与重试（【工具调用流程】）
class TestInvokeTimeoutAndRetry:
    @staticmethod
    def _toolbox(**tools: Any):
        from app.agents.mcp.client import MCPToolbox

        tb = MCPToolbox()
        tb._tools = dict(tools)  # noqa: SLF001
        tb._loaded = True  # noqa: SLF001
        return tb

    @pytest.mark.unit
    def test_backoff_is_exponential(self):
        from app.agents.mcp.client import backoff_delay

        assert [backoff_delay(i) for i in range(3)] == [0.5, 1.0, 2.0]

    @pytest.mark.unit
    async def test_flaky_tool_is_retried_then_succeeds(self, monkeypatch):
        from app.agents.mcp import client as c

        monkeypatch.setattr(c.settings, "MCP_TOOL_RETRIES", 2)
        monkeypatch.setattr(c, "_BACKOFF_BASE", 0)  # 真等 1.5s 白搭

        class _Flaky:
            name = "flaky"

            def __init__(self) -> None:
                self.n = 0

            async def ainvoke(self, args: Any) -> str:
                self.n += 1
                if self.n < 3:
                    raise RuntimeError("503 upstream")
                return "ok"

        tool = _Flaky()
        assert await self._toolbox(flaky=tool).ainvoke("flaky", {}) == "ok"
        assert tool.n == 3

    @pytest.mark.unit
    async def test_persistent_failure_gives_up_after_the_configured_attempts(self, monkeypatch):
        from app.agents.mcp import client as c

        monkeypatch.setattr(c.settings, "MCP_TOOL_RETRIES", 2)
        monkeypatch.setattr(c, "_BACKOFF_BASE", 0)

        class _Dead:
            name = "dead"
            n = 0

            async def ainvoke(self, args: Any) -> str:
                _Dead.n += 1
                raise RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            await self._toolbox(dead=_Dead()).ainvoke("dead", {})
        assert _Dead.n == 3, "重试次数应为 1（首次）+ 2"

    @pytest.mark.unit
    async def test_single_call_timeout_is_enforced(self, monkeypatch):
        import asyncio

        from app.agents.mcp import client as c

        monkeypatch.setattr(c.settings, "MCP_TOOL_TIMEOUT", 1)
        monkeypatch.setattr(c.settings, "MCP_TOOL_RETRIES", 0)

        class _Hang:
            name = "hang"

            async def ainvoke(self, args: Any) -> str:
                await asyncio.sleep(30)
                return "never"

        with pytest.raises(TimeoutError):
            await self._toolbox(hang=_Hang()).ainvoke("hang", {})

    @pytest.mark.unit
    async def test_unknown_tool_is_not_retried(self, monkeypatch):
        """名字拼错是确定性问题，重试三遍只是让用户多等 1.5 秒。"""
        from app.agents.mcp import client as c

        monkeypatch.setattr(c, "_BACKOFF_BASE", 0)
        with pytest.raises(KeyError, match="未知工具"):
            await self._toolbox().ainvoke("typo_tool", {})


# ================================================== 验收 3：描述能驱动工具选择
# 每条 query 用"用户会说的话"，不含工具名。命中判定只看**工具描述本身**（字符
# 二元组重合度）—— 描述如果写得含糊，这里就会选错，这正是要拦住的事。
_QUERY_CASES = {
    "arxiv_search": "有几篇论文还没有正式发表，想先看看最新的",
    "pubmed_search": "帮我查一下医学和生物学方向的临床研究",
    "semantic_scholar_search": "哪些论文被引用得最多，哪些是这个领域的奠基工作",
    "python_exec": "先做数值计算，把标准差算出来",
    "web_search": "这个库的当前版本号是多少，网上能不能查到",
}


def _bigrams(text: str) -> set[str]:
    clean = "".join(ch for ch in text.lower() if ch.isalnum())
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _pick(query: str, descriptions: dict[str, str]) -> str:
    """最朴素的词袋路由，冒充"模型会怎么挑工具"。"""
    q = _bigrams(query)
    scored = {name: len(q & _bigrams(desc)) for name, desc in descriptions.items()}
    return max(scored, key=lambda n: scored[n])


class TestToolDescriptionsDriveSelection:
    @pytest.mark.unit
    async def test_each_description_contains_the_routing_signal(self, mcp_client):
        """每条描述都必须写清「何时使用 / 何时不要用」—— 这是模型唯一的决策依据。"""
        for tool in await mcp_client.load_all_mcp_tools():
            desc = tool.description
            assert "何时使用" in desc, f"{tool.name} 没写何时使用"
            assert "何时不要用" in desc, f"{tool.name} 没写何时不要用"
            assert "Args:" in desc and "Returns:" in desc, f"{tool.name} 参数/返回值没写清"

    @pytest.mark.unit
    async def test_descriptions_are_mutually_distinguishable(self, mcp_client):
        """两个工具的描述长得像，模型就会随机挑 —— 用重合度把这条钉死。"""
        tools = await mcp_client.load_all_mcp_tools()
        grams = {t.name: _bigrams(t.description) for t in tools}
        names = sorted(grams)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                union = grams[a] | grams[b]
                jaccard = len(grams[a] & grams[b]) / len(union) if union else 0.0
                assert jaccard < 0.5, f"{a} 与 {b} 的描述重合度 {jaccard:.2f}，模型分不出来"

    @pytest.mark.unit
    @pytest.mark.parametrize(("expected", "query"), sorted(_QUERY_CASES.items()))
    async def test_router_picks_the_right_tool_from_descriptions_alone(self, mcp_client, expected, query):
        descriptions = {t.name: t.description for t in await mcp_client.load_all_mcp_tools()}
        assert _pick(query, descriptions) == expected

    @pytest.mark.unit
    async def test_local_tools_have_descriptions_too(self):
        """本地工具（库内检索 / 图谱 / 出图 / 写作 / 翻译）与 MCP 工具同等对待。"""
        import importlib

        from app.agents.mcp.registry import _LOCAL  # noqa: PLC2701

        for name, (module_path, fn_name) in _LOCAL.items():
            fn = getattr(importlib.import_module(module_path), fn_name)
            doc = (fn.__doc__ or "").strip()
            assert "何时使用" in doc and "何时不要用" in doc, f"{name} 的描述不规范"
            assert "Args:" in doc and "Returns:" in doc, f"{name} 的描述不全"


# ============================================ 按意图过滤工具（控制 context 体积）
class TestToolsForIntent:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("intent", "wanted"),
        [
            ("single_paper_qa", ["retrieve_papers"]),
            ("cross_paper_reasoning", ["graph_analyze", "retrieve_papers"]),
            ("graph_analysis", ["graph_analyze", "retrieve_papers"]),
            ("writing_assist", ["retrieve_papers", "write_section"]),
            ("visualization", ["make_chart", "python_exec"]),
            ("translation", ["translate_text"]),
            ("chitchat", []),
        ],
    )
    async def test_intent_whitelist_is_applied(self, mcp_client, intent, wanted):
        from app.agents.mcp.registry import get_tools_for_intent

        assert await get_tools_for_intent(intent) == wanted

    @pytest.mark.unit
    async def test_literature_search_only_gets_external_sources(self, mcp_client):
        from app.agents.mcp.registry import get_tools_for_intent

        got = await get_tools_for_intent("literature_search")
        assert got == ["arxiv_search", "pubmed_search", "semantic_scholar_search", "web_search"]
        assert "retrieve_papers" not in got  # 找库外的，别把库内检索塞进来

    @pytest.mark.unit
    @pytest.mark.parametrize("intent", [None, "", "unknown_intent_of_the_future"])
    async def test_unknown_intent_falls_back_to_everything(self, mcp_client, intent):
        """没登记的意图给全量 —— 给空集会让整个 Plan 一步都跑不了。"""
        from app.agents.mcp.registry import get_tools_for_intent

        got = await get_tools_for_intent(intent)
        assert len(got) >= 10
        assert "retrieve_papers" in got and "arxiv_search" in got

    @pytest.mark.unit
    async def test_tools_from_disabled_servers_are_filtered_out(self, monkeypatch):
        """白名单里配了、但 Server 没开（缺 key）的工具不能出现在 prompt 里。"""
        from app.agents.mcp import client as c
        from app.agents.mcp.registry import get_tools_for_intent

        for flag in ALL_FLAGS:
            monkeypatch.setattr(c.settings, flag, False)
        monkeypatch.setattr(c, "_toolbox", None)

        assert await get_tools_for_intent("literature_search") == []
        assert await get_tools_for_intent("single_paper_qa") == ["retrieve_papers"]


# ======================================= 验收 5：工具失败 → Executor → Replanner
class TestToolFailureReachesReplan:
    @pytest.mark.unit
    async def test_exhausted_react_hands_a_failed_step_to_the_replanner(self, monkeypatch):
        """整条链路：executor 里工具连环失败 → 步骤 failed → 路由到 replanner → 出重规划。

        单看 executor 或单看 replanner 都测不出这条 —— 契约在两者之间的那个 dict 上。
        """
        import app.agents.nodes.executor as ex
        import app.agents.nodes.replanner as rp

        class _LoopingLLM:
            """每轮都要求调工具，永远不给结论 → ReAct 轮次用尽 → status=failed。"""

            async def complete(self, *a: Any, **kw: Any) -> str:
                return 'Thought: 再试一次\nAction: arxiv_search\nAction Input: {"query": "x"}'

        async def broken_tool(name: str, args: Any = None, **kw: Any) -> str:
            return f"工具 {name} 调用失败：TimeoutError: 30s 超时"

        monkeypatch.setattr(ex, "get_llm", lambda: _LoopingLLM())
        monkeypatch.setattr("app.agents.mcp.registry.call_tool", broken_tool)

        state: dict[str, Any] = {
            "query": "MoE 最新进展？",
            "plan": [{"idx": 1, "goal": "外部检索", "tool": "arxiv_search", "status": "pending"}],
            "current_step": 0,
        }
        after_executor = await ex.executor_node(state)

        assert after_executor["plan"][0]["status"] == "failed"
        assert "超时" in after_executor["plan"][0]["result"]
        assert ex.route_after_execute({**state, **after_executor}) == "replanner"

        # ---- 把 executor 的产物原样喂给 replanner
        seen: dict[str, str] = {}

        async def fake_structured(schema: Any, messages: Any, **kw: Any) -> Any:
            seen["prompt"] = "\n".join(str(m.get("content") or "") for m in messages)
            return rp.ReplanResult(
                decision="replan",
                reason="arxiv 超时，改用本地语料",
                new_steps=[{"idx": 1, "goal": "改用本地语料作答", "tool": "retrieve_papers"}],
            )

        monkeypatch.setattr(rp, "complete_structured", fake_structured)
        replanned = await rp.replanner_node({**state, **after_executor})

        assert "arxiv_search" in seen["prompt"], "失败的那一步必须写进 replanner 的 prompt"
        assert replanned["plan_round"] == 1
        assert replanned["current_step"] == 1  # 指向尾追的新步骤
        assert [s["status"] for s in replanned["plan"]] == ["replanned", "pending"]


# ============================================ 验收 6：FastAPI 端点对 MCP 客户端可见
class TestFastApiMCPExposure:
    @pytest.mark.unit
    async def test_api_endpoints_are_visible_as_mcp_tools(self):
        """真起一个 MCP 会话去 list_tools —— 只看路由挂没挂等于没测。"""
        from fastapi_mcp import FastApiMCP
        from mcp.shared.memory import create_connected_server_and_client_session

        from app.main import app

        exposed = FastApiMCP(app, name="research-copilot")
        exposed.setup_server()

        async with create_connected_server_and_client_session(exposed.server) as session:
            listed = await session.list_tools()

        names = {t.name for t in listed.tools}
        assert {"upload_paper", "list_papers", "ask_question", "retrieve_chunks", "analyze_graph"} <= names
        assert {"write_draft", "list_available_tools", "check_databases"} <= names
        assert len(names) >= 20, f"只暴露了 {len(names)} 个端点，路由没全进来"

    @pytest.mark.unit
    async def test_exposed_tools_carry_input_schemas(self):
        from fastapi_mcp import FastApiMCP
        from mcp.shared.memory import create_connected_server_and_client_session

        from app.main import app

        exposed = FastApiMCP(app, name="research-copilot")
        exposed.setup_server()
        async with create_connected_server_and_client_session(exposed.server) as session:
            listed = await session.list_tools()

        by_name = {t.name: t for t in listed.tools}
        assert "paper_id" in by_name["get_paper"].inputSchema["properties"]
        assert by_name["get_paper"].description, "工具没有描述，外部 MCP 客户端看不到用途"
