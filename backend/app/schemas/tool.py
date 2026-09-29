"""工具（MCP）相关模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    name: str
    description: str = ""
    kind: str = Field(default="remote", description="remote=MCP Server | local=进程内原生")
    server: str = Field(default="", description="所属 MCP Server")
    args_schema: dict[str, Any] = Field(default_factory=dict)


class ToolListOut(BaseModel):
    tools: list[ToolInfo] = Field(default_factory=list)
    enabled_servers: list[str] = Field(default_factory=list)
    note: str = ""


class ToolCallRequest(BaseModel):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: int | None = Field(default=None, ge=1, le=300)


class ToolCallResult(BaseModel):
    name: str
    ok: bool = True
    result: Any = None
    error: str | None = None
    latency_ms: int = 0
