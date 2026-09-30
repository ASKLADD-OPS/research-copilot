"""启动引导：默认用户、Milvus 集合、建表。

`ensure_default_user` 是这层的核心。`papers.user_id` 与 `qa_history.user_id` 都是
NOT NULL，而阶段 1 还没有鉴权 —— 没有默认用户的话，一次上传都写不进去；
把 user_id 改成可空则会让 `UNIQUE (user_id, file_hash)` 静默失效
（PostgreSQL 里 NULL 互不相等）。所以默认用户不是权宜之计，是这套约束能成立的前提。
"""

from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import session_scope
from app.models import User

#: 进程内缓存，避免每次写库都查一次 users
_DEFAULT_USER_ID: int | None = None


async def ensure_default_user(session: AsyncSession | None = None) -> int:
    """幂等创建/取回本地默认用户，返回其 id。"""
    global _DEFAULT_USER_ID
    if _DEFAULT_USER_ID is not None:
        return _DEFAULT_USER_ID

    if session is not None:
        uid = await _ensure_in(session)
        _DEFAULT_USER_ID = uid
        return uid

    async with session_scope() as own:
        uid = await _ensure_in(own)
    _DEFAULT_USER_ID = uid
    return uid


async def _ensure_in(session: AsyncSession) -> int:
    """在给定会话里取回或创建默认用户。

    并发下两个请求可能同时 INSERT：靠 `email` 唯一约束兜底。这里用
    **SAVEPOINT（begin_nested）而不是 rollback** —— 调用方可能正处在自己的事务里，
    一次 rollback 会把它还没提交的写入一起抹掉。
    """
    email = settings.DEFAULT_USER_EMAIL
    existing = (await session.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    user = User(
        email=email,
        hashed_password=hash_password(settings.DEFAULT_USER_PASSWORD),
        role=settings.DEFAULT_USER_ROLE,
    )
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:  # 并发插入，别人先成功了
        row = (await session.execute(select(User.id).where(User.email == email))).scalar_one_or_none()
        if row is None:
            raise
        return int(row)

    logger.info("已创建默认用户 id={} email={}（阶段 1 尚无鉴权）", user.id, email)
    return int(user.id)


async def init_milvus(*, recreate: bool = False) -> list[str]:
    """建 Milvus 两个集合与索引。失败时向上抛，由调用方决定是否降级。"""
    from app.db.milvus import get_store

    created = await asyncio.to_thread(get_store().ensure_collections, recreate=recreate)
    if created:
        logger.info("Milvus 集合就绪：{}", ", ".join(created))
    else:
        logger.info("Milvus 集合已存在，跳过创建")
    return created


async def bootstrap(*, milvus: bool = True) -> dict[str, object]:
    """启动时的一站式初始化。

    **每一步失败都不该让服务起不来** —— 数据库没起也要能打开 /docs 和
    /api/v1/health/db，看清到底哪儿坏了。所以这里吞异常、只记 warning。
    """
    result: dict[str, object] = {"user_id": None, "milvus": None, "errors": []}

    try:
        result["user_id"] = await ensure_default_user()
    except Exception as exc:  # noqa: BLE001
        logger.warning("默认用户初始化失败：{}", exc)
        result["errors"].append(f"default_user: {exc}")  # type: ignore[union-attr]

    if milvus:
        try:
            result["milvus"] = await init_milvus()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 初始化跳过：{}", exc)
            result["errors"].append(f"milvus: {exc}")  # type: ignore[union-attr]

    return result


def reset_cache() -> None:
    """测试用：清掉进程内缓存的默认用户 id。"""
    global _DEFAULT_USER_ID
    _DEFAULT_USER_ID = None


__all__ = ["bootstrap", "ensure_default_user", "init_milvus", "reset_cache"]
