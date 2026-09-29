"""平台兼容。目前只有一件事：Windows 上的事件循环选型。

**为什么需要它（本机实测的坑）**

Windows 默认事件循环是 `ProactorEventLoop`，而 psycopg 的异步驱动明确拒绝它：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

于是只要在 Windows 上裸跑（`uvicorn app.main:app` 或 `pytest`），
所有走 PostgreSQL 的接口都会 500 —— 不是配置错，是循环类型不对。

同时 `asyncio.create_subprocess_exec` 只在 Proactor 上可用，而我们的
Python 沙箱工具恰好需要子进程。两边冲突，解决办法不是二选一，而是：

1. 全进程切到 `SelectorEventLoop`（PostgreSQL 可用）；
2. 沙箱改用「线程 + 同步 `subprocess.run`」（不再依赖 Proactor，见
   `app/mcp_servers/python_exec_server.py`）。

Linux 容器里没有这个问题（默认就是 Selector/Epoll），所以这个函数只在 Windows 生效。
"""

from __future__ import annotations

import asyncio
import sys

_applied = False


def use_selector_event_loop_on_windows() -> bool:
    """切到 SelectorEventLoop。返回是否真的做了切换。幂等。"""
    global _applied
    if _applied or sys.platform != "win32":
        return False
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - 理论不可达，win32 一定有这个类
        return False
    _applied = True
    return True


__all__ = ["use_selector_event_loop_on_windows"]
