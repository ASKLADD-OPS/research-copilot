"""阶段 10：`papers.year / venue / citation_count` —— 引文图的节点属性。

引文图的节点要显示"哪一年、发在哪、被引多少次"，这三项原本无处落库：
`papers` 只存了标题/作者/摘要，`citations` 存的是边。放在构建时现拉
Semantic Scholar 是可行的，但每画一次图就打一轮外网（20 篇 = 40 次请求），
而这三项一周内几乎不变 —— 落一列缓存下来，比"每次现算"便宜得多。

三列都可空，**不用 0 当未知**：被引 0 次是真实语义（确实没人引过），
和"还没查到"必须能区分，否则度数排序会把一批未知的排到最后冒充低影响。

    alembic upgrade head      # 加三列
    alembic downgrade -1      # 回退

Revision ID: 0004_paper_biblio_fields
Revises: 0003_subscriptions
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_paper_biblio_fields"
down_revision = "0003_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("year", sa.Integer(), nullable=True))
    op.add_column("papers", sa.Column("venue", sa.String(length=255), nullable=True))
    op.add_column("papers", sa.Column("citation_count", sa.Integer(), nullable=True))
    # 时间轴过滤与"按年份取核心子图"都要按年份扫，单独建索引
    op.create_index("ix_papers_year", "papers", ["year"])


def downgrade() -> None:
    op.drop_index("ix_papers_year", table_name="papers")
    op.drop_column("papers", "citation_count")
    op.drop_column("papers", "venue")
    op.drop_column("papers", "year")
