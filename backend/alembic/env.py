"""Alembic 运行环境。

连接串从 app.core.config.settings 取（不写进 alembic.ini）。
模型必须在 app.models 里被 import 过，autogenerate 才看得到表。
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import settings

# 关键：导入 Base 与全部模型，否则 --autogenerate 生成空迁移
from app.models import (  # noqa: F401
    Base,  # noqa: F401
    Conversation,
    Message,
    Paper,
    PaperChunk,
    Task,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 用同步驱动跑迁移（psycopg3 的 async 驱动不适合 alembic 默认流程）
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连库。"""
    context.configure(
        url=str(settings.sync_database_url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
