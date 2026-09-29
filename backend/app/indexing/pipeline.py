"""解析入库流水线：MinerU 解析 PDF → 分块 → bge-m3 双向量 → Milvus + PostgreSQL。

**同步实现**：嵌入模型与 Milvus 客户端都是同步阻塞的，包成 async 只是给阻塞调用
套一层协程外壳，反而让"阻塞到底发生在哪"更难看清。调用方（`app.indexing.runner`）
用 `asyncio.to_thread` 把它挪出事件循环即可。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.db.milvus import get_store
from app.db.session import session_scope
from app.embeddings.bge_m3 import get_embedder
from app.models import Paper, PaperChunk, PaperCitation
from app.models.base import new_uuid
from app.parsers import chunk_document, parse_pdf
from app.parsers.pdf import parse_reference_metadata


# ------------------------------------------------------------------ DB 小工具
async def _set_paper(paper_id: str, **fields: Any) -> None:
    async with session_scope() as session:
        await session.execute(update(Paper).where(Paper.id == paper_id).values(**fields))


async def _load_paper(paper_id: str) -> dict[str, Any] | None:
    async with session_scope() as session:
        row = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one_or_none()
        if row is None:
            return None
        return {"id": row.id, "title": row.title, "pdf_path": row.pdf_path, "meta": dict(row.meta or {})}


# ------------------------------------------------------------------ 主流程
def index_paper(paper_id: str) -> dict[str, Any]:
    """把一篇已登记的论文灌进检索库。失败时把错误写回 papers.error，不抛异常。"""
    try:
        return _index_paper_inner(paper_id)
    except Exception as exc:  # noqa: BLE001 - 任务失败要让 API 能读到原因
        logger.exception("论文入库失败 paper_id={}", paper_id)
        asyncio.run(_set_paper(paper_id, status="failed", error=f"{type(exc).__name__}: {exc}"))
        return {"ok": False, "paper_id": paper_id, "error": str(exc)}


def _index_paper_inner(paper_id: str) -> dict[str, Any]:
    paper = asyncio.run(_load_paper(paper_id))
    if paper is None:
        return {"ok": False, "paper_id": paper_id, "error": "论文不存在"}
    pdf_path = paper.get("pdf_path")
    if not pdf_path:
        asyncio.run(_set_paper(paper_id, status="failed", error="没有 PDF 文件路径"))
        return {"ok": False, "paper_id": paper_id, "error": "没有 PDF 文件路径"}

    asyncio.run(_set_paper(paper_id, status="parsing", error=None))

    # 1) 解析（MinerU 首选，失败自动降级）
    doc = parse_pdf(pdf_path)
    chunks = chunk_document(doc)
    if not chunks:
        asyncio.run(_set_paper(paper_id, status="failed", error="未切出任何文本块（可能是扫描件）"))
        return {"ok": False, "paper_id": paper_id, "error": "未切出任何文本块"}

    asyncio.run(
        _set_paper(
            paper_id,
            status="indexing",
            parser=doc.parser,
            page_count=doc.page_count,
            title=paper["title"] or doc.title or "",
            meta={**paper["meta"], **doc.meta},
        )
    )

    # 2) 双向量
    embedder = get_embedder()
    texts = [c.content for c in chunks]
    vectors = embedder.encode(texts)
    if len(vectors) != len(chunks):
        raise RuntimeError(f"嵌入数量不匹配: {len(vectors)} != {len(chunks)}")

    # 3) Milvus（先清旧块，保证重复索引不产生幽灵向量）
    store = get_store()
    store.ensure_collection()
    store.delete_by_paper(paper_id)
    ids = [new_uuid() for _ in chunks]
    records = [
        {
            "id": ids[i],
            "paper_id": paper_id,
            "chunk_index": c.index,
            "section": c.section,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "content": c.content,
            "dense": vectors.dense[i],
            "sparse": vectors.sparse[i] if i < len(vectors.sparse) else {},
        }
        for i, c in enumerate(chunks)
    ]
    n_upsert = store.upsert(records)

    # 4) PostgreSQL：chunk 行 + 论文状态
    asyncio.run(_persist_chunks(paper_id, chunks, ids, vectors.backend))

    # 5) 参考文献 → 引文边
    n_citations = asyncio.run(_persist_citations(paper_id, doc.references))

    logger.info(
        "入库完成 paper_id={} parser={} chunks={} upsert={} citations={}",
        paper_id,
        doc.parser,
        len(chunks),
        n_upsert,
        n_citations,
    )
    return {
        "ok": True,
        "paper_id": paper_id,
        "parser": doc.parser,
        "pages": doc.page_count,
        "chunks": len(chunks),
        "upserted": n_upsert,
        "citations": n_citations,
        "embedding_backend": vectors.backend,
    }


async def _persist_chunks(paper_id: str, chunks: list[Any], ids: list[str], backend: str) -> None:
    async with session_scope() as session:
        await session.execute(delete(PaperChunk).where(PaperChunk.paper_id == paper_id))
        session.add_all(
            [
                PaperChunk(
                    id=ids[i],
                    paper_id=paper_id,
                    chunk_index=c.index,
                    content=c.content,
                    token_count=c.token_count,
                    char_count=len(c.content),
                    section_name=c.section,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    milvus_id=ids[i],
                    embedding_model=settings.EMBEDDING_MODEL,
                    extra={"backend": backend},
                )
                for i, c in enumerate(chunks)
            ]
        )
        await session.execute(
            update(Paper)
            .where(Paper.id == paper_id)
            .values(
                status="ready",
                num_chunks=len(chunks),
                indexed_at=datetime.now(UTC),
                error=None,
            )
        )


async def _persist_citations(paper_id: str, references: list[str]) -> int:
    """参考文献 → paper_citations。能在库内匹配到的填 cited_paper_id，否则留 NULL。"""
    if not references:
        return 0
    parsed = [parse_reference_metadata(r) for r in references]

    async with session_scope() as session:
        library = (await session.execute(select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title))).all()
        by_doi = {str(d).lower(): str(i) for i, d, _, _ in library if d}
        by_arxiv = {str(a).lower(): str(i) for i, _, a, _ in library if a}
        by_title = {str(t).strip().lower()[:80]: str(i) for i, _, _, t in library if t}

        await session.execute(delete(PaperCitation).where(PaperCitation.citing_paper_id == paper_id))
        rows: list[PaperCitation] = []
        for item in parsed:
            cited_id = None
            if item["doi"]:
                cited_id = by_doi.get(item["doi"].lower())
            if not cited_id and item["arxiv_id"]:
                cited_id = by_arxiv.get(item["arxiv_id"].lower())
            if not cited_id and item["title"]:
                cited_id = by_title.get(item["title"].strip().lower()[:80])
            rows.append(
                PaperCitation(
                    citing_paper_id=paper_id,
                    cited_paper_id=cited_id,
                    cited_title=item["title"] or None,
                    cited_year=str(item["year"]) if item["year"] else None,
                    cited_doi=item["doi"],
                    cited_arxiv_id=item["arxiv_id"],
                    raw_text=item["raw"][:4000],
                )
            )
        session.add_all(rows)
        return len(rows)


def rebuild_citation_edges(paper_ids: list[str] | None = None) -> dict[str, Any]:
    """只重建引文边（不改向量）。用于手工补 DOI 后重新连线。"""

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            stmt = select(PaperCitation).where(PaperCitation.cited_paper_id.is_(None))
            if paper_ids:
                stmt = stmt.where(PaperCitation.citing_paper_id.in_(paper_ids))
            pending = (await session.execute(stmt)).scalars().all()
            if not pending:
                return {"ok": True, "linked": 0, "checked": 0}

            library = (await session.execute(select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title))).all()
            by_doi = {str(d).lower(): str(i) for i, d, _, _ in library if d}
            by_arxiv = {str(a).lower(): str(i) for i, _, a, _ in library if a}
            by_title = {str(t).strip().lower()[:80]: str(i) for i, _, _, t in library if t}

            linked = 0
            for row in pending:
                target = None
                if row.cited_doi:
                    target = by_doi.get(row.cited_doi.lower())
                if not target and row.cited_arxiv_id:
                    target = by_arxiv.get(row.cited_arxiv_id.lower())
                if not target and row.cited_title:
                    target = by_title.get(row.cited_title.strip().lower()[:80])
                if target and target != row.citing_paper_id:
                    row.cited_paper_id = target
                    linked += 1
            return {"ok": True, "linked": linked, "checked": len(pending)}

    result = asyncio.run(_run())
    logger.info("引文边重建完成 {}", result)
    return result


__all__ = ["index_paper", "rebuild_citation_edges"]
