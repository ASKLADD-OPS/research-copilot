"""用户。"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IntPKMixin


class User(IntPKMixin, CreatedAtMixin, Base):
    """账号。

    鉴权尚未接线（阶段 1 只做持久层），但 `papers.user_id` 一旦可空，
    `UNIQUE (user_id, file_hash)` 就失效了 —— PostgreSQL 里 NULL 互不相等，
    匿名论文会被无限重复插入，去重形同虚设。所以这里 user_id 是 **NOT NULL**，
    未登录场景由 `app.db.bootstrap.ensure_default_user()` 提供一个本地默认用户。
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="user")  # user | admin

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} role={self.role}>"


__all__ = ["User"]
