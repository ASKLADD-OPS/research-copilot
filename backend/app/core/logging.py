"""loguru 日志配置。导入 app.core.logging 即自动接管 stdlib logging / uvicorn。"""

from __future__ import annotations

import logging
import sys

from loguru import logger

from app.core.config import settings

_configured = False


class _InterceptHandler(logging.Handler):
    """把 stdlib logging 的记录转给 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        # 结构化：时间 / 级别 / 模块:行 / 消息；JSON 模式给生产日志采集用
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
            "{exception}"
        ),
        backtrace=not settings.is_production,
        diagnose=not settings.is_production,
        enqueue=True,  # 多线程/多进程安全
    )
    if settings.is_production:
        logger.add(
            "logs/app.json",
            level=settings.LOG_LEVEL,
            serialize=True,
            rotation="00:00",
            retention="14 days",
            compression="gz",
            enqueue=True,
        )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine", "httpx"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False

    _configured = True


__all__ = ["logger", "setup_logging"]
