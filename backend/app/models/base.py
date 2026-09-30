"""SQLAlchemy 2.0 声明式基类与公共 Mixin。

主键统一用 `BigInteger` 自增 —— 这不是随意选择：Milvus 的 `paper_id / chunk_id`
必须是 int64（向量库只能索引定长数值）。PG 侧若用 UUID 字符串，就得再维护一张
映射表或在写入时现算代理键，两边都要额外对账。直接用整数主键，PG 与 Milvus
天然对齐，`chunks.id == paper_chunks.chunk_id` 是一等公民而不是口头约定。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有模型的基类。"""


class IntPKMixin:
    """自增 int64 主键。`Identity()` 比 `SERIAL` 更标准，Alembic 也能稳定复现。"""

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin(CreatedAtMixin):
    """额外带 updated_at 的表用它（规格里没有这张表，按需继承）。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["Base", "CreatedAtMixin", "IntPKMixin", "TimestampMixin"]
