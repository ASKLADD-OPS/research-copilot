"""进程内后台任务：长任务不进队列，直接跑在事件循环的线程池里。

原先靠 Celery + Redis 做 broker。现在没有队列了：`index_paper` 这类同步阻塞的
流水线用 `asyncio.to_thread` 挪到线程池，进度写 `tasks` 表，前端轮询。

**代价写在明处**：进程重启 = 正在跑的任务丢失，`tasks` 表会留下 `running` 状态。
单机 / 单副本部署够用；真要可靠队列时把 `_spawn` 换成 arq 或 rq 即可，
`app/indexing/pipeline.py` 一行都不用动 —— 流水线本身不知道自己被谁调度。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import update

from app.db.session import session_scope
from app.models import Task

# 必须持强引用：asyncio 只对任务持弱引用，跑到一半被 GC 掉是经典事故
_PENDING: set[asyncio.Task[None]] = set()


def _spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.get_running_loop().create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def _track(task_id: str | None, **fields: Any) -> None:
    """把状态写进 tasks 表。行不存在（比如已被清理）就静默跳过。"""
    if not task_id:
        return
    async with session_scope() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(**fields))


async def wait_all() -> None:
    """等当前挂着的后台任务收尾。测试用 —— 生产里没人该 await 这个。"""
    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


# ------------------------------------------------------------------ 任务壳
async def _run_index(task_id: str, paper_id: str) -> None:
    from app.indexing.pipeline import index_paper

    await _track(task_id, status="running", progress=0.1, attempts=1, started_at=datetime.now(UTC))
    try:
        # 解析 + 嵌入 + 写库全是同步阻塞调用，必须离开事件循环
        result = await asyncio.to_thread(index_paper, paper_id)
    except Exception as exc:  # noqa: BLE001 - 任务壳要吞掉异常并落库，不能让它变成"静默失败"
        logger.exception("后台入库任务异常 task_id={} paper_id={}", task_id, paper_id)
        await _track(
            task_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now(UTC),
        )
        return

    await _track(
        task_id,
        status="succeeded" if result.get("ok") else "failed",
        progress=1.0,
        result=result,
        error=result.get("error"),
        finished_at=datetime.now(UTC),
    )


async def _run_rebuild(task_id: str | None, paper_ids: list[str] | None) -> None:
    from app.indexing.pipeline import rebuild_citation_edges

    await _track(task_id, status="running", attempts=1, started_at=datetime.now(UTC))
    try:
        result = await asyncio.to_thread(rebuild_citation_edges, paper_ids)
    except Exception as exc:  # noqa: BLE001
        logger.exception("引文边重建任务异常 task_id={}", task_id)
        await _track(task_id, status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=datetime.now(UTC))
        return
    await _track(task_id, status="succeeded", progress=1.0, result=result, finished_at=datetime.now(UTC))


def spawn_index(task_id: str, paper_id: str) -> None:
    """解析入库丢到后台。调用方必须已经 commit 了对应的 tasks 行。"""
    _spawn(_run_index(task_id, paper_id))


def spawn_rebuild_edges(task_id: str | None, paper_ids: list[str] | None = None) -> None:
    _spawn(_run_rebuild(task_id, paper_ids))


__all__ = ["spawn_index", "spawn_rebuild_edges", "wait_all"]
