"""Checkpointer —— 会话状态持久化，支撑多轮对话与断点续跑。

优先 Postgres（生产 / docker-compose），无连接时退化为内存版，
保证本机裸跑与单测也能起图。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.config import settings


def build_checkpointer() -> Any:
    """返回可用的 checkpointer：PostgresSaver > MemorySaver。"""
    try:  # pragma: no cover - 依赖真实数据库
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
        logger.warning("Postgres checkpointer 不可用（{}），退化为 MemorySaver", exc)
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


async def init_checkpointer_tables() -> None:
    """首次启动时建表（幂等）。"""
    cp = build_checkpointer()
    if hasattr(cp, "setup"):  # pragma: no cover - 需要真实数据库
        try:
            await cp.setup()
            logger.info("checkpointer 表已就绪")
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkpointer.setup() 失败：{}", exc)


__all__ = ["build_checkpointer", "init_checkpointer_tables"]
