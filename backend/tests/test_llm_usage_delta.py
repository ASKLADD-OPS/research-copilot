"""用量统计必须是"本轮增量"，不能是进程累计。

`get_llm()` 是 `@lru_cache` 单例，`usage` 挂在它身上 —— 直接上报
`usage.total_tokens` 得到的是**服务启动至今**的总数。实测同一进程里连打两次
闲聊，`done` 帧分别是 `481719` / `484886` tokens，而差值 `3167` 才是第二轮
的真实消耗。表现是前端显示一个随运行时长单调增长的假数字（演示页上就是
"问一句闲聊花了 48 万 token"），落库的 `run.tokens_used` 一并失真。

这类 bug 不报错、typecheck 全绿、日志正常，所以用一个契约测试把出口钉死。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.llm.client import LLMClient, usage_delta, usage_snapshot


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    """把单例换成一个可控客户端 —— `usage_*` 读的就是 `get_llm()`。"""
    client = LLMClient()
    monkeypatch.setattr("app.llm.client.get_llm", lambda: client)
    return client


@pytest.mark.unit
def test_delta_counts_only_this_turn(fake_llm: LLMClient) -> None:
    """历史累计再多，增量也只算这一轮。"""
    # 假装这个进程已经服务了很多轮
    fake_llm.usage.add("planner", prompt=480_000, completion=1_719)
    base = usage_snapshot()

    # 本轮真实消耗
    fake_llm.usage.add("executor", prompt=3_000, completion=167)

    assert usage_delta(base) == {"total_tokens": 3_167, "calls": 1}


@pytest.mark.unit
def test_delta_is_zero_when_nothing_ran(fake_llm: LLMClient) -> None:
    fake_llm.usage.add("planner", prompt=10, completion=5)
    base = usage_snapshot()

    assert usage_delta(base) == {"total_tokens": 0, "calls": 0}


@pytest.mark.unit
def test_snapshot_keys_match_what_delta_reads(fake_llm: LLMClient) -> None:
    """`usage_delta` 认的键必须真在快照里，否则会 KeyError。"""
    assert {"total", "calls"} <= set(usage_snapshot())


def _reads_cumulative_counter(path: pathlib.Path) -> bool:
    """代码里是否出现 `....usage.total_tokens`（AST 判断，避开文档字符串）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "total_tokens":
            continue
        inner = node.value
        if isinstance(inner, ast.Attribute) and inner.attr == "usage":
            return True
    return False


@pytest.mark.unit
def test_no_endpoint_reports_the_process_wide_counter() -> None:
    """出口一律 `usage_delta(usage_snapshot())`，不许直接读累计值。

    曾经 `chat` / `qa` / `writing` / `writing.outline` 四个模块六处都这么读，
    所以这里扫全仓而不是只盯着改动过的那几个文件。
    """
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = sorted(
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if p.name != "client.py" and _reads_cumulative_counter(p)
    )

    assert offenders == [], (
        f"这些文件直接把进程累计用量当成本轮用量报出去了，应改成 usage_delta(usage_snapshot())：{offenders}"
    )
