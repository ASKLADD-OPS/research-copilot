"""论文管理：上传 / 列表 / 详情 / 更新 / 删除 / 重新索引 / 分块 / 原文。

两个 id 相关的硬性约定（阶段 1 规格决定，改动前先看 models/base.py 的说明）：
- 主键是 **int64**（`BigInteger`），不是 UUID —— 只为与 Milvus 的 INT64 对齐；
- 因此路由参数是 `int`，传 UUID 字符串会直接被 FastAPI 判 422。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageDep, SessionDep
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, UnsupportedFileTypeError
from app.db.bootstrap import ensure_default_user
from app.db.milvus import get_store
from app.indexing.pipeline import semantic_hash
from app.models import Chunk, Paper, PaperVersion
from app.schemas import (
    ApiResponse,
    ChunkOut,
    Page,
    PageMeta,
    PaperCreate,
    PaperDetail,
    PaperOut,
    PaperUpdate,
    PaperUploadResult,
)

router = APIRouter(prefix="/papers", tags=["论文"])

_ALLOWED_SUFFIX = {".pdf"}
_CHUNK = 1024 * 1024  # 1MB 一片读，避免整份 PDF 进内存


async def _save_upload(file: UploadFile, suffix: str) -> tuple[Path, int, str]:
    """流式落盘，同时守体积上限、顺手算 sha256 作为内容指纹。

    边写边算哈希：读第二遍只为算哈希是最容易忘掉的性能税，
    而 PDF 动辄几十 MB，多读一遍在慢盘上是肉眼可见的等待。
    """
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    dest = settings.upload_path / f"{hashlib.sha1(str(id(file)).encode()).hexdigest()[:16]}{suffix}"
    size = 0
    digest = hashlib.sha256()
    with dest.open("wb") as out:
        while piece := await file.read(_CHUNK):
            size += len(piece)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise BadRequestError(f"文件超过上限 {settings.MAX_UPLOAD_MB}MB")
            digest.update(piece)
            out.write(piece)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise BadRequestError("上传文件为空")
    return dest, size, digest.hexdigest()


async def _get_paper(session: AsyncSession, paper_id: int) -> Paper:
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise NotFoundError(f"论文不存在: {paper_id}")
    return paper


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

    dest, size, file_hash = await _save_upload(file, suffix)
    user_id = await ensure_default_user(session)

    # 内容去重：命中 (user_id, file_hash) 唯一约束就复用已有行，不重复落盘解析。
    # 这是"同一份 PDF 传两次"的正解 —— 而不是等 INSERT 撞约束再报 409，
    # 那样用户拿不到已经入库的那一篇。
    existing = (
        await session.execute(select(Paper).where(Paper.user_id == user_id, Paper.file_hash == file_hash))
    ).scalar_one_or_none()
    if existing is not None:
        await asyncio.to_thread(dest.unlink, missing_ok=True)
        logger.info("上传内容重复，复用已有论文 paper_id={} hash={}", existing.id, file_hash[:12])
        return ApiResponse.ok(
            PaperUploadResult(paper=PaperOut.model_validate(existing), index_started=False),
            message=f"内容与已有论文重复（id={existing.id}），已复用，未重复解析",
        )

    paper = Paper(
        user_id=user_id,
        title=title or Path(file.filename or "").stem,
        source_url=None,
        file_path=str(dest),
        file_hash=file_hash,
        parsed_status="pending",
    )
    session.add(paper)
    await session.flush()

    index_started = False
    if index:
        await session.commit()  # 必须先提交：后台协程用另一个会话，没提交它读不到这行
        from app.indexing.runner import spawn_index

        spawn_index(paper.id)
        index_started = True
        await session.refresh(paper)
    else:
        await session.commit()
        await session.refresh(paper)

    logger.info("上传完成 paper_id={} size={} index={}", paper.id, size, index_started)
    return ApiResponse.ok(
        PaperUploadResult(paper=PaperOut.model_validate(paper), index_started=index_started),
        message="已入库，后台解析中（轮询 GET /papers/{id} 看 parsed_status）" if index_started else "已登记（未解析）",
    )


@router.post("", response_model=ApiResponse[PaperOut], summary="手工登记论文（无 PDF）")
async def create_paper(payload: PaperCreate, session: SessionDep) -> ApiResponse[PaperOut]:
    user_id = await ensure_default_user(session)
    data = payload.model_dump()
    paper = Paper(
        **data,
        user_id=user_id,
        parsed_status="pending",
        semantic_hash=semantic_hash(data.get("title"), data.get("abstract")),
    )
    session.add(paper)
    await session.commit()
    await session.refresh(paper)
    return ApiResponse.ok(PaperOut.model_validate(paper))


@router.get("", response_model=ApiResponse[Page[PaperOut]], summary="论文列表")
async def list_papers(
    session: SessionDep,
    page: PageDep,
    q: Annotated[str, Query(description="标题/摘要模糊搜索")] = "",
    status: Annotated[str | None, Query(description="pending|parsing|chunking|embedding|ready|failed")] = None,
) -> ApiResponse[Page[PaperOut]]:
    stmt = select(Paper)
    count_stmt = select(func.count()).select_from(Paper)

    if q:
        like = f"%{q}%"
        cond = Paper.title.ilike(like) | Paper.abstract.ilike(like)
        stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
    if status:
        stmt, count_stmt = stmt.where(Paper.parsed_status == status), count_stmt.where(Paper.parsed_status == status)

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
                total=total, page=page.page, page_size=page.page_size, has_next=page.offset + len(rows) < total
            ),
        )
    )


@router.get("/{paper_id}", response_model=ApiResponse[PaperDetail], summary="论文详情")
async def get_paper(paper_id: int, session: SessionDep) -> ApiResponse[PaperDetail]:
    paper = await _get_paper(session, paper_id)
    n_chunks = int(
        (await session.execute(select(func.count()).select_from(Chunk).where(Chunk.paper_id == paper_id))).scalar_one()
    )
    detail = PaperDetail.model_validate(paper, from_attributes=True)
    detail.chunk_count = n_chunks
    return ApiResponse.ok(detail)


@router.patch("/{paper_id}", response_model=ApiResponse[PaperOut], summary="更新元数据")
async def update_paper(paper_id: int, payload: PaperUpdate, session: SessionDep) -> ApiResponse[PaperOut]:
    paper = await _get_paper(session, paper_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(paper, key, value)
    paper.semantic_hash = semantic_hash(paper.title, paper.abstract)
    await session.commit()
    await session.refresh(paper)
    return ApiResponse.ok(PaperOut.model_validate(paper))


@router.post("/{paper_id}/reindex", response_model=ApiResponse[PaperUploadResult], summary="重新解析入库")
async def reindex_paper(paper_id: int, session: SessionDep) -> ApiResponse[PaperUploadResult]:
    paper = await _get_paper(session, paper_id)
    if not paper.file_path or not await asyncio.to_thread(Path(paper.file_path).exists):
        raise BadRequestError("该论文没有可用的 PDF 文件，无法重新索引")

    paper.parsed_status = "pending"
    paper.error = None
    await session.commit()
    await session.refresh(paper)

    from app.indexing.runner import spawn_index

    spawn_index(paper.id)
    return ApiResponse.ok(PaperUploadResult(paper=PaperOut.model_validate(paper), index_started=True))


@router.get("/{paper_id}/chunks", response_model=ApiResponse[list[ChunkOut]], summary="分块内容")
async def list_chunks(
    paper_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ApiResponse[list[ChunkOut]]:
    rows = (
        (await session.execute(select(Chunk).where(Chunk.paper_id == paper_id).order_by(Chunk.id).limit(limit)))
        .scalars()
        .all()
    )
    return ApiResponse.ok([ChunkOut.model_validate(r) for r in rows])


@router.get("/{paper_id}/versions", summary="arXiv 版本谱系")
async def list_versions(paper_id: int, session: SessionDep) -> ApiResponse[list[dict[str, Any]]]:
    """看这篇论文历史上见过哪些版本 —— 用于判断"我手上的 v1 是不是已经过时了"。"""
    await _get_paper(session, paper_id)
    rows = (
        (await session.execute(select(PaperVersion).where(PaperVersion.paper_id == paper_id).order_by(PaperVersion.id)))
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        [
            {
                "id": r.id,
                "arxiv_id": r.arxiv_id,
                "version": r.version,
                "semantic_hash": r.semantic_hash,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    )


@router.get("/{paper_id}/file", summary="原始 PDF（供 PDF.js 取流）")
async def get_pdf(paper_id: int, session: SessionDep) -> FileResponse:
    paper = await _get_paper(session, paper_id)
    if not paper.file_path:
        raise NotFoundError(f"论文没有 PDF: {paper_id}")
    path = Path(paper.file_path)
    # pathlib 是阻塞调用，放进线程池，别卡住事件循环（ASYNC240）
    if not await asyncio.to_thread(path.exists):
        raise NotFoundError("PDF 文件已丢失")
    return FileResponse(path, media_type="application/pdf", filename=f"{(paper.title or str(paper_id))[:80]}.pdf")


@router.delete("/{paper_id}", response_model=ApiResponse[dict[str, Any]], summary="删除论文")
async def delete_paper(paper_id: int, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    paper = await _get_paper(session, paper_id)
    file_path = paper.file_path

    # Milvus 与 PG 不是同一事务，先删向量再删行 —— 反过来的话中途失败会留下幽灵向量
    try:
        await asyncio.to_thread(get_store().delete_paper, paper_id)
    except Exception as exc:  # noqa: BLE001 - Milvus 不可用不该阻塞删除
        logger.warning("删除 Milvus 向量失败 paper_id={}: {}", paper_id, exc)

    await session.execute(delete(Chunk).where(Chunk.paper_id == paper_id))
    await session.delete(paper)  # chunks/citations/paper_versions 由 FK CASCADE 收尾
    await session.commit()

    if file_path:
        await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
    return ApiResponse.ok({"deleted": paper_id})


__all__ = ["router"]

# ConflictError 保留在导入里：上传路径的重复判定走"复用"分支，
# 但手工登记重复 DOI/arxiv_id 的场景后续会用到它。
_ = ConflictError
