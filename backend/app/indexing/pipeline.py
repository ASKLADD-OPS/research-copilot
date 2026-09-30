"""解析入库流水线：MinerU 解析 PDF → 分块 → bge-m3 双向量 → Milvus + PostgreSQL。

**同步实现**：嵌入模型与 Milvus 客户端都是同步阻塞的，包成 async 只是给阻塞调用
套一层协程外壳，反而让"阻塞到底发生在哪"更难看清。调用方（`app.indexing.runner`）
用 `asyncio.to_thread` 把它挪出事件循环即可。

一次入库做四件事
----------------
1. 解析 + 分块 → `chunks`（同时 upsert `paper_versions` 记录 arXiv 版本谱系）
2. bge-m3 双向量 → Milvus `paper_chunks`（id = chunk_id，PG 与向量库一一对齐）
3. 论文级摘要向量 → Milvus `paper_summaries`（供语义去重与论文级检索）
4. 参考文献 → `citations` 边表
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from loguru import logger
from sqlalchemy import delete, select, update

from app.db.milvus import get_store
from app.db.session import session_scope
from app.embeddings.bge_m3 import get_embedder
from app.models import Chunk, Citation, Paper, PaperVersion
from app.parsers import chunk_document, parse_pdf
from app.parsers.pdf import parse_reference_metadata

_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ 指纹
def semantic_hash(title: str | None, abstract: str | None) -> str:
    """语义指纹：对规范化后的 (title + abstract) 取 sha256。

    这是**廉价近似**，不是真的语义哈希 —— 措辞一变（"we propose" → "this paper
    presents"）它就变了。它的用途是版本谱系里的"这版是不是换了个说法"的初筛，
    真正判定近似重复靠 `paper_summaries` 的向量相似度（见 `find_duplicate`）。
    之所以两者都留：指纹便宜且可索引，向量贵但准。
    """
    raw = _WS_RE.sub(" ", f"{title or ''} {abstract or ''}".strip().lower())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ DB 小工具
async def _set_paper(paper_id: int, **fields: Any) -> None:
    async with session_scope() as session:
        await session.execute(update(Paper).where(Paper.id == paper_id).values(**fields))


async def _load_paper(paper_id: int) -> dict[str, Any] | None:
    async with session_scope() as session:
        row = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "title": row.title,
            "file_path": row.file_path,
            "arxiv_id": row.arxiv_id,
            "version": row.version,
            "abstract": row.abstract,
            "semantic_hash": row.semantic_hash,
        }


# ------------------------------------------------------------------ 主流程
def index_paper(paper_id: int) -> dict[str, Any]:
    """把一篇已登记的论文灌进检索库。失败时把错误写回 `papers.error`，不抛异常。"""
    try:
        return _index_paper_inner(paper_id)
    except Exception as exc:  # noqa: BLE001 - 任务失败要让 API/前端能读到原因
        logger.exception("论文入库失败 paper_id={}", paper_id)
        asyncio.run(_set_paper(paper_id, parsed_status="failed", error=f"{type(exc).__name__}: {exc}"))
        return {"ok": False, "paper_id": paper_id, "error": str(exc)}


def _index_paper_inner(paper_id: int) -> dict[str, Any]:
    paper = asyncio.run(_load_paper(paper_id))
    if paper is None:
        return {"ok": False, "paper_id": paper_id, "error": "论文不存在"}
    file_path = paper.get("file_path")
    if not file_path:
        asyncio.run(_set_paper(paper_id, parsed_status="failed", error="没有 PDF 文件路径"))
        return {"ok": False, "paper_id": paper_id, "error": "没有 PDF 文件路径"}

    asyncio.run(_set_paper(paper_id, parsed_status="parsing", error=None))

    # 1) 解析（MinerU 首选，失败自动降级 PyMuPDF → pdfplumber → pypdf）
    doc = parse_pdf(file_path)
    chunks = chunk_document(doc)
    if not chunks:
        asyncio.run(_set_paper(paper_id, parsed_status="failed", error="未切出任何文本块（可能是扫描件）"))
        return {"ok": False, "paper_id": paper_id, "error": "未切出任何文本块"}

    title = paper["title"] or doc.title or ""
    asyncio.run(
        _set_paper(
            paper_id,
            parsed_status="chunking",
            parser=doc.parser,
            page_count=doc.page_count,
            title=title,
        )
    )

    # 2) 双向量（dense + learned sparse，一次前向）
    asyncio.run(_set_paper(paper_id, parsed_status="embedding"))
    embedder = get_embedder()
    vectors = embedder.encode([c.content for c in chunks])
    if len(vectors) != len(chunks):
        raise RuntimeError(f"嵌入数量不匹配: {len(vectors)} != {len(chunks)}")

    # 3) PG 先落 chunks（要拿到自增主键），再用同一批 id 写 Milvus
    chunk_ids = asyncio.run(_persist_chunks(paper_id, chunks))

    store = get_store()
    store.ensure_collections()
    store.delete_paper(paper_id)  # 先清旧向量，重复索引不产生幽灵
    n_upsert = store.upsert_chunks(
        [
            {
                "id": chunk_ids[i],
                "paper_id": paper_id,
                "chunk_id": chunk_ids[i],
                "dense": vectors.dense[i],
                "sparse": vectors.sparse[i] if i < len(vectors.sparse) else {},
            }
            for i in range(len(chunks))
        ]
    )

    # 4) 论文级摘要向量（语义去重 + 论文级检索）
    n_summary = _upsert_summary(paper_id, title, paper.get("abstract"), embedder)

    # 5) 参考文献 → 引文边 + 版本谱系
    n_citations = asyncio.run(_persist_citations(paper_id, doc.references))
    asyncio.run(_persist_version(paper_id, paper.get("arxiv_id"), paper.get("version"), title, paper.get("abstract")))

    asyncio.run(
        _set_paper(
            paper_id,
            parsed_status="ready",
            error=None,
            semantic_hash=semantic_hash(title, paper.get("abstract")),
        )
    )

    logger.info(
        "入库完成 paper_id={} parser={} chunks={} upsert={} summary={} citations={}",
        paper_id,
        doc.parser,
        len(chunks),
        n_upsert,
        n_summary,
        n_citations,
    )
    return {
        "ok": True,
        "paper_id": paper_id,
        "parser": doc.parser,
        "pages": doc.page_count,
        "chunks": len(chunks),
        "upserted": n_upsert,
        "summary_upserted": n_summary,
        "citations": n_citations,
        "embedding_backend": vectors.backend,
    }


def _upsert_summary(paper_id: int, title: str, abstract: str | None, embedder: Any) -> int:
    """摘要向量。`id` 用 paper_id 本身 —— 一篇论文一条摘要，天然是一一对应。"""
    text = f"{title}\n\n{abstract or ''}".strip()
    if not text:
        return 0
    vec = embedder.encode_dense([text])
    if not vec:
        return 0
    try:
        return get_store().upsert_summaries([{"id": paper_id, "paper_id": paper_id, "dense": vec[0]}])
    except Exception as exc:  # noqa: BLE001 - 摘要向量是增强项，不该拖垮整篇入库
        logger.warning("论文摘要向量写入失败 paper_id={}: {}", paper_id, exc)
        return 0


async def _persist_chunks(paper_id: int, chunks: list[Any]) -> list[int]:
    """写 chunk 行并返回自增主键（顺序与入参一致）。

    `bbox` 暂为 None：当前解析器（MinerU/PyMuPDF）只吐文本流，不出坐标框。
    列已经备好，等接入版面模型后直接填，不需要改表。
    """
    async with session_scope() as session:
        await session.execute(delete(Chunk).where(Chunk.paper_id == paper_id))
        rows = [
            Chunk(
                paper_id=paper_id,
                section=c.section,
                page=c.page_start,
                bbox=None,
                content=c.content,
                token_count=c.token_count,
                chunk_type="text",
            )
            for c in chunks
        ]
        session.add_all(rows)
        await session.flush()  # 拿到 Identity 主键
        return [int(r.id) for r in rows]


async def _persist_version(
    paper_id: int, arxiv_id: str | None, version: str | None, title: str, abstract: str | None
) -> None:
    """记录 arXiv 版本谱系。非 arXiv 论文（没有 arxiv_id）直接跳过。"""
    if not arxiv_id:
        return
    ver = version or "v1"
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(PaperVersion).where(PaperVersion.arxiv_id == arxiv_id, PaperVersion.version == ver)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.paper_id = paper_id
            existing.semantic_hash = semantic_hash(title, abstract)
            return
        session.add(
            PaperVersion(
                arxiv_id=arxiv_id,
                version=ver,
                paper_id=paper_id,
                semantic_hash=semantic_hash(title, abstract),
            )
        )


async def _persist_citations(paper_id: int, references: list[str]) -> int:
    """参考文献 → citations。能在库内匹配到的填 target_paper_id，否则留 NULL（外部引用）。"""
    if not references:
        return 0
    parsed = [parse_reference_metadata(r) for r in references]

    async with session_scope() as session:
        library = (await session.execute(select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title))).all()
        by_doi = {str(d).lower(): int(i) for i, d, _, _ in library if d}
        by_arxiv = {str(a).lower(): int(i) for i, _, a, _ in library if a}
        by_title = {str(t).strip().lower()[:80]: int(i) for i, _, _, t in library if t}

        await session.execute(delete(Citation).where(Citation.source_paper_id == paper_id))
        rows: list[Citation] = []
        for item in parsed:
            target = None
            if item["doi"]:
                target = by_doi.get(item["doi"].lower())
            if not target and item["arxiv_id"]:
                target = by_arxiv.get(item["arxiv_id"].lower())
            if not target and item["title"]:
                target = by_title.get(item["title"].strip().lower()[:80])
            rows.append(
                Citation(
                    source_paper_id=paper_id,
                    target_paper_id=target,
                    target_title=item["title"] or None,
                    target_doi=item["doi"],
                    context_snippet=item["raw"][:4000],
                )
            )
        session.add_all(rows)
        return len(rows)


def rebuild_citation_edges(paper_ids: list[int] | None = None) -> dict[str, Any]:
    """只重建引文边（不改向量）。用于手工补 DOI 后重新连线。"""

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            stmt = select(Citation).where(Citation.target_paper_id.is_(None))
            if paper_ids:
                stmt = stmt.where(Citation.source_paper_id.in_(paper_ids))
            pending = (await session.execute(stmt)).scalars().all()
            if not pending:
                return {"ok": True, "linked": 0, "checked": 0}

            library = (await session.execute(select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title))).all()
            # 只按 doi / title 匹配：citations 表没有 target_arxiv_id 列，
            # 引用原文里抽出来的 arXiv 号无处落库（见 app/models/citation.py）。
            by_doi = {str(d).lower(): int(i) for i, d, _, _ in library if d}
            by_title = {str(t).strip().lower()[:80]: int(i) for i, _, _, t in library if t}

            linked = 0
            for row in pending:
                target = None
                if row.target_doi:
                    target = by_doi.get(row.target_doi.lower())
                if not target and row.target_title:
                    target = by_title.get(row.target_title.strip().lower()[:80])
                if target and target != row.source_paper_id:
                    row.target_paper_id = target
                    linked += 1
            return {"ok": True, "linked": linked, "checked": len(pending)}

    result = asyncio.run(_run())
    logger.info("引文边重建完成 {}", result)
    return result


async def find_duplicate_paper(dense: list[float], threshold: float = 0.95) -> int | None:
    """用论文级摘要向量做语义去重：返回疑似重复的 paper_id（无则 None）。

    阈值默认 0.95：bge-m3 对同一篇论文的不同版本（v1 vs v2）相似度通常在 0.97 以上，
    而同一主题的两篇不同论文很少超过 0.93 —— 0.95 落在两个分布之间。
    """
    from app.db.milvus import asearch_summaries

    hits = await asearch_summaries(dense, limit=1)
    if hits and hits[0].score >= threshold:
        return hits[0].paper_id
    return None


__all__ = ["find_duplicate_paper", "index_paper", "rebuild_citation_edges", "semantic_hash"]
