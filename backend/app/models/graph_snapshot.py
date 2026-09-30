"""引文图谱快照 —— 把"某次分析看到的那张图"冻下来。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IntPKMixin


class GraphSnapshot(IntPKMixin, CreatedAtMixin, Base):
    """一次图谱分析的快照。

    为什么不每次现算：图分析是 NetworkX 全图计算（pagerank / louvain），
    论文多了以后秒级起步；而"昨天那份综述里引的那张社区图"必须能原样复现。
    `paper_ids` 是当时参与计算的论文集合 —— 少了它，图数据就成了无法解释的孤证。

    `graph_data` 形态：{"nodes": [{id,title,year,community,pagerank}], "edges": [{source,target}]}
    `insights` 形态：[{"kind": "pagerank|community|path", "title": str, "summary": str, "payload": {}}]
    """

    __tablename__ = "graph_snapshots"
    __table_args__ = (Index("ix_graph_snapshots_user_created", "user_id", "created_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    graph_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    insights: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)

    def __repr__(self) -> str:
        n = len((self.graph_data or {}).get("nodes") or [])
        return f"<GraphSnapshot {self.id} nodes={n}>"


__all__ = ["GraphSnapshot"]
