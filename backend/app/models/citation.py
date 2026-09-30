"""引文边表 —— 引文网络分析的唯一数据来源。

存「谁引了谁」，而不是存一张完整图。Milvus 管向量、Postgres 管关系、
NetworkX 在内存里算 —— 万篇量级下这是最省事且够用的组合，不需要图数据库。

`target_paper_id` 可空：参考文献里的论文多半 **不在本库**（外部论文），
此时只保留 `target_title` / `target_doi`，等该论文入库后再回填。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IntPKMixin


class Citation(IntPKMixin, CreatedAtMixin, Base):
    __tablename__ = "citations"
    __table_args__ = (
        Index("ix_citations_source", "source_paper_id"),
        Index("ix_citations_target", "target_paper_id"),
    )

    source_paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False)
    # 被引论文若也在库中则填，否则为 NULL（外部引用）
    target_paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id", ondelete="SET NULL"))

    # ---- 被引论文的原始标识（来自参考文献条目，未入库时唯一可用的线索）----
    target_title: Mapped[str | None] = mapped_column(Text)
    target_doi: Mapped[str | None] = mapped_column(String(255), index=True)

    # 引用出现在正文哪儿。片段而非整条参考文献 —— 溯源面板要显示的是"这句话出自哪"
    context_snippet: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Citation {self.source_paper_id} -> {self.target_paper_id or self.target_title!r}>"


__all__ = ["Citation"]
