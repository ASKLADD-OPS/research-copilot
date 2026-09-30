"""进程内后台任务：长任务不进队列，直接跑在事件循环的线程池里。

原先靠 Celery + Redis 做 broker。现在没有队列了：`index_paper` 这类同步阻塞的
流水线用 `asyncio.to_thread` 挪到线程池，**进度直接写 `papers.parsed_status`**，
前端轮询 `GET /papers/{id}` 看状态。

为什么不再单独建一张任务表
--------------------------
阶段 1 的 schema 里没有 tasks 表，而 `papers.parsed_status` 本来就是这个语义：
一篇论文当前的解析阶段。多一张表就得维护两张表之间的一致性（任务成功但论文状态
没更新怎么办？），而这张表的唯一用途正好是"这篇到哪一步了"。

**代价写在明处**：进程重启 = 正在跑的任务丢失，`parsed_status` 会停在
`parsing` / `chunking` / `embedding`。判据就是这三个值 —— 重启后把卡住的重新排一次
即可。真要可靠队列时把 `_spawn` 换成 arq 或 rq，`pipeline.py` 一行都不用动。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from loguru import logger
from sqlalchemy import update

from app.db.session import session_scope
from app.models import Paper

# 必须持强引用：asyncio 只对任务持弱引用，跑到一半被 GC 掉是经典事故
_PENDING: set[asyncio.Task[None]] = set()


def _spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.get_running_loop().create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def _set_status(paper_id: int, status: str, *, error: str | None = None) -> None:
    """写论文状态。行已不存在（被删了）就静默跳过。"""
    async with session_scope() as session:
        await session.execute(update(Paper).where(Paper.id == paper_id).values(parsed_status=status, error=error))


async def wait_all() -> None:
    """等当前挂着的后台任务收尾。测试用 —— 生产里没人该 await 这个。"""
    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


# ------------------------------------------------------------------ 任务壳
async def _run_index(paper_id: int) -> None:
    from app.indexing.pipeline import index_paper

    await _set_status(paper_id, "parsing")
    try:
        # 解析 + 嵌入 + 写库全是同步阻塞调用，必须离开事件循环
        result = await asyncio.to_thread(index_paper, paper_id)
    except Exception as exc:  # noqa: BLE001 - 任务壳要吞掉异常并落库，不能让它变成"静默失败"
        logger.exception("后台入库任务异常 paper_id={}", paper_id)
        await _set_status(paper_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return

    if not result.get("ok"):
        # pipeline 内部已经把 status/error 写进去了，这里只做兜底
        await _set_status(paper_id, "failed", error=str(result.get("error") or "入库失败"))


async def _run_rebuild(paper_ids: list[int] | None) -> None:
    from app.indexing.pipeline import rebuild_citation_edges

    try:
        result = await asyncio.to_thread(rebuild_citation_edges, paper_ids)
        logger.info("引文边重建完成 {}", result)
    except Exception:  # noqa: BLE001
        logger.exception("引文边重建任务异常")


def spawn_index(paper_id: int) -> None:
    """解析入库丢到后台。调用方必须已经 commit 了对应的 papers 行。"""
    _spawn(_run_index(paper_id))


def spawn_rebuild_edges(paper_ids: list[int] | None = None) -> None:
    _spawn(_run_rebuild(paper_ids))


__all__ = ["spawn_index", "spawn_rebuild_edges", "wait_all"]
