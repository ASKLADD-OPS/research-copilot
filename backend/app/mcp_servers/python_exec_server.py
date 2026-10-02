"""Python 沙箱 MCP Server —— 让 Agent 做数值计算、数据处理、简单统计。

独立运行：`python -m app.mcp_servers.python_exec_server`

⚠️ 安全边界（务必读）
本实现在**本机子进程**里跑代码，做了五件事：临时工作目录、墙钟超时、剥离环境变量
（不带任何 API key）、AST 白名单守卫、POSIX 下卡地址空间上限。这**不是**安全沙箱 ——
AST 守卫挡的是"这段代码根本没机会执行"的显式逃逸，挡不住解释器/内核层面的漏洞
（真正的隔离靠 seccomp/命名空间）。生产部署必须把它放进独立容器
（无网络、只读根文件系统、内存与 CPU 限额）再暴露给 Agent。

守卫为什么用 AST 而不是正则：正则匹配的是源码文本，`__import__("o" + "s").system(...)`
之类的拼接一转就绕过去了；AST 看的是**结构**（Import / Name / Attribute 节点），
拼接字符串也照样落进 Name 节点被拦。
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import settings

mcp = FastMCP("python-exec")

# 纯计算用得到的标准库。白名单漏了某个模块只会让工具报错，不会放行逃逸 ——
# 这个方向的失败是安全的。
_ALLOWED_IMPORTS = frozenset(
    {
        "array",
        "base64",
        "bisect",
        "collections",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "pprint",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "typing",
        "uuid",
        "zlib",
    }
)

# 逃逸原语：动态导入 / 动态求值 / 文件与反射。纯计算一个都不需要。
_BANNED_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "open",
        "quit",
        "setattr",
        "vars",
    }
)

_DUNDER = re.compile(r"^__\w+__$")
_DUNDER_IN_STR = re.compile(r"__\w+__")
# `__name__` 只是 "snippet"/"__main__" 这样的字符串，放行以免误杀常见片段。
_DUNDER_OK = frozenset({"__name__"})

_ENV_ALLOW = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME", "USERPROFILE")


def guard(code: str) -> str | None:
    """静态守卫：返回拒绝原因；放行返回 None。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"语法错误：{exc.msg}（第 {exc.lineno} 行）"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_IMPORTS:
                    return f"禁止导入 `{alias.name}`：本沙箱只开放纯计算标准库。"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_IMPORTS:
                return f"禁止从 `{node.module}` 导入：本沙箱只开放纯计算标准库。"
        elif isinstance(node, ast.Name):
            if node.id in _BANNED_NAMES:
                return f"禁止使用 `{node.id}`。"
            if node.id not in _DUNDER_OK and _DUNDER.match(node.id):
                return f"禁止使用 `{node.id}`。"
        elif isinstance(node, ast.Attribute):
            if _DUNDER.match(node.attr):
                return f"禁止访问 `.{node.attr}` 属性。"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DUNDER_IN_STR.search(node.value):
            return f"禁止出现魔术属性名字符串 `{node.value[:40]}`。"
    return None


def _reject(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "stdout": "",
        "stderr": f"拒绝执行：{reason}本沙箱只做纯计算，不支持进程 / 网络 / 文件操作。",
        "returncode": -1,
        "timeout": False,
    }


def child_env() -> dict[str, str]:
    """子进程环境：只从白名单重建，把宿主机的一切凭据挡在外面。"""
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _limit_memory() -> None:  # pragma: no cover - 只在 POSIX 子进程里跑
    """在 fork 之后、exec 之前把地址空间卡住（见 settings.PYTHON_EXEC_MAX_MEM_MB）。"""
    import resource

    cap = settings.PYTHON_EXEC_MAX_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


@mcp.tool()
async def python_exec(code: str, timeout: int = settings.PYTHON_EXEC_TIMEOUT) -> dict[str, Any]:
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
    timeout = max(1, min(int(timeout or settings.PYTHON_EXEC_TIMEOUT), 60))

    if not code.strip():
        return {"ok": False, "stdout": "", "stderr": "代码为空", "returncode": -1, "timeout": False}

    reason = guard(code)
    if reason:
        return _reject(reason)

    env = child_env()

    with tempfile.TemporaryDirectory(prefix="pyexec_") as tmp:
        script = Path(tmp) / "snippet.py"
        script.write_text(code, encoding="utf-8")

        # 用同步 subprocess + to_thread，而不是 asyncio.create_subprocess_exec：
        # 后者在 Windows 上要求 ProactorEventLoop，而 psycopg 异步驱动要求 Selector 循环，
        # 两者不可兼得（见 app/core/compat.py）。放线程里跑两边都不冲突。
        def _run_sync() -> dict[str, Any]:
            try:
                proc = subprocess.run(  # noqa: S603 - `-I` 隔离模式 + AST 守卫已过
                    [sys.executable, "-I", str(script)],
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    # Windows 无 rlimit，内存上限只能靠容器（见模块 docstring）
                    preexec_fn=_limit_memory if os.name == "posix" else None,
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
