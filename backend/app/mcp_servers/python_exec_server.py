"""Python 沙箱 MCP Server —— 让 Agent 做数值计算、数据处理、简单统计。

独立运行：`python -m app.mcp_servers.python_exec_server`

⚠️ 安全边界（务必读）
本实现在**本机子进程**里跑代码，做了四件事：临时工作目录、超时、剥离环境变量
（不带任何 API key）、危险调用黑名单。这**不是**安全沙箱 —— 黑名单挡不住有心人，
Python 的 `os` / `ctypes` 总能绕。生产部署必须把它放进独立容器
（无网络、只读根文件系统、内存与 CPU 限额）再暴露给 Agent。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("python-exec")

# 只挡最直白的几条；真正的隔离靠容器，见模块 docstring
FORBIDDEN = re.compile(
    r"\b("
    r"subprocess|os\.system|os\.popen|os\.exec|pty\.spawn|socket|ctypes|"
    r"shutil\.rmtree|requests\.|urllib\.request|httpx\.|open\(['\"]?/(etc|proc|sys)"
    r")\b",
    re.I,
)

_ENV_ALLOW = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME", "USERPROFILE")


@mcp.tool()
async def python_exec(code: str, timeout: int = 10) -> dict[str, Any]:
    """在受限子进程中执行 Python 代码并返回 stdout。

    何时使用：数值计算、统计、单位换算、对已检索到的数据做整理排序、生成图表所需的
    数据结构。**有确定答案的计算才用它**，不要用它试探环境。
    何时不要用：发起网络请求（用 arxiv_search / web_search）、读写本机文件、装包。

    约定：把要输出的结果 `print()` 出来 —— 只有 stdout 会返回给模型。
    可用标准库（math/statistics/json/re/datetime/itertools 等），**无第三方包**。

    Args:
        code: 要执行的 Python 源码。
        timeout: 超时秒数，1-60，默认 10。

    Returns:
        {"ok": bool, "stdout": str, "stderr": str, "returncode": int, "timeout": bool}
    """
    timeout = max(1, min(int(timeout), 60))

    if not code.strip():
        return {"ok": False, "stdout": "", "stderr": "代码为空", "returncode": -1, "timeout": False}

    hit = FORBIDDEN.search(code)
    if hit:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"拒绝执行：包含被禁止的调用 `{hit.group(1)}`。本沙箱只做纯计算，不支持进程/网络/文件操作。",
            "returncode": -1,
            "timeout": False,
        }

    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    with tempfile.TemporaryDirectory(prefix="pyexec_") as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(code, encoding="utf-8")

        # 用同步 subprocess + to_thread，而不是 asyncio.create_subprocess_exec：
        # 后者在 Windows 上要求 ProactorEventLoop，而 psycopg 异步驱动要求 Selector 循环，
        # 两者不可兼得（见 app/core/compat.py）。放线程里跑两边都不冲突。
        def _run_sync() -> dict[str, Any]:
            try:
                proc = subprocess.run(  # noqa: S603 - 隔离模式 + 代码黑名单已过
                    [sys.executable, "-I", str(script)],
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": f"执行超时（>{timeout}s），可能死循环或计算量过大。",
                    "returncode": -1,
                    "timeout": True,
                }
            return {
                "ok": proc.returncode == 0,
                "stdout": proc.stdout.decode("utf-8", "replace")[:8000],
                "stderr": proc.stderr.decode("utf-8", "replace")[:4000],
                "returncode": proc.returncode or 0,
                "timeout": False,
            }

        return await asyncio.to_thread(_run_sync)


if __name__ == "__main__":  # pragma: no cover
    mcp.run()
