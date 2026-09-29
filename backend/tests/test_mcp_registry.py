"""工具注册表（Agent 侧调工具的唯一入口）测试。

关键行为：**工具不存在或调用失败时返回可读字符串，而不是抛异常**。
这是刻意的设计 —— 抛异常会打断整张图，而 LLM 看到"未知工具 X，可用工具：..."
完全有能力换个工具重试。这条约定错了，Agent 会变得脆得没法用。
"""

from __future__ import annotations

import pytest

from app.agents.mcp.registry import _LOCAL, call_tool, tool_names


@pytest.mark.unit
def test_local_tools_match_the_documented_set():
    assert set(_LOCAL) == {
        "retrieve_papers",
        "graph_analyze",
        "make_chart",
        "write_section",
        "translate_text",
    }


@pytest.mark.unit
def test_tool_names_are_sorted_and_unique():
    names = tool_names()
    assert names == sorted(set(names))
    assert set(names) == set(_LOCAL)


@pytest.mark.unit
async def test_unknown_tool_returns_readable_string():
    """未知工具不能抛异常，且要告诉模型可选项。"""
    result = await call_tool("definitely_not_a_tool", {})
    assert isinstance(result, str)
    assert "unknown" in result.lower() or "未知工具" in result
    assert "retrieve_papers" in result  # 附上可用清单


@pytest.mark.unit
async def test_unknown_tool_with_empty_args_does_not_crash():
    assert isinstance(await call_tool("nope", None), str)


@pytest.mark.unit
async def test_bad_args_on_local_tool_degrade_to_string():
    """参数不匹配（LLM 编错了 key）也要降级成字符串回喂，不能炸掉整图。"""
    result = await call_tool("translate_text", {"不存在的参数": 1, "text": "hi"})
    assert isinstance(result, str)
