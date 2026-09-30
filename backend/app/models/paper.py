"""论文与其版本谱系。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IntPKMixin

if TYPE_CHECKING:
    from app.models.chunk import Chunk


class Paper(IntPKMixin, CreatedAtMixin, Base):
    """一篇论文。

    两条去重/识别线，别混用：

    - **内容去重**：`UNIQUE (user_id, file_hash)`。同一份 PDF 传两次只留一行。
    - **版本识别**：`(arxiv_id, version)`。arXiv 论文会出 v1/v2/v3，它们是
      *同一篇论文的不同版本*，不是重复 —— 用 `semantic_hash` 判断版本之间
      是否只是措辞变化，用 `paper_versions` 表记录谱系。
    """

    __tablename__ = "papers"
    __table_args__ = (
        UniqueConstraint("user_id", "file_hash", name="uq_papers_user_file_hash"),
        Index("ix_papers_arxiv_version", "arxiv_id", "version"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # ---- 书目信息 ----
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    abstract: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    version: Mapped[str | None] = mapped_column(String(16))  # v1 / v2 …；非 arXiv 论文为空
    source_url: Mapped[str | None] = mapped_column(Text)  # 落地页 / 下载地址

    # ---- 文件与哈希 ----
    file_path: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # 字节级 sha256 → 内容去重
    semantic_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # 语义指纹 → 版本差异判定

    # ---- 处理状态 ----
    # pending → parsing → chunking → embedding → ready | failed
    parsed_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)

    # ---- 规格外补充（既有模块依赖，详见 docs/数据持久层.md）----
    parser: Mapped[str | None] = mapped_column(String(64))  # 实际生效的解析器，MinerU 降级时靠它辨认
    page_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="Chunk.id"
    )

    def __repr__(self) -> str:
        return f"<Paper {self.id} {self.title[:40]!r} status={self.parsed_status}>"


class PaperVersion(IntPKMixin, CreatedAtMixin, Base):
    """arXiv 版本谱系：(arxiv_id, version) → paper_id。

    `Paper.arxiv_id/version` 只记「当前入库的是哪一版」；这张表记「历史上见过哪些版」。
    先同步 v1、后来拿 v2 覆盖时 v1 那一行留着 —— 否则"这篇论文改了什么"就无从追溯。
    """

    __tablename__ = "paper_versions"
    __table_args__ = (UniqueConstraint("arxiv_id", "version", name="uq_paper_versions_arxiv_version"),)

    arxiv_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    semantic_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    def __repr__(self) -> str:
        return f"<PaperVersion {self.arxiv_id}{self.version} paper={self.paper_id}>"


__all__ = ["Paper", "PaperVersion"]
