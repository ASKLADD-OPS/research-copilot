"""Redis 客户端 —— 「三库」里的第三个。

**当前职责只有健康检查与预留缓存**。项目没有 Celery（长任务在
`app/indexing/runner.py` 里进程内跑），所以 Redis 不是任何链路的硬依赖：
连不上时 `ping()` 返回失败，`/api/v1/health/db` 如实报 degraded，
但问答、检索、入库全部照常工作。

**可选依赖**：`redis` 包没装时这个模块依然可导入（客户端置空），
理由同上 —— 一个健康检查不该把整个服务拖成 import error。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import logger

try:  # pragma: no cover - 取决于运行环境是否装了 redis
    from redis.asyncio import Redis
except Exception:  # noqa: BLE001
    Redis = None  # type: ignore[assignment]


def redis_available() -> bool:
    """`redis` 包是否可用。UI 与健康检查据此区分"没装"和"连不上"。"""
    return Redis is not None


async def get_redis() -> Any | None:
    """懒加载单例。未安装 redis 包时返回 None。"""
    if Redis is None:
        return None
    return _client()


@lru_cache(maxsize=1)
def _client() -> Any:
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
        socket_timeout=settings.REDIS_CONNECT_TIMEOUT,
        decode_responses=True,
    )


async def aping() -> tuple[bool, str]:
    """占用最小的探活：PING。"""
    if Redis is None:
        return False, "未安装 redis 包（pip install redis）"
    try:
        client = _client()
        pong = await client.ping()
        if not pong:
            return False, "PING 返回假值"
        info = await client.info("server")
        return True, f"PONG · redis {info.get('redis_version', '?')}"
    except Exception as exc:  # noqa: BLE001 - 探活永不抛
        return False, f"{type(exc).__name__}: {exc}"[:200]


async def aclose() -> None:
    if Redis is None:
        return
    try:
        client = _client()
        await client.aclose()
        _client.cache_clear()
    except Exception as exc:  # noqa: BLE001
        logger.debug("关闭 Redis 连接失败（忽略）：{}", exc)


__all__ = ["aclose", "aping", "get_redis", "redis_available"]
