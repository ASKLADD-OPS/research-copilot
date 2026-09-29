"""FastAPI 依赖项。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session


async def get_db() -> AsyncSession:
    """占位：真正的会话由 `get_session` 生成器提供，这里仅为类型标注服务。"""
    raise NotImplementedError  # pragma: no cover - 不会被调用


SessionDep = Annotated[AsyncSession, Depends(get_session)]


class Pagination:
    """分页参数。用依赖类而非三个散装 Query，路由签名更干净。"""

    def __init__(
        self,
        page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
        page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


PageDep = Annotated[Pagination, Depends(Pagination)]

__all__ = ["PageDep", "Pagination", "SessionDep", "get_db"]
