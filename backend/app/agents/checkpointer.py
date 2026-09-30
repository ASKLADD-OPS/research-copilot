"""Checkpointer —— 会话状态持久化，支撑多轮对话与断点续跑。

优先 Postgres（生产 / docker-compose），不可用时退化为内存版，
保证本机裸跑与单测也能起图。

**这里曾经有一段长期失效的降级逻辑**（2026-09-30 修复）：

原实现用 `AsyncConnectionPool(conninfo, open=False)` + `AsyncPostgresSaver(pool)`
来"试探"Postgres，失败就退化为 `MemorySaver`。但这两个调用**都不建立连接** ——
`open=False` 的池不连接，包一层 saver 也不连接。于是：

    - try 块在应用运行时**永远不抛异常**，降级分支根本走不到；
    - 唯一能触发降级的情形是"没有运行中的事件循环"（池构造会报
      `no running event loop`）—— 也就是**只有单测里会退化，线上不会**；
    - 真正的连接失败被推迟到 `graph.astream()` 内部才爆。表现为
      `POST /chat/stream` 连一个 token 都吐不出来，只回一帧 error。

现在改成**真正连一次**（带 `connect_timeout`）再决定，并把结论缓存 ——
checkpointer 的选择本来就是每进程一次的事（图是单例），缓存与既有生命周期一致。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.config import settings

# 每进程探测一次。图是单例，checkpointer 的选择本来就只做一次。
_probe_result: bool | None = None


def _postgres_usable() -> bool:
    """真的连一次数据库来判断可用性。

    不能用 `AsyncConnectionPool(open=False)` 判断 —— 见模块头注释。
    也**不能用纯 socket 探测**：端口通不代表凭据对。本机就遇到过
    原生 PostgreSQL 在 5432 上监听、但角色密码不匹配的情况，
    纯 TCP 探测会误判为"可用"，然后照样在 astream 里炸掉。
    """
    conninfo = settings.sync_database_url.replace("postgresql+psycopg", "postgresql")
    try:
        import psycopg

        with psycopg.connect(conninfo, connect_timeout=min(settings.DB_CONNECT_TIMEOUT, 3)):
            return True
    except Exception as exc:  # noqa: BLE001 - 任何故障都只意味着"不可用"
        logger.info("Postgres 探测失败：{}", exc)
        return False


def _postgres_usable_cached() -> bool:
    global _probe_result
    if _probe_result is None:
        _probe_result = _postgres_usable()
    return _probe_result


def build_checkpointer() -> Any:
    """返回可用的 checkpointer：AsyncPostgresSaver > MemorySaver。"""
    from langgraph.checkpoint.memory import MemorySaver

    if not _postgres_usable_cached():
        logger.warning("Postgres 不可用，checkpointer 退化为 MemorySaver（会话状态不落库）")
        return MemorySaver()

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.sync_database_url.replace("postgresql+psycopg", "postgresql"),
            max_size=5,
            open=False,
        )
        saver = AsyncPostgresSaver(pool)
        logger.info("LangGraph checkpointer = AsyncPostgresSaver")
        return saver
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres checkpointer 构造失败（{}），退化为 MemorySaver", exc)
        return MemorySaver()


async def init_checkpointer_tables() -> None:
    """首次启动时建表（幂等）。

    顺带把探测结果预热进缓存 —— 这一步跑在 lifespan 里，不占请求路径，
    于是那次可能耗时数秒的连接超时不会落在第一个用户请求上。
    """
    cp = build_checkpointer()
    if hasattr(cp, "setup"):  # pragma: no cover - 需要真实数据库
        try:
            await cp.setup()
            logger.info("checkpointer 表已就绪")
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpointer.setup() 失败：{}", exc)


__all__ = ["build_checkpointer", "init_checkpointer_tables"]
