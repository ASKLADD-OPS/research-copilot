"""论文管理：上传 / 列表 / 详情 / 更新 / 删除 / 重新索引 / 分块 / 原文。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageDep, SessionDep
from app.core.config import settings
from app.core.errors import BadRequestError, NotFoundError, UnsupportedFileTypeError
from app.db.milvus import get_store
from app.models import Paper, PaperChunk, Task
from app.models.base import new_uuid
from app.schemas import (
    ApiResponse,
    Page,
    PageMeta,
    PaperChunkOut,
    PaperCreate,
    PaperDetail,
    PaperOut,
    PaperUpdate,
    PaperUploadResult,
)

router = APIRouter(prefix="/papers", tags=["论文"])

_ALLOWED_SUFFIX = {".pdf"}
_CHUNK = 1024 * 1024  # 1MB 一片读，避免整份 PDF 进内存


async def _save_upload(file: UploadFile, suffix: str) -> tuple[Path, int]:
    """流式落盘，同时守住体积上限。"""
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    dest = settings.upload_path / f"{new_uuid()}{suffix}"
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise BadRequestError(f"文件超过上限 {settings.MAX_UPLOAD_MB}MB")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise BadRequestError("上传文件为空")
    return dest, size


async def _dispatch_index(session: AsyncSession, paper: Paper) -> str:
    """登记任务行并丢进后台执行，返回 task_id。

    任务行必须**先 commit** 再 spawn —— 后台协程用的是另一个会话，
    没提交它读不到这行，进度就写不进去。
    """
    from app.indexing.runner import spawn_index

    task = Task(kind="parse_paper", status="pending", payload={"paper_id": paper.id})
    session.add(task)
    await session.commit()
    await session.refresh(task)

    spawn_index(task.id, paper.id)
    return task.id


@router.post("/upload", response_model=ApiResponse[PaperUploadResult], summary="上传 PDF 并入库")
async def upload_paper(
    session: SessionDep,
    file: Annotated[UploadFile, File(description="PDF 文件")],
    title: Annotated[str, Form()] = "",
    index: Annotated[bool, Form(description="是否立即解析入库")] = True,
) -> ApiResponse[PaperUploadResult]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIX:
        raise UnsupportedFileTypeError(f"仅支持 PDF，收到 {suffix or '未知类型'}")

    dest, size = await _save_upload(file, suffix)
    paper = Paper(
        title=title or Path(file.filename or "").stem,
        source="upload",
        pdf_path=str(dest),
        file_size=size,
        status="pending",
    )
    session.add(paper)
    await session.flush()  # 拿到 paper.id，任务 payload 要用

    task_id: str | None = None
    if index:
        task_id = await _dispatch_index(session, paper)
    else:
        await session.commit()
    await session.refresh(paper)

    logger.info("上传完成 paper_id={} size={} task_id={}", paper.id, size, task_id)
    return ApiResponse.ok(
        PaperUploadResult(paper=PaperOut.model_validate(paper), task_id=task_id),
        message="已入库，后台解析中" if task_id else "已登记（未解析）",
    )


@router.post("", response_model=ApiResponse[PaperOut], summary="手工登记论文（无 PDF）")
async def create_paper(payload: PaperCreate, session: SessionDep) -> ApiResponse[PaperOut]:
    paper = Paper(**payload.model_dump(), status="pending")
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return ApiResponse.ok(PaperOut.model_validate(paper))


@router.get("", response_model=ApiResponse[Page[PaperOut]], summary="论文列表")
async def list_papers(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str, Query(description="标题/摘要模糊搜索")] = "",
    status: Annotated[str | None, Query(description="pending|parsing|indexing|ready|failed")] = None,
) -> ApiResponse[Page[PaperOut]]:
    stmt = select(Paper)
    count_stmt = select(func.count()).select_from(Paper)

    if q:
        like = f"%{q}%"
        cond = Paper.title.ilike(like) | Paper.abstract.ilike(like)
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    if status:
        stmt, count_stmt = stmt.where(Paper.status == status), count_stmt.where(Paper.status == status)

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        (await session.execute(stmt.order_by(Paper.created_at.desc()).offset(page.offset).limit(page.page_size)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        Page[PaperOut](
            items=[PaperOut.model_validate(r) for r in rows],
            meta=PageMeta(
                total=total,
                page=page.page,
                page_size=page.page_size,
                has_next=page.offset + len(rows) < total,
            ),
        )
    )


@router.get("/{paper_id}", response_model=ApiResponse[PaperDetail], summary="论文详情")
async def get_paper(paper_id: str, session: SessionDep) -> ApiResponse[PaperDetail]:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError(f"论文不存在: {paper_id}")
    return ApiResponse.ok(PaperDetail.model_validate(paper))


@router.patch("/{paper_id}", response_model=ApiResponse[PaperOut], summary="更新元数据")
async def update_paper(paper_id: str, payload: PaperUpdate, session: SessionDep) -> ApiResponse[PaperOut]:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError(f"论文不存在: {paper_id}")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(paper, key, value)
    await session.commit()
    await session.refresh(paper)
    return ApiResponse.ok(PaperOut.model_validate(paper))


@router.post("/{paper_id}/reindex", response_model=ApiResponse[PaperUploadResult], summary="重新解析入库")
async def reindex_paper(paper_id: str, session: SessionDep) -> ApiResponse[PaperUploadResult]:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError(f"论文不存在: {paper_id}")
    if not paper.pdf_path or not await asyncio.to_thread(Path(paper.pdf_path).exists):
        raise BadRequestError("该论文没有可用的 PDF 文件，无法重新索引")

    paper.status = "pending"
    task_id = await _dispatch_index(session, paper)
    await session.refresh(paper)
    return ApiResponse.ok(PaperUploadResult(paper=PaperOut.model_validate(paper), task_id=task_id))


@router.get("/{paper_id}/chunks", response_model=ApiResponse[list[PaperChunkOut]], summary="分块内容")
async def list_chunks(
    paper_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ApiResponse[list[PaperChunkOut]]:
    rows = (
        (
            await session.execute(
                select(PaperChunk).where(PaperChunk.paper_id == paper_id).order_by(PaperChunk.chunk_index).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse.ok([PaperChunkOut.model_validate(r) for r in rows])


@router.get("/{paper_id}/file", summary="原始 PDF（供 PDF.js 取流）")
async def get_pdf(paper_id: str, session: SessionDep) -> FileResponse:
    paper = await session.get(Paper, paper_id)
    if paper is None or not paper.pdf_path:
        raise NotFoundError(f"论文不存在或没有 PDF: {paper_id}")
    path = Path(paper.pdf_path)
    # pathlib 是阻塞调用，放进线程池，别卡住事件循环（ASYNC240）
    if not await asyncio.to_thread(path.exists):
        raise NotFoundError("PDF 文件已丢失")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{(paper.title or paper_id)[:80]}.pdf",
    )


@router.delete("/{paper_id}", response_model=ApiResponse[dict[str, Any]], summary="删除论文")
async def delete_paper(paper_id: str, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError(f"论文不存在: {paper_id}")

    pdf_path = paper.pdf_path
    # Milvus 与 PG 不是同一事务，先删向量再删行 —— 反过来的话中途失败会留下幽灵向量
    try:
        await asyncio.to_thread(get_store().delete_by_paper, paper_id)
    except Exception as exc:  # noqa: BLE001 - Milvus 不可用不该阻塞删除
        logger.warning("删除 Milvus 向量失败 paper_id={}: {}", paper_id, exc)

    await session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper_id))
    await session.delete(paper)
    await session.commit()

    if pdf_path:
        await asyncio.to_thread(Path(pdf_path).unlink, missing_ok=True)
    return ApiResponse.ok({"deleted": paper_id})
