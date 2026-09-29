"""任务查询：后台入库 / 引文边重建的进度。

任务是进程内跑的（见 `app/indexing/runner.py`），所以这里只是读 `tasks` 表，
不提供"取消"—— 真要取消得先有队列。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.errors import NotFoundError
from app.models import Task
from app.schemas import ApiResponse, TaskOut

router = APIRouter(prefix="/tasks", tags=["任务"])


@router.get("", response_model=ApiResponse[list[TaskOut]], summary="最近的任务")
async def list_tasks(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100, description="返回条数")] = 20,
) -> ApiResponse[list[TaskOut]]:
    rows = (await session.execute(select(Task).order_by(Task.created_at.desc()).limit(limit))).scalars().all()
    return ApiResponse.ok([TaskOut.model_validate(r) for r in rows])


@router.get("/{task_id}", response_model=ApiResponse[TaskOut], summary="任务详情")
async def get_task(task_id: str, session: SessionDep) -> ApiResponse[TaskOut]:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    return ApiResponse.ok(TaskOut.model_validate(task))
