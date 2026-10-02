"""阶段 9：`subscriptions` —— 定时抓取订阅。

每天 8:00 由进程内调度器（`app/workers/scheduler.py`）遍历 `enabled=true` 的行，
对每个主题跑一遍探索闭环（检索 → 下载 → 解析入库 → 评分），把结果写回
`last_*` 五个字段。**没有 Celery / 没有任务表**：那张"上次跑成什么样"的表
就是这张订阅表本身。

    alembic upgrade head      # 建表 + 索引
    alembic downgrade -1      # 回退

Revision ID: 0003_subscriptions
Revises: 0002_paper_duplicate_of
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_subscriptions"
down_revision = "0002_paper_duplicate_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("max_papers", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("min_score", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_subscriptions_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    # 调度器唯一的查询：enabled 为真有哪些。复合索引让这次全表扫描变成索引扫描。
    op.create_index("ix_subscriptions_enabled_topic", "subscriptions", ["enabled", "topic"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_enabled_topic", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
