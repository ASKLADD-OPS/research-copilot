"""pytest 公共配置。

两个作用：
1. 把 `backend/` 放进 `sys.path` —— 直接跑 `pytest` 时 pytest 只把 `tests/`
   加进去，`import app` 会失败（`python -m pytest` 才顺带加了 cwd）。
2. 在任何 `app.*` 被导入**之前**钉住环境变量。`app.core.config.settings` 是
   模块级单例，导入后改 env 已经来不及了，所以这一步必须放最前面。

这里的默认值全部指向"不存在的服务"，用来保证单测**绝不**意外连上真库：
需要外部依赖的用例请显式标 `@pytest.mark.integration`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# ---- 测试环境（必须在 import app.* 之前）----
os.environ.setdefault("ENV", "development")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # 别让 loguru 淹没测试输出
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "59999")  # 没人监听的端口
os.environ.setdefault("MILVUS_HOST", "127.0.0.1")
os.environ.setdefault("MILVUS_PORT", "59998")
os.environ.setdefault("MINERU_ENABLED", "false")  # 单测不 shell out 到外部二进制
os.environ.setdefault("LLM_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("AUTO_CREATE_TABLES", "false")
# 关掉全部 MCP Server：单测不拉子进程、不发外网请求
for flag in ("ARXIV", "PUBMED", "SEMANTIC_SCHOLAR", "PYTHON_EXEC", "WEB_SEARCH"):
    os.environ.setdefault(f"MCP_{flag}_ENABLED", "false")
os.environ.setdefault("MCP_TRANSPORT", "inproc")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    from app.core.config import settings as s

    return s


@pytest.fixture
def tracer():
    """干净实例，避免 lru_cache 的全局单例被其他用例改过阈值。"""
    from app.rag.source_tracing import SourceTracer

    return SourceTracer()


@pytest.fixture
def make_chunk():
    """造一个"长得像 RetrievedChunk"的对象。

    用 duck typing 而不是真的构造 RetrievedChunk：溯源引擎只读
    id/content/paper_id/section/page_start/page_end 六个属性，用真类反而
    要把 embedding、score 这些无关字段一起编出来。
    """

    class FakeChunk:
        def __init__(
            self,
            id: str,
            content: str,
            paper_id: str = "p1",
            section: str | None = "Method",
            page_start: int | None = 3,
            page_end: int | None = 3,
        ) -> None:
            self.id = id
            self.content = content
            self.paper_id = paper_id
            self.section = section
            self.page_start = page_start
            self.page_end = page_end

    return FakeChunk
