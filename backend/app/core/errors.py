"""统一错误类型与异常处理。

所有 API 响应体统一为 {code, data, message}（见 app.schemas.common.ApiResponse）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import logger

# ---------------------------------------------------------------- 业务错误码
# 1xxx 通用 / 2xxx 论文与解析 / 3xxx 检索与 RAG / 4xxx Agent / 5xxx 工具与 MCP / 9xxx 系统


class AppError(Exception):
    """所有业务异常基类。子类只需给出 code / message / http_status。"""

    code: int = 1000
    message: str = "服务异常"
    http_status: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str | None = None, *, data: Any = None, code: int | None = None) -> None:
        self.message = message or self.message
        self.data = data
        if code is not None:
            self.code = code
        super().__init__(self.message)


class NotFoundError(AppError):
    code, message = 1001, "资源不存在"
    http_status = status.HTTP_404_NOT_FOUND


class BadRequestError(AppError):
    code, message = 1002, "请求参数不合法"


class UnauthorizedError(AppError):
    code, message = 1003, "未认证或凭据已失效"
    http_status = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(AppError):
    code, message = 1004, "无权访问"
    http_status = status.HTTP_403_FORBIDDEN


class ConflictError(AppError):
    code, message = 1005, "资源冲突"
    http_status = status.HTTP_409_CONFLICT


class ParseError(AppError):
    code, message = 2001, "文档解析失败"


class UnsupportedFileTypeError(AppError):
    code, message = 2002, "不支持的文件类型"


class RetrievalError(AppError):
    code, message = 3001, "检索失败"


class EmptyRetrievalError(AppError):
    """CRAG 判定 irrelevant 且三级降级仍无结果。"""

    code, message = 3002, "检索不到相关资料"


class GroundingError(AppError):
    """溯源闸门未通过：引用无法落到检索上下文，或 Grounding Ratio 低于阈值。"""

    code, message = 3003, "生成内容未能溯源到引用上下文"


class AgentError(AppError):
    code, message = 4001, "Agent 执行失败"


class IntentUnclearError(AppError):
    """意图置信度低于阈值，需要澄清追问。"""

    code, message = 4002, "意图不明确，需要澄清"


class ToolError(AppError):
    code, message = 5001, "外部工具调用失败"


class MCPConnectionError(ToolError):
    code, message = 5002, "MCP Server 连接失败"


class LLMError(AppError):
    code, message = 9001, "模型调用失败"
    http_status = status.HTTP_502_BAD_GATEWAY


class LLMRateLimitError(LLMError):
    code, message = 9002, "模型限流"


class DatabaseUnavailableError(AppError):
    """数据库不可达 / 查询失败。

    统一在这里兜住，否则 SQLAlchemy 的异常会冒到 ServerErrorMiddleware，
    前端拿到的是裸 500 堆栈而不是 {code, data, message} 信封。
    """

    code, message = 9003, "数据库不可用"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


# ---------------------------------------------------------------- 注册处理器


def _payload(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "data": data, "message": message}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError code={} msg={}", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=_payload(exc.code, exc.message, exc.data))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,  # 用字面量：HTTP_422_UNPROCESSABLE_ENTITY 已弃用，CONTENT 常量旧版 starlette 没有
            content=_payload(1002, "请求参数不合法", {"errors": exc.errors()}),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        """DB 掉线 → 503 信封。注意：若交给通用的 Exception handler，
        Starlette 会把它路由到 ServerErrorMiddleware，那边发完响应仍会 re-raise。"""
        logger.error("数据库异常: {}: {}", type(exc).__name__, exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_payload(9003, "数据库不可用", {"reason": type(exc).__name__}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.status_code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: {}", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(9999, "服务器内部错误"),
        )


__all__ = [
    "AppError",
    "NotFoundError",
    "BadRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "ConflictError",
    "ParseError",
    "UnsupportedFileTypeError",
    "RetrievalError",
    "EmptyRetrievalError",
    "GroundingError",
    "AgentError",
    "IntentUnclearError",
    "ToolError",
    "MCPConnectionError",
    "LLMError",
    "LLMRateLimitError",
    "register_exception_handlers",
]
