"""MCP 工具：列表与直调（调试/前端工具面板用）。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter

from app.core.errors import ToolError
from app.schemas import ApiResponse, ToolCallRequest, ToolCallResult, ToolInfo, ToolListOut

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
