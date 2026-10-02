"""后台任务层。

**没有 Celery，没有 worker 容器。** 两类长任务都在 API 进程内跑：

- `app/indexing/runner.py`：PDF 解析入库流水线（`asyncio.to_thread` + 线程池）；
- `app/workers/scheduler.py`：每天 8:00 的订阅抓取（asyncio 调度循环）。

两者的取舍是同一个：单机单用户场景下，进程内跑省掉 broker + worker 的部署与运维，
代价是进程重启时在跑的任务丢失（判据与恢复方式各自写在模块头）。
"""

from app.workers.scheduler import (
    next_run_at,
    run_due_subscriptions,
    run_subscription,
    seconds_until,
    start_scheduler,
    stop_scheduler,
)

__all__ = [
    "next_run_at",
    "run_due_subscriptions",
    "run_subscription",
    "seconds_until",
    "start_scheduler",
    "stop_scheduler",
]
