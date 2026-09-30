"""进程内后台任务壳：状态落库 + 失败不静默。

不打真库：`_set_status` 被换掉，`pipeline` 的两个函数被换成桩。要守住的是**状态机**
——pending → parsing → ready，以及"流水线抛异常时论文行必须记 failed"，还有
"流水线自己返回 ok=False 时不能被当成成功"。

阶段 1 起，进度不再写 tasks 表，而是直接写 `papers.parsed_status`
（规格里没有 tasks 表，而 parsed_status 本来就是"这篇到哪一步了"的语义）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.indexing import pipeline, runner

pytestmark = pytest.mark.unit


@pytest.fixture
def captured(monkeypatch):
    """收集 _set_status 的写入，并按调用顺序返回。"""
    writes: list[tuple[int, str, str | None]] = []

    async def fake_set_status(paper_id, status, *, error=None):
        writes.append((paper_id, status, error))

    monkeypatch.setattr(runner, "_set_status", fake_set_status)
    return writes


async def test_spawn_index_runs_pipeline_and_marks_parsing_first(captured, monkeypatch):
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": True, "paper_id": pid, "chunks": 7})

    runner.spawn_index(1)
    await runner.wait_all()

    # 只写了一次 parsing：终态 ready 由 pipeline 内部负责，
    # 任务壳不重复写 —— 两处都写终态迟早会不一致。
    assert [w[1] for w in captured] == ["parsing"]
    assert captured[0][0] == 1
    assert captured[0][2] is None


async def test_spawn_index_marks_failed_when_pipeline_reports_not_ok(captured, monkeypatch):
    """pipeline 自己吞异常并返回 ok=False —— 论文行必须跟着记失败。"""
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": False, "paper_id": pid, "error": "PDF 缺失"})

    runner.spawn_index(2)
    await runner.wait_all()

    assert [w[1] for w in captured] == ["parsing", "failed"]
    assert captured[-1][2] == "PDF 缺失"


async def test_spawn_index_survives_unexpected_exception(captured, monkeypatch):
    """流水线意外抛异常时不能静默消失，要落到 papers.parsed_status / error。"""

    def boom(pid):
        raise RuntimeError("Milvus 连不上")

    monkeypatch.setattr(pipeline, "index_paper", boom)

    runner.spawn_index(3)
    await runner.wait_all()

    assert captured[-1][1] == "failed"
    assert "Milvus 连不上" in captured[-1][2]


async def test_spawn_rebuild_edges_does_not_touch_paper_status(captured, monkeypatch):
    """重建引文边不动论文状态 —— 它不是"某篇论文的解析阶段"。"""
    monkeypatch.setattr(pipeline, "rebuild_citation_edges", lambda ids: {"ok": True, "linked": 3, "checked": 9})

    runner.spawn_rebuild_edges([1, 2])
    await runner.wait_all()

    assert captured == []


async def test_rebuild_edges_swallows_exception(monkeypatch):
    """重建失败不该把异常漏到事件循环里（asyncio 会打成 "Task exception was never retrieved"）。"""

    def boom(ids):
        raise RuntimeError("库挂了")

    monkeypatch.setattr(pipeline, "rebuild_citation_edges", boom)

    runner.spawn_rebuild_edges(None)
    await runner.wait_all()  # 不抛即通过


async def test_pending_set_is_empty_after_drain(captured, monkeypatch):
    """强引用集合必须自己清空，否则长时间运行会攒住所有任务对象。"""
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": True, "paper_id": pid})

    runner.spawn_index(5)
    assert len(runner._PENDING) == 1

    await runner.wait_all()
    # done_callback 是同步调用的，但可能排在下一个事件循环轮次
    await asyncio.sleep(0)
    assert not runner._PENDING
