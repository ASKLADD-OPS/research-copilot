"""进程内定时调度器 —— 每天 8:00 把订阅的主题各抓一遍。

为什么不是 Celery Beat
----------------------
项目显式不用 Celery，这一点在四处写在明处：`pyproject.toml` 依赖块末尾的注释、
`app/indexing/runner.py` 的头注释、`app/core/config.py` 的 Redis 段落、
`app/api/v1/papers.py` 的模块头。理由都一样：长任务用 `asyncio.to_thread` 在进程内跑，
就不需要 broker + worker 容器 + 一套部署编排。

一个"每天一次、单用户、分钟量级"的抓取任务，为它引入 Celery 是纯负债：多两个进程要部署、
多一份 broker 要运维、多一类"任务投出去了但 worker 没起"的故障模式。而它换来的
可靠性（任务不丢）在这里恰恰不是重点 —— 丢了明天再抓，论文级去重
（`arxiv_id` / `file_hash`，见 `app/models/paper.py`）保证不会重复入库。

代价写在明处
------------
**进程重启 = 调度状态丢失**。重启后不补跑错过的那一次，直接算"下一个 8:00"。
这与 `runner.py` 的取舍同构（那里丢的是在跑的入库任务，`papers.parsed_status`
会停在 parsing/chunking/embedding，判据就是这三个值）。真需要"错过也补跑"时，
把 `run_due_subscriptions` 挂到 arq / rq 的 cron 上即可 —— `explore()` 是一支
异步生成器，谁当驱动器都行，一行都不用改。

时区
----
按**服务器本地时间**算（`datetime.now()`），不做时区转换：这是个桌面端单机应用，
"每天 8:00"指的就是用户机器上的 8:00。中国无夏令时，`seconds_until` 不需要
处理跳变；真要跨时区部署时再引入 zoneinfo。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from app.core.config import settings

#: 必须持强引用：asyncio 只对任务持弱引用，跑到一半被 GC 掉是经典事故
#: （与 `app/indexing/runner.py::_PENDING` 同一个理由）。
_task: asyncio.Task[None] | None = None

#: 调度线程名，便于在日志与调试器里认出来
TASK_NAME = "subscription-scheduler"


# ==================================================================== 时间
def seconds_until(hour: int, minute: int, *, now: datetime | None = None) -> float:
    """距离下一个 `hour:minute` 还有多少秒。纯函数，便于单测。

    恰好等于目标时刻（秒与微秒都为 0）时返回 0 而不是一整天 —— 调用方
    `await asyncio.sleep(0)` 会立刻跑，这是"刚好卡在 8:00:00 启动"时该有的行为。
    """
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def humanize(seconds: float) -> str:
    """`43200` → `12h0m`。只用于日志，不参与计算。"""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    return f"{hours}h{rest // 60}m"


def next_run_at(now: datetime | None = None) -> datetime:
    """下一次触发的时刻。`GET /tools/scheduler` 用它回答"到底什么时候跑"。"""
    now = now or datetime.now()
    return now + timedelta(seconds=seconds_until(settings.SUBSCRIBE_HOUR, settings.SUBSCRIBE_MINUTE, now=now))


# ==================================================================== 单个订阅
async def mark_running(sub_id: int) -> tuple[str, int, float] | None:
    """把订阅标成 running 并取出抓取参数 `(topic, max_papers, min_score)`。

    返回 `None` 表示订阅不存在（或被删了）。

    **先标 running 再抓**：抓一轮可能要几分钟，不先落状态的话界面上会一直停在
    上一次的结果，看起来像"点了没反应"。
    """
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models import Subscription

    async with AsyncSessionLocal() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
        if sub is None:
            logger.warning("订阅 {} 不存在，跳过", sub_id)
            return None
        sub.last_status = "running"
        sub.last_error = None
        await session.commit()
        return sub.topic, sub.max_papers, sub.min_score


async def record_run(sub_id: int, payload: dict[str, Any], *, error: str = "") -> None:
    """把一次探索的结果写回订阅行。

    `payload` 是 `explore()` 的 `done` 载荷（中途失败时可能是空字典）。
    这个函数被两条路径共用 —— 调度器的 `run_subscription`，以及
    `POST /tools/subscriptions/{id}/run` 那条 SSE 流。手动跑一次也要更新
    `last_status`/`last_new`，否则"立即抓取"和"定时抓取"会给出两种状态。
    """
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models import Subscription

    new = int(payload.get("new") or 0)
    async with AsyncSessionLocal() as session:
        sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
        if sub is None:  # 抓取过程中被删了
            logger.warning("订阅 {} 已在抓取期间被删除，结果丢弃", sub_id)
            return
        sub.last_run_at = datetime.now(UTC)
        sub.last_new = new
        sub.total_new = int(sub.total_new or 0) + new
        sub.last_status = "failed" if error else "ok"
        sub.last_error = error or None
        await session.commit()


async def run_subscription(sub_id: int) -> dict[str, Any]:
    """跑一个订阅：置状态 → 排空探索闭环 → 写回结果。

    用**两条独立的会话**而不是一条：`explore()` 内部自己开会话逐篇入库
    （见 `explore._download`），把 `running` 标记与最终结果分开提交，
    才不会出现"抓了 20 分钟但 `last_status` 一直停在上一轮"的观感。
    """
    from app.agents.explore import explore

    target = await mark_running(sub_id)
    if target is None:
        return {"ok": False, "subscription_id": sub_id, "error": "订阅不存在"}
    topic, ceiling, threshold = target

    payload: dict[str, Any] = {}
    error = ""
    try:
        # 排空生成器：轨迹事件在这里没有消费者（要看得走 /tools/explore 那条 SSE），
        # 但**必须排空** —— done 载荷就是最后那一条事件。
        async for event, data in explore(topic, max_papers=ceiling, min_score=threshold):
            if event == "done":
                payload = data
    except Exception as exc:  # noqa: BLE001 - 一个订阅失败不能带走其余订阅
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("订阅抓取异常 id={} topic={!r}", sub_id, topic)

    await record_run(sub_id, payload, error=error)

    new = int(payload.get("new") or 0)
    downloaded = int(payload.get("downloaded") or 0)
    logger.info(
        "订阅 {} 完成 topic={!r} 新增={} 入库={} 推荐={} 说明={}",
        sub_id,
        topic,
        new,
        downloaded,
        len(payload.get("recommendations") or []),
        error or str(payload.get("critique") or "")[:120],
    )
    return {
        "ok": not error,
        "subscription_id": sub_id,
        "topic": topic,
        "new": new,
        "downloaded": downloaded,
        "recommendations": len(payload.get("recommendations") or []),
        "error": error or None,
    }


async def run_due_subscriptions() -> list[dict[str, Any]]:
    """把 `enabled` 的订阅各跑一遍。

    **串行**：嵌入模型是进程内单例（`get_embedder` 的 lru_cache），并发前向会把
    CPU/显存打满；arXiv 也不欢迎并发抓取。串行代价是"订阅多了会跑很久"，
    而这是每天一次的后台任务，没人等它。
    """
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models import Subscription

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Subscription.id).where(Subscription.enabled.is_(True)).order_by(Subscription.topic)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        logger.info("订阅抓取：没有启用的订阅，跳过")
        return []

    logger.info("订阅抓取开始：{} 个订阅", len(rows))
    results = [await run_subscription(int(sub_id)) for sub_id in rows]
    logger.info("订阅抓取结束：新增 {} 篇", sum(int(r.get("new") or 0) for r in results))
    return results


# ==================================================================== 调度循环
async def scheduler_loop() -> None:
    """永续循环：睡到下一个 8:00 → 跑一轮 → 再睡。"""
    hour, minute = settings.SUBSCRIBE_HOUR, settings.SUBSCRIBE_MINUTE
    while True:
        delay = seconds_until(hour, minute)
        logger.info("订阅调度已就绪：每天 {:02d}:{:02d} 抓取，下次在 {} 后", hour, minute, humanize(delay))
        await asyncio.sleep(delay)
        try:
            await run_due_subscriptions()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一轮失败不能杀死循环，否则从此不再抓
            logger.exception("订阅抓取轮次异常（不影响下一次调度）")


def start_scheduler() -> asyncio.Task[None]:
    """启动调度任务。幂等 —— 已经在跑就把现有任务还回去。"""
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.get_running_loop().create_task(scheduler_loop(), name=TASK_NAME)
    return _task


async def stop_scheduler(task: asyncio.Task[None] | None = None) -> None:
    """取消并等它收尾。`asyncio.sleep` 长眠时被 cancel 会立刻抛 CancelledError，不会等满。"""
    global _task
    target = task if task is not None else _task
    _task = None
    if target is None or target.done():
        return
    target.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await target


__all__ = [
    "TASK_NAME",
    "humanize",
    "mark_running",
    "next_run_at",
    "record_run",
    "run_due_subscriptions",
    "run_subscription",
    "scheduler_loop",
    "seconds_until",
    "start_scheduler",
    "stop_scheduler",
]
