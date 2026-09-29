"""进程内后台任务壳：进度落库 + 失败不静默。

不打真库：`_track` 被换掉，`pipeline` 的那两个函数被换成桩。要守住的是**状态机**
——running → succeeded/failed，以及"流水线抛异常时任务行必须记 failed"。
"""

from __future__ import annotations

import asyncio

import pytest

from app.indexing import pipeline, runner

pytestmark = pytest.mark.unit


@pytest.fixture
def captured(monkeypatch):
    """收集 _track 的写入，并按调用顺序返回。"""
    writes: list[tuple[str, dict]] = []

    async def fake_track(task_id, **fields):
        writes.append((task_id, fields))

    monkeypatch.setattr(runner, "_track", fake_track)
    return writes


async def test_spawn_index_marks_succeeded(captured, monkeypatch):
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": True, "paper_id": pid, "chunks": 7})

    runner.spawn_index("task-1", "paper-1")
    await runner.wait_all()

    assert [w[1]["status"] for w in captured] == ["running", "succeeded"]
    assert captured[0][0] == "task-1"
    assert captured[1][1]["result"]["chunks"] == 7
    assert captured[1][1]["progress"] == 1.0
    assert "finished_at" in captured[1][1]


async def test_spawn_index_marks_failed_when_pipeline_reports_not_ok(captured, monkeypatch):
    """pipeline 自己吞异常并返回 ok=False —— 任务行必须跟着记失败。"""
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": False, "paper_id": pid, "error": "PDF 缺失"})

    runner.spawn_index("task-2", "paper-2")
    await runner.wait_all()

    assert captured[-1][1]["status"] == "failed"
    assert captured[-1][1]["error"] == "PDF 缺失"


async def test_spawn_index_survives_unexpected_exception(captured, monkeypatch):
    """流水线意外抛异常时任务不能静默消失，要落到 tasks 表。"""

    def boom(pid):
        raise RuntimeError("Milvus 连不上")

    monkeypatch.setattr(pipeline, "index_paper", boom)

    runner.spawn_index("task-3", "paper-3")
    await runner.wait_all()

    assert captured[-1][1]["status"] == "failed"
    assert "Milvus 连不上" in captured[-1][1]["error"]


async def test_spawn_rebuild_edges_records_result(captured, monkeypatch):
    monkeypatch.setattr(pipeline, "rebuild_citation_edges", lambda ids: {"ok": True, "linked": 3, "checked": 9})

    runner.spawn_rebuild_edges("task-4", ["p1"])
    await runner.wait_all()

    assert captured[-1][1]["status"] == "succeeded"
    assert captured[-1][1]["result"]["linked"] == 3


async def test_track_is_noop_without_task_id(monkeypatch):
    """没有任务行（例如 graph/rebuild 不传 id）时不该去碰数据库。"""
    calls = []
    monkeypatch.setattr(runner, "session_scope", lambda: calls.append(1))

    await runner._track(None, status="running")
    assert calls == []


async def test_pending_set_is_empty_after_drain(captured, monkeypatch):
    """强引用集合必须自己清空，否则长时间运行会攒住所有任务对象。"""
    monkeypatch.setattr(pipeline, "index_paper", lambda pid: {"ok": True, "paper_id": pid})

    runner.spawn_index("t5", "p5")
    assert len(runner._PENDING) == 1

    await runner.wait_all()
    # done_callback 是同步调用的，但可能排在下一个事件循环轮次
    await asyncio.sleep(0)
    assert not runner._PENDING
