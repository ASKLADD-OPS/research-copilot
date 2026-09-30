"""FastAPI 应用入口。

启动顺序：日志 → （开发态）建表 → 暴露 API 为 MCP。
刻意**不**在启动时预热 bge-m3 / CrossEncoder：模型加载要几十秒，
放在首个请求里（lazy）比让容器卡在 starting 状态更好调。

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.compat import use_selector_event_loop_on_windows
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.db.session import dispose_engine, engine
from app.schemas import ApiResponse, HealthComponent, HealthReport

# 必须在任何连接建立之前调用（Windows 上 psycopg 异步驱动不接受默认的 Proactor 循环）
use_selector_event_loop_on_windows()

VERSION = "0.1.0"
API_PREFIX = "/api/v1"


# ------------------------------------------------------------------ 健康检查
async def _check_postgres() -> HealthComponent:
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return HealthComponent(name="postgres", ok=True, latency_ms=_ms(started))
    except Exception as exc:  # noqa: BLE001
        detail = _brief(exc)
        if "ProactorEventLoop" in detail:
            # uvicorn 先建循环、后导入 app，所以 policy 切换必须在入口脚本里做
            detail += " ← Windows 上请用 `python run.py` 启动（见 backend/README.md）"
        return HealthComponent(name="postgres", ok=False, latency_ms=_ms(started), detail=detail)


async def _check_milvus() -> HealthComponent:
    started = time.perf_counter()
    try:
        from app.db.milvus import get_store

        ok, detail = await asyncio.to_thread(get_store().health)
        return HealthComponent(name="milvus", ok=bool(ok), latency_ms=_ms(started), detail=None if ok else detail)
    except Exception as exc:  # noqa: BLE001
        return HealthComponent(name="milvus", ok=False, latency_ms=_ms(started), detail=_brief(exc))


def _check_mineru() -> HealthComponent:
    """只查 CLI 在不在，不真跑解析 —— MinerU 一次解析几十秒起步，健康检查等不起。"""
    if not settings.MINERU_ENABLED:
        return HealthComponent(name="mineru", ok=True, detail="MINERU_ENABLED=false（解析降级为 PyMuPDF）")

    from app.parsers.mineru import resolve_cmd

    path = resolve_cmd()
    return HealthComponent(
        name="mineru",
        ok=bool(path),
        detail=None if path else f"找不到 mineru 可执行文件（MINERU_CMD={settings.MINERU_CMD}），将降级解析",
    )


def _check_llm() -> HealthComponent:
    """只检查配置，不发探测请求 —— 健康检查不该按 token 计费。"""
    configured = bool(settings.LLM_API_KEY)
    return HealthComponent(
        name="llm",
        ok=configured,
        detail=None if configured else "LLM_API_KEY 未配置（问答/写作会失败）",
    )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _brief(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


HEALTH_TIMEOUT = 8.0  # 单个依赖的探测上限（秒）


async def _guard(name: str, coro: Any, timeout: float = HEALTH_TIMEOUT) -> HealthComponent:
    """任何依赖都不许把 /health 拖死 —— 探针必须在数秒内给出结论。"""
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        return HealthComponent(name=name, ok=False, latency_ms=_ms(started), detail=f"探测超时（>{timeout:g}s）")
    except Exception as exc:  # noqa: BLE001
        return HealthComponent(name=name, ok=False, latency_ms=_ms(started), detail=_brief(exc))


# ------------------------------------------------------------------ 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info("启动 Research Copilot v{} env={}", VERSION, settings.ENV)

    if settings.AUTO_CREATE_TABLES:
        try:
            from app.models import Base

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("表结构就绪（AUTO_CREATE_TABLES=true；生产请用 alembic upgrade head）")
        except Exception as exc:  # noqa: BLE001 - 数据库没起也要让服务起来，好让人看到 /health
            logger.warning("建表失败，稍后请手工跑 alembic upgrade head：{}", exc)

    # 默认用户 + Milvus 两个集合。同样尽力而为：Milvus 没起时服务照常可用，
    # /api/v1/health/db 会如实报 degraded，而不是让进程起不来。
    try:
        from app.db.bootstrap import bootstrap

        result = await bootstrap()
        if result.get("errors"):
            logger.warning("启动引导有未完成项：{}", result["errors"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动引导失败：{}", exc)

    if settings.AUTO_CREATE_TABLES:
        try:
            from app.agents.checkpointer import init_checkpointer_tables

            await init_checkpointer_tables()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangGraph checkpointer 表初始化失败（会话不落库）：{}", exc)

    if settings.MCP_EXPOSE_API:
        _mount_mcp(app)

    yield

    from app.db.redis import aclose as redis_aclose

    await redis_aclose()
    await dispose_engine()
    logger.info("已关闭")


def _mount_mcp(app: FastAPI) -> None:
    """把后端 API 暴露为 MCP 工具（零配置）。

    装不上/版本不兼容就跳过 —— 这是一个**可选的对外能力**，
    不该成为服务起不来的原因。
    """
    try:
        from fastapi_mcp import FastApiMCP

        mcp = FastApiMCP(app, name="research-copilot")
        mcp.mount_http(mount_path=settings.MCP_API_MOUNT)
        logger.info("已将 API 暴露为 MCP：{}", settings.MCP_API_MOUNT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fastapi-mcp 挂载跳过：{}", _brief(exc))


# ------------------------------------------------------------------ 应用
app = FastAPI(
    title="Research Copilot API",
    description=(
        "学术研究 Multi-Agent 系统后端。\n\n"
        "- **RAG**：bge-m3 双向量 → Milvus 混合检索 → RRF(k=60) → CrossEncoder 重排 → CRAG 三级降级\n"
        "- **Agent**：LangGraph 有向循环图（意图识别 + ReAct + Plan-and-Execute + Reflection）\n"
        "- **抗幻觉**：Self-Citation + 蕴含校验 + Grounding Ratio 阈值\n"
        "- **工具**：arXiv / PubMed / Semantic Scholar / Python 沙箱 / Web Search，全部走 MCP"
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(api_router, prefix=API_PREFIX)


@app.get("/", response_model=ApiResponse[dict[str, Any]], tags=["系统"], summary="服务信息")
async def root() -> ApiResponse[dict[str, Any]]:
    return ApiResponse.ok(
        {
            "name": "research-copilot",
            "version": VERSION,
            "env": settings.ENV,
            "docs": "/docs",
            "api": API_PREFIX,
        }
    )


@app.get(
    "/health",
    response_model=ApiResponse[HealthReport],
    tags=["系统"],
    summary="健康检查（Postgres / Milvus / MinerU / LLM）",
)
async def health(request: Request) -> ApiResponse[HealthReport]:
    components = list(
        await asyncio.gather(
            _guard("postgres", _check_postgres()),
            _guard("milvus", _check_milvus()),
        )
    )
    components.append(_check_mineru())
    components.append(_check_llm())

    bad = [c for c in components if not c.ok]
    status = "ok" if not bad else ("degraded" if len(bad) < len(components) else "down")
    report = HealthReport(status=status, version=VERSION, env=settings.ENV, components=components)

    payload = ApiResponse.ok(report)
    if status == "down":
        # 让编排层（compose healthcheck / k8s）能据此判失败
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return payload
