"""阶段 7：`papers.duplicate_of` —— 跨库重复时指向正主那一篇。

为什么需要这一列：`UNIQUE(user_id, file_hash)` 只挡得住"同一份 PDF 传两次"。
同一篇论文从不同来源进来（arXiv 镜像 / 出版社站 / 第三方库）字节不同、`arxiv_id`
也不一定有，内容哈希完全失灵；而"同一篇论文在库里出现两遍"会让每段正文被召回
两次、引用列表里出现两条一模一样的条目。判定靠摘要向量（见
`app/parsers/semantic_dedup.py`），落点需要一列 —— 就是这一列。

刻意**不复用** `paper_versions`：那张表是"同一篇论文的版本谱系"（`arxiv_id`
是主键的一部分），跨库重复没有可比的 `arxiv_id`，塞进去会让版本查询长出一堆
假版本。

    alembic upgrade head      # 加列 + 索引
    alembic downgrade -1      # 回退

Revision ID: 0002_paper_duplicate_of
Revises: 0001_initial
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_paper_duplicate_of"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("duplicate_of", sa.BigInteger(), nullable=True))
    # SET NULL 而不是 CASCADE：正主被删时，被合并的那一篇仍然是完整可用的论文
    # （它有自己的 chunks 与 PDF），不该跟着消失。
    op.create_foreign_key(
        "fk_papers_duplicate_of_papers",
        "papers",
        "papers",
        ["duplicate_of"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_papers_duplicate_of", "papers", ["duplicate_of"])


def downgrade() -> None:
    op.drop_index("ix_papers_duplicate_of", table_name="papers")
    op.drop_constraint("fk_papers_duplicate_of_papers", "papers", type_="foreignkey")
    op.drop_column("papers", "duplicate_of")
