"""异步任务（PDF 解析/入库、引文边重建）。由 `app/indexing/runner.py` 在进程内执行。

任务是**进程内**的：没有 broker、没有 worker 容器，进程重启会丢正在跑的任务。
`status` 停在 running 的行即"上次没跑完"，手工重跑即可。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class Task(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    kind: Mapped[str] = mapped_column(String(64), index=True)  # parse_paper|rebuild_edges
    # pending | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0 ~ 1.0

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_tasks_kind_status", "kind", "status"),)

    def __repr__(self) -> str:
        return f"<Task {self.id[:8]} {self.kind} {self.status} {self.progress:.0%}>"
