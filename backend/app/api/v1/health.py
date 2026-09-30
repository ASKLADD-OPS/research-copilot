"""三库健康检查：PostgreSQL / Milvus / Redis。

为什么不并进 `/health`
----------------------
`/health` 是给容器编排与前端顶栏看的**总览**（还含 MinerU、LLM 配置），
语义是"这个进程能不能干活"。`/health/db` 是给数据层排障用的**细查**：
它要回答的是"8 张表建全了吗、两个集合的索引建上了吗、Redis 通不通"。
混在一起的话，任何一次存储抖动都会让顶栏判定整个后端不可用，
而实际上检索完全正常 —— 这两种失败必须能被分开表达。

探测一律有超时上限。本机对未监听端口是 DROP 而不是 REJECT，
没有超时的话一个打不开的 Redis 能让这个接口挂满 30 秒。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine
from app.models import SPEC_TABLES
from app.schemas import ApiResponse, DbHealthReport, StoreHealth

router = APIRouter(prefix="/health", tags=["系统"])

PROBE_TIMEOUT = 5.0  # 单个存储的探测上限（秒）


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _brief(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


# ------------------------------------------------------------------ PostgreSQL
async def _probe_postgres() -> StoreHealth:
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            found = {
                str(r[0])
                for r in (
                    await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))
                ).all()
            }
            version = str((await conn.execute(text("SHOW server_version"))).scalar_one())
    except Exception as exc:  # noqa: BLE001
        detail = _brief(exc)
        if "ProactorEventLoop" in detail:
            detail += " ← Windows 上请用 `python run.py` 启动（见 backend/README.md）"
        return StoreHealth(name="postgres", ok=False, latency_ms=_ms(started), detail=detail)

    missing = sorted(set(SPEC_TABLES) - found)
    ok = not missing
    return StoreHealth(
        name="postgres",
        ok=ok,
        latency_ms=_ms(started),
        detail=f"PostgreSQL {version}；规格 {len(SPEC_TABLES)} 张表齐全" if ok else f"缺表: {', '.join(missing)}",
        info={"tables": sorted(found), "missing_tables": missing, "server_version": version},
    )


# ------------------------------------------------------------------ Milvus
async def _probe_milvus() -> StoreHealth:
    started = time.perf_counter()
    try:
        from app.db.milvus import get_store

        store = get_store()
        ok, detail = await asyncio.to_thread(store.health)
        info: dict[str, Any] = {}
        if ok or "未创建" in detail:
            # 集合在就顺手把索引参数回读一遍 —— 「建过」和「建上了」是两回事
            info = await asyncio.to_thread(store.describe)
        return StoreHealth(name="milvus", ok=bool(ok), latency_ms=_ms(started), detail=detail, info=info)
    except Exception as exc:  # noqa: BLE001
        return StoreHealth(name="milvus", ok=False, latency_ms=_ms(started), detail=_brief(exc))


# ------------------------------------------------------------------ Redis
async def _probe_redis() -> StoreHealth:
    started = time.perf_counter()
    try:
        from app.db.redis import aping

        ok, detail = await aping()
        return StoreHealth(
            name="redis",
            ok=bool(ok),
            latency_ms=_ms(started),
            detail=detail,
            info={"url_host": f"{settings.REDIS_HOST}:{settings.REDIS_PORT}"},
        )
    except Exception as exc:  # noqa: BLE001
        return StoreHealth(name="redis", ok=False, latency_ms=_ms(started), detail=_brief(exc))


async def _guard(name: str, coro: Any, timeout: float = PROBE_TIMEOUT) -> StoreHealth:
    """任何存储都不许把这个接口拖死 —— 探针必须在数秒内给出结论。"""
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        return StoreHealth(name=name, ok=False, latency_ms=_ms(started), detail=f"探测超时（>{timeout:g}s）")
    except Exception as exc:  # noqa: BLE001
        return StoreHealth(name=name, ok=False, latency_ms=_ms(started), detail=_brief(exc))


# ------------------------------------------------------------------ 路由
@router.get("/db", response_model=ApiResponse[DbHealthReport], summary="三库健康检查（Postgres / Milvus / Redis）")
async def health_db() -> ApiResponse[DbHealthReport] | JSONResponse:
    components = list(
        await asyncio.gather(
            _guard("postgres", _probe_postgres()),
            _guard("milvus", _probe_milvus()),
            _guard("redis", _probe_redis()),
        )
    )
    bad = [c for c in components if not c.ok]
    status = "ok" if not bad else ("degraded" if len(bad) < len(components) else "down")

    pg = next((c for c in components if c.name == "postgres"), None)
    report = DbHealthReport(
        status=status,
        components=components,
        missing_tables=list((pg.info.get("missing_tables") if pg else None) or []),
        spec_tables=list(SPEC_TABLES),
    )

    payload = ApiResponse.ok(report)
    if status == "down":
        # 三个库全挂说明这个进程确实干不了活，让编排层能据此判失败
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return payload


__all__ = ["router"]
