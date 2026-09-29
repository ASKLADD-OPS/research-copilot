"""论文切块 —— 检索的最小单元，也是引用溯源的最小粒度。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.paper import Paper


class PaperChunk(UUIDMixin, TimestampMixin, Base):
    """一个 chunk。

    `milvus_id` 是 Milvus 侧主键（= chunk.id），两边靠它对齐；
    向量本身存在 Milvus，不落 Postgres，避免双写不一致时无从判断谁是真源。
    """

    __tablename__ = "paper_chunks"

    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    section_id: Mapped[str | None] = mapped_column(ForeignKey("paper_sections.id", ondelete="SET NULL"))

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    # 溯源定位：章节名 + 页码 + 段内偏移，前端据此高亮 PDF 原文
    section_name: Mapped[str | None] = mapped_column(String(512))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    char_offset: Mapped[int | None] = mapped_column(Integer)

    # Milvus 主键（= id，冗余存一份便于重建索引时对账）
    milvus_id: Mapped[str | None] = mapped_column(String(64), index=True)
    embedding_model: Mapped[str | None] = mapped_column(String(64))

    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    paper: Mapped[Paper] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunks_paper_index", "paper_id", "chunk_index", unique=True),)

    def __repr__(self) -> str:
        return f"<PaperChunk {self.id[:8]} paper={self.paper_id[:8]} #{self.chunk_index}>"
