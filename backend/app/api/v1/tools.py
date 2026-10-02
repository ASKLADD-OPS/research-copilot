"""工具（MCP）直调 + **主题探索闭环** + 订阅抓取。

三类东西放在同一个路由下，因为它们共用同一个"工具"心智模型：

| 端点 | 干什么 | 谁驱动 |
|---|---|---|
| `GET /tools`、`POST /tools/call` | 列出 / 直调单个工具（调试用） | 人 |
| `POST /tools/explore` | 给一个主题，Agent 自己检索 → 下载 → 打分 → 推荐 | Agent（SSE 带轨迹） |
| `POST /tools/subscribe` 等 | 把主题存成订阅，每天 8:00 自动抓 | 定时调度器 |

`/tools/explore` 与 `/tools/subscriptions/{id}/run` **返回 SSE 而不是 JSON**：
需求要的"实时进度条 + 每步 Thought/Action/Observation"只有一个持续通道能表达，
而规格里 `/chat/stream` 与 `/qa/stream` 本来就是 POST + `text/event-stream` —— 同一种做法。
结果（推荐列表）挂在收尾的 `done` 帧里，不另开一个可能被掐断的事件。

**没有 Celery**：定时抓取由 `app/workers/scheduler.py` 的进程内 asyncio 调度器承担，
理由与代价写在那个模块的头上。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import AgentError, NotFoundError, ToolError
from app.db.bootstrap import ensure_default_user
from app.llm.streaming import SSE_HEADERS, error_event, sse, sse_comment
from app.schemas import (
    ApiResponse,
    ExploreRequest,
    SchedulerOut,
    SubscribeRequest,
    SubscriptionOut,
    ToolCallRequest,
    ToolCallResult,
    ToolInfo,
    ToolListOut,
)

router = APIRouter(prefix="/tools", tags=["工具"])


@router.get("", response_model=ApiResponse[ToolListOut], summary="可用工具清单", operation_id="list_available_tools")
async def list_tools() -> ApiResponse[ToolListOut]:
    from app.agents.mcp.client import enabled_servers, get_toolbox
    from app.agents.mcp.registry import _LOCAL  # noqa: PLC2701 - 只读列举本地工具名

    servers = enabled_servers()
    tools: list[ToolInfo] = []
    note = ""

    toolbox = get_toolbox()
    try:
        await toolbox.load()
        for tool in toolbox.langchain_tools():
            tools.append(
                ToolInfo(
                    name=str(getattr(tool, "name", "")),
                    description=(getattr(tool, "description", "") or "")[:500],
                    kind="remote",
                    args_schema=_schema_of(tool),
                )
            )
    except Exception as exc:  # noqa: BLE001 - 外部 Server 起不来不该让接口挂掉
        note = f"部分 MCP Server 装载失败：{type(exc).__name__}: {exc}"

    tools.extend(ToolInfo(name=n, kind="local", server="in-process") for n in sorted(_LOCAL))
    return ApiResponse.ok(ToolListOut(tools=tools, enabled_servers=servers, note=note))


def _schema_of(tool: Any) -> dict[str, Any]:
    try:
        return tool.args_schema.model_json_schema()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return {}


@router.post("/call", response_model=ApiResponse[ToolCallResult], summary="直接调用工具", operation_id="invoke_tool")
async def call(payload: ToolCallRequest) -> ApiResponse[ToolCallResult]:
    from app.agents.mcp.registry import call_tool

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(call_tool(payload.name, payload.args), timeout=payload.timeout or 120)
    except TimeoutError as exc:
        raise ToolError(f"工具 {payload.name} 超时") from exc

    latency = int((time.perf_counter() - started) * 1000)
    ok = not (isinstance(result, str) and result.startswith("工具 "))
    return ApiResponse.ok(
        ToolCallResult(name=payload.name, ok=ok, result=result, latency_ms=latency),
        message="ok" if ok else "工具调用失败（已降级为文本返回）",
    )


# ==================================================================== 探索闭环
async def _explore_frames(payload: ExploreRequest, *, subscription_id: int | None = None) -> AsyncIterator[str]:
    """把探索闭环的事件流原样转成 SSE 帧。

    `subscription_id` 不为空时，收尾把结果写回那条订阅 —— 手动"立即抓取"与
    定时抓取共用 `record_run`，两边的 `last_status` 才会是同一套语义。
    """
    from app.agents.explore import explore

    finished: dict[str, Any] = {}
    error = ""
    yield sse_comment("open")
    try:
        async for event, data in explore(
            payload.topic,
            max_papers=payload.max_papers,
            min_score=payload.min_score,
            max_rounds=payload.max_rounds,
        ):
            if event == "done":
                finished = data
            yield sse(event, data)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要以 error 帧收尾，前端才不会卡在转圈
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("主题探索失败 topic={!r}", payload.topic)
        yield error_event(AgentError.code, error)

    if subscription_id is not None:
        from app.workers.scheduler import record_run

        try:
            await record_run(subscription_id, finished, error=error)
        except Exception:  # noqa: BLE001 - 结果帧已经发出去了，这里再抛只会污染流尾
            logger.exception("订阅 {} 结果回写失败", subscription_id)


@router.post("/explore", summary="主题探索闭环（SSE：Agent 自动检索 + 下载 + 推荐）", operation_id="explore_topic")
async def explore_topic(payload: ExploreRequest) -> StreamingResponse:
    """给一个主题，Agent 走完「规划检索词 → 两路检索 → 相关性打分 → 下载入库 → 评审 → 换词重来 → 推荐」。

    用 SSE 而不是等一个 JSON：需求要的"实时进度条 + 每步 Thought/Action/Observation"
    只有一个持续通道能表达。事件协议见 `app/agents/explore.py` 的模块头。

    **一次可能跑几分钟**（要下 PDF、解析、算向量），期间会持续发帧，不会看起来像断线。
    """
    return StreamingResponse(_explore_frames(payload), media_type="text/event-stream", headers=SSE_HEADERS)


# ==================================================================== 订阅
async def _get_subscription(session: AsyncSession, sub_id: int) -> Any:
    from app.models import Subscription

    sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(f"订阅不存在: {sub_id}")
    return sub


@router.post(
    "/subscribe",
    response_model=ApiResponse[SubscriptionOut],
    summary="订阅定期抓取",
    operation_id="subscribe_topic",
)
async def subscribe(payload: SubscribeRequest, session: SessionDep) -> ApiResponse[SubscriptionOut]:
    """把主题存成订阅。每天 `SUBSCRIBE_HOUR`:`SUBSCRIBE_MINUTE`（默认 08:00）自动抓一遍，
    新论文直接走既有的解析入库链路；结果写回订阅行的 `last_*` 字段，前端按它显示角标。

    同一个主题重复订阅不新增行，而是更新它的阈值 —— 用户点两次"订阅"不该得到两条一样的订阅。
    """
    from app.models import Subscription

    user_id = await ensure_default_user(session)
    topic = payload.topic.strip()
    existing = (
        await session.execute(select(Subscription).where(Subscription.user_id == user_id, Subscription.topic == topic))
    ).scalar_one_or_none()
    if existing is not None:
        existing.max_papers = payload.max_papers
        existing.min_score = payload.min_score
        existing.enabled = payload.enabled
        await session.commit()
        await session.refresh(existing)
        return ApiResponse.ok(SubscriptionOut.model_validate(existing), message="该主题已订阅，已更新其参数")

    sub = Subscription(
        user_id=user_id,
        topic=topic,
        max_papers=payload.max_papers,
        min_score=payload.min_score,
        enabled=payload.enabled,
        last_status="pending",
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    logger.info("新增订阅 id={} topic={!r}", sub.id, sub.topic)
    return ApiResponse.ok(
        SubscriptionOut.model_validate(sub),
        message=f"已订阅，每天 {settings.SUBSCRIBE_HOUR:02d}:{settings.SUBSCRIBE_MINUTE:02d} 自动抓取",
    )


@router.get(
    "/subscriptions",
    response_model=ApiResponse[list[SubscriptionOut]],
    summary="订阅列表",
    operation_id="list_subscriptions",
)
async def list_subscriptions(session: SessionDep) -> ApiResponse[list[SubscriptionOut]]:
    from app.models import Subscription

    rows = (await session.execute(select(Subscription).order_by(Subscription.created_at.desc()))).scalars().all()
    return ApiResponse.ok([SubscriptionOut.model_validate(r) for r in rows])


@router.delete(
    "/subscriptions/{sub_id}",
    response_model=ApiResponse[dict[str, Any]],
    summary="取消订阅",
    operation_id="unsubscribe_topic",
)
async def unsubscribe(sub_id: int, session: SessionDep) -> ApiResponse[dict[str, Any]]:
    from app.models import Subscription

    await _get_subscription(session, sub_id)
    await session.execute(delete(Subscription).where(Subscription.id == sub_id))
    await session.commit()
    return ApiResponse.ok({"deleted": sub_id})


@router.post(
    "/subscriptions/{sub_id}/run",
    summary="立即抓取一次（不等定时）",
    operation_id="run_subscription_now",
)
async def run_subscription_now(
    sub_id: int,
    session: SessionDep,
    max_rounds: Annotated[int | None, Query(ge=1, le=5, description="检索轮次上限；缺省取配置")] = None,
) -> StreamingResponse:
    """手动触发一次。**这是"定时任务会不会正常跑"唯一能当场验证的入口** ——
    否则只能等到明天 8:00 才知道调度器是活的。

    走的是与定时抓取完全相同的 `explore()`，区别只在于轨迹会一路 SSE 发出来。
    """
    from app.workers.scheduler import mark_running

    sub = await _get_subscription(session, sub_id)
    # 先落 running 再开流：前端刚点下去就该看到状态变了
    await mark_running(sub_id)
    return StreamingResponse(
        _explore_frames(
            ExploreRequest(topic=sub.topic, max_papers=sub.max_papers, min_score=sub.min_score, max_rounds=max_rounds),
            subscription_id=sub_id,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/scheduler",
    response_model=ApiResponse[SchedulerOut],
    summary="定时抓取调度状态",
    operation_id="get_scheduler_status",
)
async def scheduler_status() -> ApiResponse[SchedulerOut]:
    """`next_run_at` 让"定时任务是否就绪"可当场核对，不必等到 8:00。"""
    from app.workers.scheduler import next_run_at

    enabled = settings.SUBSCRIBE_ENABLED
    return ApiResponse.ok(
        SchedulerOut(
            enabled=enabled,
            hour=settings.SUBSCRIBE_HOUR,
            minute=settings.SUBSCRIBE_MINUTE,
            next_run_at=next_run_at() if enabled else None,
        ),
        message="ok" if enabled else "定时抓取已关闭（SUBSCRIBE_ENABLED=false）",
    )


__all__ = ["router"]
