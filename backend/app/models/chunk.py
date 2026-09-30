"""论文切块 —— 检索的最小单元，也是引用溯源的最小粒度。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IntPKMixin

if TYPE_CHECKING:
    from app.models.paper import Paper

#: 与数据库 CHECK 约束保持一致，改这里必须同步改迁移
CHUNK_TYPES = ("text", "formula", "table", "figure_caption")


class Chunk(IntPKMixin, CreatedAtMixin, Base):
    """一个 chunk。向量存在 Milvus，不落 Postgres —— 避免双写不一致时无从判断谁是真源。

    `bbox` 是 **归一化** 坐标 `[x0, y0, x1, y1]`（各分量 ∈ [0,1]），不是像素：
    存像素的话用户一缩放高亮框就飘。类型是 JSONB 而非定长数组，因为一个表格块
    可能横跨两个文本框，需要 `{"page": n, "boxes": [[...], [...]]}` 这种多区域形态。
    """

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint(
            "chunk_type IN ('text', 'formula', 'table', 'figure_caption')",
            name="ck_chunks_chunk_type",
        ),
        Index("ix_chunks_paper_page", "paper_id", "page"),
    )

    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)

    section: Mapped[str | None] = mapped_column(String(512))  # 归一化章节名，如 related_work
    page: Mapped[int | None] = mapped_column(Integer)  # 1-based；前端据此跳 PDF 页码
    bbox: Mapped[Any | None] = mapped_column(JSONB)  # 归一化定位框，见类文档
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")

    paper: Mapped[Paper] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<Chunk {self.id} paper={self.paper_id} p.{self.page} {self.chunk_type}>"


__all__ = ["CHUNK_TYPES", "Chunk"]
