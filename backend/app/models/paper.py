"""论文与其元数据。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.chunk import PaperChunk


class Paper(UUIDMixin, TimestampMixin, Base):
    """一篇论文（本地 PDF 或来自 arXiv/PubMed 的元数据）。"""

    __tablename__ = "papers"

    # ---- 标识 ----
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    abstract: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(String(512))

    # ---- 外部 ID（用于去重与跨源合并）----
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pmid: Mapped[str | None] = mapped_column(String(32), index=True)
    s2_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="upload")  # upload|arxiv|pubmed|s2

    # ---- 文件与解析 ----
    pdf_path: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    file_size: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    parser: Mapped[str | None] = mapped_column(String(64))  # mineru|pymupdf|pdfplumber|pypdf

    # ---- 处理状态 ----
    # pending → parsing → chunking → embedding → indexed | failed
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    num_chunks: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 其他 ----
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    chunks: Mapped[list[PaperChunk]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperChunk.chunk_index"
    )

    __table_args__ = (
        Index("ix_papers_status_created", "status", "created_at"),
        Index("ix_papers_title_trgm", "title"),
    )

    def __repr__(self) -> str:
        return f"<Paper {self.id[:8]} {self.title[:40]!r} status={self.status}>"


class PaperSection(UUIDMixin, TimestampMixin, Base):
    """论文的章节结构（Abstract / Intro / Method …），用于父子切块与结构化引用。"""

    __tablename__ = "paper_sections"

    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("paper_sections.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(512))  # 归一化后的章节名
    raw_heading: Mapped[str | None] = mapped_column(Text)  # 原文标题
    level: Mapped[int] = mapped_column(Integer, default=1)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
