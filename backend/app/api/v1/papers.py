"""论文管理：上传 / 批量上传 / arXiv 直取 / 列表 / 详情 / 更新 / 删除 / 重新索引 / 分块 / 原文。

两个 id 相关的硬性约定（阶段 1 规格决定，改动前先看 models/base.py 的说明）：
- 主键是 **int64**（`BigInteger`），不是 UUID —— 只为与 Milvus 的 INT64 对齐；
- 因此路由参数是 `int`，传 UUID 字符串会直接被 FastAPI 判 422。

上传的三条入口
--------------
| 入口 | 用途 | 异步方式 |
|---|---|---|
| `POST /papers/upload`（文件）| 单篇本地 PDF | `spawn_index` 进程内线程池 |
| `POST /papers/upload`（`arxiv_url`）| arXiv 链接/编号，先经 MCP 工具下载 | 同上 |
| `POST /papers/batch-upload` | 批量，逐条独立成败 | 每篇一个 `spawn_index` |

**没有 Celery**：`app/indexing/runner.py` 用 `asyncio.to_thread` 在进程内跑流水线，
进度写 `papers.parsed_status`，前端轮询 `GET /papers/{id}`。代价写在那个模块头上
（进程重启 = 在跑的任务丢失）。批量上传靠"每篇一个后台任务"拿到并发，
不需要 broker 也就不需要为它维护一个 worker 容器。
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
from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ToolError,
    UnsupportedFileTypeError,
)
from app.db.bootstrap import ensure_default_user
from app.db.milvus import get_store
from app.indexing.pipeline import semantic_hash
from app.models import Chunk, Paper, PaperVersion
from app.schemas import (
    ApiResponse,
    BatchUploadResult,
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


async def _schedule(session: AsyncSession, paper: Paper, *, index: bool) -> PaperUploadResult:
    """提交这一行并（可选地）排后台解析。

    **必须先 commit 再排任务**：后台协程用另一个会话，没提交它读不到这行，
    表现是任务报"论文不存在"而 API 返回 200 —— 最难查的那种。
    """
    await session.commit()
    if index:
        from app.indexing.runner import spawn_index

        spawn_index(paper.id)
    await session.refresh(paper)
    return _result(paper, index_started=index)


def _result(paper: Paper, *, index_started: bool) -> PaperUploadResult:
    return PaperUploadResult(paper=PaperOut.model_validate(paper), index_started=index_started)


async def _ingest_pdf(session: AsyncSession, file: UploadFile, title: str, index: bool) -> PaperUploadResult:
    """单篇 PDF 入库（上传与批量上传共用这一段）。"""
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
        return _result(existing, index_started=False)

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

    result = await _schedule(session, paper, index=index)
    logger.info("上传完成 paper_id={} size={} index={}", paper.id, size, result.index_started)
    return result


async def _ingest_arxiv(session: AsyncSession, url: str, title: str, index: bool) -> PaperUploadResult:
    """arXiv 链接/编号 → 经 MCP 工具下载 PDF → 与本地 PDF 走同一条解析链路。

    下载必须走 MCP（`arxiv_fetch`），不在这里直连 HTTP —— "外部工具统一 MCP"
    这条约束的意义就是：网络出口只有一处，超时/重试/UA/体积极限都由那一层管。
    """
    from app.agents.mcp.registry import call_tool
    from app.mcp_servers.arxiv_server import parse_arxiv_ref

    try:
        arxiv_id, version = parse_arxiv_ref(url)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    user_id = await ensure_default_user(session)
    # 先查库：同一 (arxiv_id, version) 收过就别再下一遍（几十 MB + 一次外网往返）
    existing = (
        (await session.execute(select(Paper).where(Paper.arxiv_id == arxiv_id, Paper.version == version)))
        .scalars()
        .first()
    )
    if existing is not None:
        return _result(existing, index_started=False)

    fetched = await call_tool(
        "arxiv_fetch", {"arxiv_id": f"{arxiv_id}{version}", "dest_dir": str(settings.upload_path)}
    )
    if isinstance(fetched, str) or not isinstance(fetched, dict) or not fetched.get("pdf_path"):
        # 工具被关掉（MCP_ARXIV_ENABLED=false）/ 网络不通 / 编号不存在都落到这里
        raise ToolError(f"arXiv 下载失败：{fetched if isinstance(fetched, str) else '工具未返回文件路径'}")

    file_path = Path(str(fetched["pdf_path"]))
    file_hash = str(fetched.get("sha256") or "")
    if not file_hash:  # 工具没给哈希就自己算，内容去重靠它
        file_hash = hashlib.sha256(await asyncio.to_thread(file_path.read_bytes)).hexdigest()

    duplicate = (
        await session.execute(select(Paper).where(Paper.user_id == user_id, Paper.file_hash == file_hash))
    ).scalar_one_or_none()
    if duplicate is not None:
        await asyncio.to_thread(file_path.unlink, missing_ok=True)
        return _result(duplicate, index_started=False)

    paper = Paper(
        user_id=user_id,
        title=title or str(fetched.get("title") or "") or f"{arxiv_id}{version}",
        authors=[{"name": name} for name in (fetched.get("authors") or [])],
        abstract=fetched.get("abstract") or None,
        doi=None,
        arxiv_id=arxiv_id,
        version=version,
        source_url=str(fetched.get("url") or f"https://arxiv.org/abs/{arxiv_id}{version}"),
        file_path=str(file_path),
        file_hash=file_hash,
        parsed_status="pending",
    )
    session.add(paper)
    await session.flush()
    logger.info("arXiv 下载完成 {} {} → paper_id={} bytes={}", arxiv_id, version, paper.id, fetched.get("bytes"))
    return await _schedule(session, paper, index=index)


@router.post(
    "/upload", response_model=ApiResponse[PaperUploadResult], summary="上传 PDF 并入库", operation_id="upload_paper"
)
async def upload_paper(
    session: SessionDep,
    file: Annotated[UploadFile | None, File(description="PDF 文件")] = None,
    title: Annotated[str, Form()] = "",
    index: Annotated[bool, Form(description="是否立即解析入库")] = True,
    arxiv_url: Annotated[str, Form(description="arXiv 链接或编号；给了它就不必传文件")] = "",
) -> ApiResponse[PaperUploadResult]:
    """上传单篇 PDF；或给一个 arXiv 链接，由服务端下载后入库。"""
    if arxiv_url.strip():
        result = await _ingest_arxiv(session, arxiv_url, title, index)
        return ApiResponse.ok(
            result,
            message=_upload_message(result, f"已收录 arXiv {arxiv_url.strip()}"),
        )
    if file is None or not file.filename:
        raise BadRequestError("需要上传 PDF 文件，或提供 arxiv_url")

    result = await _ingest_pdf(session, file, title, index)
    return ApiResponse.ok(result, message=_upload_message(result, "已入库"))


@router.post(
    "/batch-upload",
    response_model=ApiResponse[BatchUploadResult],
    summary="批量上传 PDF（每篇一个后台解析任务）",
    operation_id="batch_upload_papers",
)
async def batch_upload_papers(
    session: SessionDep,
    files: Annotated[list[UploadFile], File(description="多个 PDF 文件")],
    index: Annotated[bool, Form(description="是否立即解析入库")] = True,
) -> ApiResponse[BatchUploadResult]:
    """批量上传。**逐条独立成败** —— 第 7 篇坏掉不该把前 6 篇一起回滚。"""
    if not files:
        raise BadRequestError("至少上传一个文件")
    if len(files) > settings.BATCH_UPLOAD_MAX_FILES:
        raise BadRequestError(f"一次最多 {settings.BATCH_UPLOAD_MAX_FILES} 篇，收到 {len(files)}")

    items: list[PaperUploadResult] = []
    failed: list[dict[str, Any]] = []
    for upload in files:
        name = upload.filename or "(未命名)"
        try:
            result = await _ingest_pdf(session, upload, "", index)
        except AppError as exc:
            # 会话可能停在中途失败的事务里，先回滚再继续下一篇
            await session.rollback()
            failed.append({"filename": name, "error": exc.message})
            logger.warning("批量上传跳过 {}: {}", name, exc.message)
            continue
        except Exception as exc:  # noqa: BLE001 - 单个文件不该让整批 500
            await session.rollback()
            failed.append({"filename": name, "error": f"{type(exc).__name__}: {exc}"})
            logger.exception("批量上传异常 {}", name)
            continue
        items.append(result)

    payload = BatchUploadResult(
        items=items,
        failed=failed,
        accepted=len(items),
        started=sum(1 for r in items if r.index_started),
    )
    return ApiResponse.ok(
        payload,
        message=f"已受理 {payload.accepted} 篇（{payload.started} 篇开始解析），失败 {len(failed)} 篇",
    )


def _upload_message(result: PaperUploadResult, prefix: str) -> str:
    """统一"上传完到底发生了什么"的提示。

    复用（内容重复）与排了任务要分开说：前者用户不需要轮询，后者需要。
    """
    if result.index_started:
        return f"{prefix}，后台解析中（轮询 GET /papers/{result.paper.id} 看 parsed_status）"
    return f"{prefix}，未重复解析（id={result.paper.id}）"


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


@router.get("", response_model=ApiResponse[Page[PaperOut]], summary="论文列表", operation_id="list_papers")
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


@router.get("/{paper_id}", response_model=ApiResponse[PaperDetail], summary="论文详情", operation_id="get_paper")
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


@router.post(
    "/{paper_id}/reindex",
    response_model=ApiResponse[PaperUploadResult],
    summary="重新解析入库",
    operation_id="reindex_paper",
)
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


@router.get(
    "/{paper_id}/chunks",
    response_model=ApiResponse[list[ChunkOut]],
    summary="分块内容",
    operation_id="get_paper_chunks",
)
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


@router.delete(
    "/{paper_id}", response_model=ApiResponse[dict[str, Any]], summary="删除论文", operation_id="delete_paper"
)
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
