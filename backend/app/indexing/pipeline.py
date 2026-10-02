"""解析入库流水线：MinerU 解析 PDF → 分块 → bge-m3 双向量 → Milvus + PostgreSQL。

**同步实现**：嵌入模型与 Milvus 客户端都是同步阻塞的，包成 async 只是给阻塞调用
套一层协程外壳，反而让"阻塞到底发生在哪"更难看清。调用方（`app.indexing.runner`）
用 `asyncio.to_thread` 把它挪出事件循环即可。

一次入库做五件事
----------------
1. 解析 + 分块 → `chunks`（带页码、归一化 bbox、chunk_type；公式与图注各成一块）
2. **语义去重**（写向量之前）：同一篇 arXiv 论文的另一版本只记谱系不合并；
   跨库重复（摘要向量 ≥ 0.95）合并到已有那一篇，**不重复写向量**（见下方注释）
3. bge-m3 双向量 → Milvus `paper_chunks`（id = chunk_id，PG 与向量库一一对齐）
4. 论文级摘要向量 → Milvus `paper_summaries`（供语义去重与论文级检索）
5. 参考文献 → `citations` 边表；`paper_versions` 记版本谱系 + 语义指纹

**为什么去重要在写向量之前**：合并的语义是"库里只有一份向量"。先写再删的话
（delete_paper + 回滚）中间会出现"两个 paper_id 指向同一段正文"的窗口，
检索侧会短暂召回重复项；而且失败时留下的幽灵向量没人收。
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
from app.parsers import chunk_document, parse_pdf, resolve_duplicate, semantic_fingerprint
from app.parsers.pdf import parse_reference_metadata

_WS_RE = re.compile(r"\s+")


# ------------------------------------------------------------------ 指纹
def semantic_hash(title: str | None, abstract: str | None) -> str:
    """语义指纹：对规范化后的 (title + abstract) 取 sha256。

    这是**廉价近似**，不是真的语义哈希 —— 措辞一变（"we propose" → "this paper
    presents"）它就变了。它的用途是版本谱系里的"这版是不是换了个说法"的初筛，
    真正判定近似重复靠 `paper_summaries` 的向量相似度（见 `find_duplicate_paper`）。
    之所以两者都留：指纹便宜且可索引，向量贵但准。

    与 `semantic_dedup.semantic_fingerprint`（md5(标题向量+摘要向量+作者)）是
    两个东西：这个是**落 `papers.semantic_hash` 列**的文本指纹（不依赖嵌入模型，
    所以 HTTP 层随叫随算）；那个要把两段文本喂进模型，只在入库流水线里有。
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
            "authors": row.authors or [],
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

    # 1) 解析（MinerU 首选，失败自动降级 PyMuPDF → pdfplumber → pypdf；扫描件走 OCR）
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

    # 论文级摘要向量：**先算**，去重要用它（也是 paper_summaries 的内容）
    summary_text = f"{title}\n\n{paper.get('abstract') or ''}".strip()
    summary_dense = embedder.encode_dense([summary_text])[0] if summary_text else None

    # 3) 语义去重（写向量之前 —— 理由见模块头）
    verdict = resolve_duplicate(
        paper_id,
        summary_dense,
        arxiv_id=paper.get("arxiv_id"),
        version=paper.get("version"),
    )

    chunk_ids = asyncio.run(_persist_chunks(paper_id, chunks))

    fingerprint = semantic_fingerprint(
        title,
        paper.get("abstract"),
        paper.get("authors"),
        dense_fn=lambda texts: embedder.encode_dense(texts),
    )

    if verdict.is_duplicate:
        # 合并：chunks 留在 PG（这一份 PDF 仍可读、可高亮），但不写向量 ——
        # 向量库里只有正主那一份，检索不会召回两条一样的正文。
        asyncio.run(
            _set_paper(
                paper_id,
                parsed_status="ready",
                error=None,
                duplicate_of=verdict.canonical_paper_id,
                semantic_hash=semantic_hash(title, paper.get("abstract")),
            )
        )
        asyncio.run(_persist_version(paper_id, paper.get("arxiv_id"), paper.get("version"), fingerprint=fingerprint))
        logger.info(
            "去重合并 paper_id={} → canonical={} chunks={} sim={:.4f}",
            paper_id,
            verdict.canonical_paper_id,
            len(chunks),
            verdict.similarity,
        )
        return {
            "ok": True,
            "paper_id": paper_id,
            "parser": doc.parser,
            "pages": doc.page_count,
            "chunks": len(chunks),
            "upserted": 0,
            "duplicate_of": verdict.canonical_paper_id,
            "dedup": verdict.as_dict(),
        }

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
    n_summary = _upsert_summary(paper_id, summary_dense)

    # 5) 参考文献 → 引文边 + 版本谱系
    n_citations = asyncio.run(_persist_citations(paper_id, doc.references))
    asyncio.run(_persist_version(paper_id, paper.get("arxiv_id"), paper.get("version"), fingerprint=fingerprint))

    asyncio.run(
        _set_paper(
            paper_id,
            parsed_status="ready",
            error=None,
            duplicate_of=None,
            semantic_hash=semantic_hash(title, paper.get("abstract")),
        )
    )

    logger.info(
        "入库完成 paper_id={} parser={} chunks={} upsert={} summary={} citations={} dedup={}",
        paper_id,
        doc.parser,
        len(chunks),
        n_upsert,
        n_summary,
        n_citations,
        verdict.kind,
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
        "dedup": verdict.as_dict(),
    }


