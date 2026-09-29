"""本机开发启动器（**Windows 上请用它，不要直接 `uvicorn app.main:app`**）。

原因：Windows 默认的 ProactorEventLoop 会让 psycopg 异步驱动直接报
`Psycopg cannot use the 'ProactorEventLoop'`。而 uvicorn 是"先建事件循环、后导入 app"，
所以 `app/main.py` 里那次 policy 切换来得太晚 —— 必须在这个**入口脚本**里，
于 uvicorn 之前把 policy 换掉。

**为什么还要显式传 `loop="none"`（uvicorn ≥ 0.36 的行为）**

`asyncio.set_event_loop_policy()` 只能影响 `asyncio.new_event_loop()`。而 uvicorn 0.36
起不再用 policy，改用 `loop_factory`，其中：

    uvicorn.loops.asyncio.asyncio_loop_factory(use_subprocess=False)
        -> win32 上直接返回 asyncio.ProactorEventLoop

也就是说，`--no-reload`（即 `use_subprocess=False`）时 uvicorn 会**覆盖**掉本文件刚设好的
policy，psycopg 照样报 Proactor。`loop="none"` 让 uvicorn 的 loop_factory 为 `None`，
退回 `asyncio.new_event_loop()` —— 那条路才看 policy。

（`--reload` 模式恰好没事：reload 走 `use_subprocess=True`，factory 此时返回
`SelectorEventLoop`。所以这个坑只在 `--no-reload` 下暴露。）

Linux / 容器内不受影响，直接 `uvicorn app.main:app` 即可（Dockerfile 就是这么做的）。

    python run.py            # 默认 0.0.0.0:8000，热重载
    python run.py --port 9000
"""

from __future__ import annotations

import argparse
import sys

from app.core.compat import use_selector_event_loop_on_windows

# ↓↓ 必须在 import uvicorn 之前完成
SWITCHED = use_selector_event_loop_on_windows()


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Copilot 后端开发服务器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true", help="关闭热重载")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    import uvicorn

    if SWITCHED:
        print("[compat] Windows: 已切换到 SelectorEventLoop（psycopg 需要）", flush=True)

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        # 见文件头：loop="none" 才会走 asyncio.new_event_loop()，从而尊重上面设的 policy。
        # 用默认的 "auto" 会让 uvicorn 在 --no-reload 下把循环换回 ProactorEventLoop。
        loop="none",
        log_level=args.log_level,
    )


if __name__ == "__main__":
    sys.exit(main())
