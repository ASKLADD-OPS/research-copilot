"""任务相关响应模型（后台入库 / 引文边重建的进度）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    progress: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


__all__ = ["TaskOut"]
