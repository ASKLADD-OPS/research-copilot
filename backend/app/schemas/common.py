"""统一响应体与通用模型。所有 API 一律返回 {code, data, message}。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# 业务成功码。非 0 均为错误，与 app.core.errors 中的错误码一一对应。
CODE_OK = 0


class ApiResponse(BaseModel, Generic[T]):
    """统一响应体。

    用法：
        return ApiResponse.ok(payload)
        return ApiResponse.fail(3002, "检索不到相关资料")
    """

    model_config = ConfigDict(from_attributes=True)

    code: int = CODE_OK
    data: T | None = None
    message: str = "ok"

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> ApiResponse[T]:
        return cls(code=CODE_OK, data=data, message=message)

    @classmethod
    def fail(cls, code: int, message: str, data: T | None = None) -> ApiResponse[T]:
        return cls(code=code, data=data, message=message)


class PageMeta(BaseModel):
    total: int = Field(default=0, description="满足条件的总条数")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    has_next: bool = False


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class HealthComponent(BaseModel):
    name: str
    ok: bool
    latency_ms: float | None = None
    detail: str | None = None


class HealthReport(BaseModel):
    status: str = Field(description="ok | degraded | down")
    version: str
    env: str
    components: list[HealthComponent] = Field(default_factory=list)
