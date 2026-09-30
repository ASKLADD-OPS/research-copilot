"""阶段 1 初始 schema：规格要求的 8 张表。

手写而非 autogenerate 的理由
----------------------------
`--autogenerate` 生成的是**当下模型元的快照**，同一个 schema 在不同环境下可能
产出字面不同的脚本（列顺序、索引命名、CHECK 约束的写法都会漂）。初始迁移是整个
数据层的基线，它必须逐字可复核 —— 所以这里逐列写死，并且与 `app/models/*` 一一对应。

    alembic upgrade head     # 建表
    alembic downgrade base   # 全部回退（会删数据，仅开发态用）

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# 建表顺序即依赖顺序：users → papers → paper_versions/chunks/citations → 其余
_CHUNK_TYPES = ("text", "formula", "table", "figure_caption")


def _pk() -> sa.Column:
    """统一主键：int64 自增，与 Milvus 的 INT64 主键天然对齐。"""
    return sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    # ------------------------------------------------------------ users
    op.create_table(
        "users",
        _pk(),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False, server_default=""),
        sa.Column("role", sa.String(32), nullable=False, server_default="user"),
        _created_at(),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------ papers
    op.create_table(
        "papers",
        _pk(),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("authors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("arxiv_id", sa.String(64), nullable=True),
        sa.Column("version", sa.String(16), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("file_hash", sa.String(64), nullable=True),
        sa.Column("semantic_hash", sa.String(64), nullable=True),
        sa.Column("parsed_status", sa.String(24), nullable=False, server_default="pending"),
        # 规格外补充：既有模块依赖（解析器辨认 / 页数 / 失败原因）
        sa.Column("parser", sa.String(64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_papers_user_id", ondelete="CASCADE"),
        # 内容去重：同一用户同一份文件只留一行
        sa.UniqueConstraint("user_id", "file_hash", name="uq_papers_user_file_hash"),
        # file_hash 可空时下面这些索引仍然是普通索引（PG 唯一约束里 NULL 互不相等，
        # 所以"没有文件"的元数据条目可以并存，这是刻意的）
    )
    op.create_index("ix_papers_user_id", "papers", ["user_id"])
    op.create_index("ix_papers_doi", "papers", ["doi"])
    op.create_index("ix_papers_arxiv_id", "papers", ["arxiv_id"])
    op.create_index("ix_papers_file_hash", "papers", ["file_hash"])
    op.create_index("ix_papers_semantic_hash", "papers", ["semantic_hash"])
    op.create_index("ix_papers_parsed_status", "papers", ["parsed_status"])
    # 版本识别：(arxiv_id, version) 联合索引
    op.create_index("ix_papers_arxiv_version", "papers", ["arxiv_id", "version"])

    # ------------------------------------------------------------ paper_versions
    op.create_table(
        "paper_versions",
        _pk(),
        sa.Column("arxiv_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], name="fk_paper_versions_paper_id", ondelete="CASCADE"),
        sa.UniqueConstraint("arxiv_id", "version", name="uq_paper_versions_arxiv_version"),
    )
    op.create_index("ix_paper_versions_arxiv_id", "paper_versions", ["arxiv_id"])
    op.create_index("ix_paper_versions_paper_id", "paper_versions", ["paper_id"])
    op.create_index("ix_paper_versions_semantic_hash", "paper_versions", ["semantic_hash"])

    # ------------------------------------------------------------ chunks
    op.create_table(
        "chunks",
        _pk(),
        sa.Column("paper_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.String(512), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("bbox", postgresql.JSONB(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_type", sa.String(16), nullable=False, server_default="text"),
        _created_at(),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], name="fk_chunks_paper_id", ondelete="CASCADE"),
        sa.CheckConstraint(
            "chunk_type IN ('text', 'formula', 'table', 'figure_caption')",
            name="ck_chunks_chunk_type",
        ),
    )
    op.create_index("ix_chunks_paper_id", "chunks", ["paper_id"])
    op.create_index("ix_chunks_paper_page", "chunks", ["paper_id", "page"])

    # ------------------------------------------------------------ citations
    op.create_table(
        "citations",
        _pk(),
        sa.Column("source_paper_id", sa.BigInteger(), nullable=False),
        sa.Column("target_paper_id", sa.BigInteger(), nullable=True),  # 可空 = 外部论文（尚未入库）
        sa.Column("target_title", sa.Text(), nullable=True),
        sa.Column("target_doi", sa.String(255), nullable=True),
        sa.Column("context_snippet", sa.Text(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["source_paper_id"], ["papers.id"], name="fk_citations_source", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_paper_id"], ["papers.id"], name="fk_citations_target", ondelete="SET NULL"),
    )
    op.create_index("ix_citations_source", "citations", ["source_paper_id"])
    op.create_index("ix_citations_target", "citations", ["target_paper_id"])
    op.create_index("ix_citations_target_doi", "citations", ["target_doi"])

    # ------------------------------------------------------------ qa_history
    op.create_table(
        "qa_history",
        _pk(),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        # 溯源数组：[{answer_span, chunk_id, paper_id, page, bbox, confidence, method}]
        sa.Column("sources", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("grounding_ratio", sa.Float(), nullable=True),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_qa_history_user_id", ondelete="CASCADE"),
    )
    op.create_index("ix_qa_history_user_id", "qa_history", ["user_id"])
    op.create_index("ix_qa_history_user_created", "qa_history", ["user_id", "created_at"])

    # ------------------------------------------------------------ graph_snapshots
    op.create_table(
        "graph_snapshots",
        _pk(),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("graph_data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("insights", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_graph_snapshots_user_id", ondelete="CASCADE"),
    )
    op.create_index("ix_graph_snapshots_user_id", "graph_snapshots", ["user_id"])
    op.create_index("ix_graph_snapshots_user_created", "graph_snapshots", ["user_id", "created_at"])

    # ------------------------------------------------------------ agent_runs
    op.create_table(
        "agent_runs",
        _pk(),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("intent", sa.String(32), nullable=True),
        sa.Column("mode", sa.String(32), nullable=True),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_agent_runs_user_id", ondelete="CASCADE"),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_session_created", "agent_runs", ["session_id", "created_at"])


def downgrade() -> None:
    # 逆依赖顺序删除
    for table in (
        "agent_runs",
        "graph_snapshots",
        "qa_history",
        "citations",
        "chunks",
        "paper_versions",
        "papers",
        "users",
    ):
        op.drop_table(table)
