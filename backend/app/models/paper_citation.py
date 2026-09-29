"""引文边表 —— 引文网络分析的唯一数据来源。

存"谁引了谁"，而不是存一张完整图。理由：Milvus 管向量、Postgres 管关系，
NetworkX 在内存里算 —— 万篇量级下这是最省事且够用的组合，不需要图数据库。

`cited_paper_id` 可空：参考文献里的论文多半**不在本库**（外部论文），
此时只保留 `cited_title` / `cited_doi` 等原始信息，等该论文入库后再回填。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


class PaperCitation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "paper_citations"
    __table_args__ = (
        Index("ix_citations_citing", "citing_paper_id"),
        Index("ix_citations_cited", "cited_paper_id"),
    )

    citing_paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False)
    # 被引论文若也在库中则填，否则为 NULL（外部引用）
    cited_paper_id: Mapped[str | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"), nullable=True)

    # ---- 被引论文的原始标识（来自参考文献条目，未入库时唯一可用的线索）----
    cited_title: Mapped[str | None] = mapped_column(Text)
    cited_authors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    cited_year: Mapped[int | None] = mapped_column(Integer)
    cited_doi: Mapped[str | None] = mapped_column(String(255), index=True)
    cited_arxiv_id: Mapped[str | None] = mapped_column(String(64))

    # 引用发生在正文哪个位置（"第 3 页，section=related_work"）
    section: Mapped[str | None] = mapped_column(String(512))
    page: Mapped[int | None] = mapped_column(Integer)
    raw_text: Mapped[str | None] = mapped_column(Text)  # 参考文献原文

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PaperCitation {self.citing_paper_id} -> {self.cited_paper_id or self.cited_title!r}>"


__all__ = ["PaperCitation"]
