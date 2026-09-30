"""数据层初始化 CLI。

    python -m app.db.init_db              # 建表（create_all）+ 集合 + 默认用户
    python -m app.db.init_db --migrate    # 改用 alembic upgrade head 建表（推荐）
    python -m app.db.init_db --recreate-milvus   # 删掉重建两个集合（会丢向量）
    python -m app.db.init_db --check      # 只自检：8 张表 + 2 个集合 + 索引参数

`--check` 是给验收和排障用的：它把 Milvus 的索引参数**回读**出来，而不是
只报告"创建接口调用成功" —— 后者在建表失败时同样会打印成功。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Any

from loguru import logger
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import dispose_engine, engine
from app.models import SPEC_TABLES


async def _existing_tables() -> set[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))).all()
    return {str(r[0]) for r in rows}


async def create_tables_orm() -> list[str]:
    """用 SQLAlchemy 元数据建表（不走迁移，适合快速起环境）。"""
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return sorted(await _existing_tables())


def migrate() -> None:
    """跑 alembic upgrade head。复用 alembic 的 CLI，避免另写一套迁移逻辑。"""
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.sync_database_url)
    command.upgrade(cfg, "head")


async def check() -> dict[str, Any]:
    """自检：PG 8 张表 / Milvus 2 个集合 / 索引参数。返回结构化结果。"""
    report: dict[str, Any] = {"postgres": {}, "milvus": {}, "ok": True}

    try:
        tables = await _existing_tables()
        missing = sorted(set(SPEC_TABLES) - tables)
        report["postgres"] = {"tables": sorted(tables), "missing": missing}
        report["ok"] = report["ok"] and not missing
    except Exception as exc:  # noqa: BLE001
        report["postgres"] = {"error": f"{type(exc).__name__}: {exc}"}
        report["ok"] = False

    try:
        from app.db.milvus import describe_collections

        info = await asyncio.to_thread(describe_collections)
        report["milvus"] = info
        for name, detail in info.items():
            if not detail.get("exists"):
                report["ok"] = False
                continue
            got = {(i.get("field"), i.get("type"), i.get("metric")) for i in detail.get("indexes", [])}
            want = {(i["field_name"], i["index_type"], i["metric_type"]) for i in detail.get("expected_indexes", [])}
            if not want <= got:
                report["ok"] = False
                report.setdefault("index_mismatch", {})[name] = {
                    "got": sorted(map(str, got)),
                    "want": sorted(map(str, want)),
                }
    except Exception as exc:  # noqa: BLE001
        report["milvus"] = {"error": f"{type(exc).__name__}: {exc}"}
        report["ok"] = False

    return report


async def run(args: argparse.Namespace) -> int:
    if args.check:
        print(json.dumps(await check(), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.recreate_milvus:
        from app.db.milvus import get_store

        created = await asyncio.to_thread(get_store().ensure_collections, recreate=True)
        logger.info("已重建 Milvus 集合：{}", created or "（无）")

    if args.migrate:
        migrate()
        logger.info("alembic upgrade head 完成")
    else:
        tables = await create_tables_orm()
        logger.info("create_all 完成，当前表：{}", ", ".join(tables))

    from app.db.bootstrap import ensure_default_user, init_milvus

    uid = await ensure_default_user()
    logger.info("默认用户 id={}", uid)
    if not args.skip_milvus:
        try:
            logger.info("Milvus 集合：{}", await init_milvus(recreate=False) or "（已存在）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 初始化失败（不影响 PG 部分）：{}", exc)

    print(json.dumps(await check(), ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据层初始化 / 自检")
    parser.add_argument("--migrate", action="store_true", help="用 alembic upgrade head 建表（默认用 create_all）")
    parser.add_argument("--recreate-milvus", action="store_true", help="删掉并重建 Milvus 集合（会丢向量）")
    parser.add_argument("--skip-milvus", action="store_true", help="跳过 Milvus 初始化")
    parser.add_argument("--check", action="store_true", help="只自检，不做任何写入")
    args = parser.parse_args(argv)

    setup_logging()
    try:
        return asyncio.run(run(args))
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(dispose_engine())


if __name__ == "__main__":
    sys.exit(main())
