"""SQLAlchemy 异步会话管理。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    # psycopg 的 connect_timeout；缺省 0 = 无限等，健康检查会挂死
    connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
    pool_pre_ping=True,  # 长连接被中间件掐断后自动重连
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 提交后仍可读对象属性，避免 API 层意外 lazy load
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：`session: AsyncSession = Depends(get_session)`。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """非请求上下文（后台入库任务、Agent 节点内部）用这个自己开事务。"""
    async with AsyncSessionLocal() as session, session.begin():
        yield session


async def dispose_engine() -> None:
    """应用关闭时释放连接池。"""
    await engine.dispose()