def _upsert_summary(paper_id: int, dense: list[float] | None) -> int:
    """摘要向量。`id` 用 paper_id 本身 —— 一篇论文一条摘要，天然是一一对应。

    向量由调用方算好传进来（去重那一关已经用过它），不在这里重复一次前向。
    """
    if not dense:
        return 0
    try:
        return get_store().upsert_summaries([{"id": paper_id, "paper_id": paper_id, "dense": dense}])
    except Exception as exc:  # noqa: BLE001 - 摘要向量是增强项，不该拖垮整篇入库
        logger.warning("论文摘要向量写入失败 paper_id={}: {}", paper_id, exc)
        return 0


async def _persist_chunks(paper_id: int, chunks: list[Any]) -> list[int]:
    """写 chunk 行并返回自增主键（顺序与入参一致）。

    `bbox` 来自版面块（归一化坐标），拿不到时是 None —— 列一直备着，
    坐标缺失不该阻塞入库，前端只是画不出高亮框。
    """
    async with session_scope() as session:
        await session.execute(delete(Chunk).where(Chunk.paper_id == paper_id))
        rows = [
            Chunk(
                paper_id=paper_id,
                section=c.section,
                page=c.page_start,
                bbox=list(c.bbox) if c.bbox else None,
                content=c.content,
                token_count=c.token_count,
                chunk_type=c.chunk_type or "text",
            )
            for c in chunks
        ]
        session.add_all(rows)
        await session.flush()  # 拿到 Identity 主键
        return [int(r.id) for r in rows]


async def _persist_version(
    paper_id: int,
    arxiv_id: str | None,
    version: str | None,
    *,
    fingerprint: str | None = None,
) -> None:
    """记录 arXiv 版本谱系。非 arXiv 论文（没有 arxiv_id）直接跳过。

    `semantic_hash` 存语义指纹（见 `semantic_dedup.semantic_fingerprint`）：
    同一篇的两个版本指纹不同，就能回答"v7 到底改没改内容"，而不只是"版本号变了"。
    """
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
            if fingerprint:
                existing.semantic_hash = fingerprint
            return
        session.add(
            PaperVersion(
                arxiv_id=arxiv_id,
                version=ver,
                paper_id=paper_id,
                semantic_hash=fingerprint,
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
    """**只看相似度**的粗查询：返回疑似重复的 paper_id（无则 None）。

    阈值默认 0.95：bge-m3 对同一篇论文的不同版本（v1 vs v2）相似度通常在 0.97 以上，
    而同一主题的两篇不同论文很少超过 0.93 —— 0.95 落在两个分布之间。

    它**不区分"新版本"与"跨库重复"**，所以入库流水线不用它，用的是
    `semantic_dedup.resolve_duplicate()`（会回表比对 arxiv_id/version 再下结论）。
    这里保留是因为它只碰 Milvus、不碰 PG，适合做单点探测。
    """
    from app.db.milvus import asearch_summaries

    hits = await asearch_summaries(dense, limit=1)
    if hits and hits[0].score >= threshold:
        return hits[0].paper_id
    return None


__all__ = ["find_duplicate_paper", "index_paper", "rebuild_citation_edges", "semantic_hash"]
