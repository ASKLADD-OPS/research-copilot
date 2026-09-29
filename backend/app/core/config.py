"""全局配置（Pydantic Settings v2）。

唯一入口：`from app.core.config import settings`
所有可调参数都在这里，且都能被环境变量覆盖（键名大写，大小写不敏感）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),  # 本机直跑读 backend/.env，容器里由 compose 注入
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ 通用
    ENV: Literal["development", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "dev-secret-change-me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    BACKEND_CORS_ORIGINS: str = "http://localhost:3000"
    MAX_UPLOAD_MB: int = 100
    UPLOAD_DIR: str = "./data/papers"  # PDF 落盘目录（容器里挂到 /app/data/papers）
    AUTO_CREATE_TABLES: bool = True  # 启动时 create_all（仅开发态；生产用 alembic upgrade head）

    # ------------------------------------------------------------------ PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "copilot"
    POSTGRES_PASSWORD: str = "copilot_pass"
    POSTGRES_DB: str = "research_copilot"
    DATABASE_URL: str | None = None
    # 连不上时必须尽快报错：本机/防火墙有时是 DROP 而不是 REJECT，
    # 不设超时会让 /health 永久挂住（compose healthcheck 直接判死）。
    # 注意 localhost 会解析出 IPv6 + IPv4 两个地址，libpq 逐个试，最坏耗时翻倍
    # —— 所以这里给 3s，双栈最坏 6s，仍在 /health 的 8s 护栏内。
    DB_CONNECT_TIMEOUT: int = 3

    # ------------------------------------------------------------------ 文档解析（MinerU）
    # MinerU 以 CLI 形式调用（见 app/parsers/mineru.py）：它依赖 torch + 数 GB 模型权重，
    # 放在独立 venv / 独立容器里，靠 MINERU_CMD 指过去，不要塞进 API 进程。
    MINERU_ENABLED: bool = True
    MINERU_CMD: str = "mineru"  # 裸命令走 PATH，也可写绝对路径（如独立 venv 的 mineru.exe）
    MINERU_METHOD: str = "auto"  # auto | txt | ocr（对应 CLI 的 -m）
    MINERU_DEVICE: str = "cpu"  # cpu | cuda（对应 -d）
    MINERU_TIMEOUT: int = 600  # 整篇解析上限（秒）；CPU 上每页数秒起，别设太小
    MINERU_EXTRA_ARGS: str = ""  # 追加 CLI 参数（-f / -t / -l 等版本差异项从这里补）

    # ------------------------------------------------------------------ Milvus
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION: str = "papers"
    MILVUS_DENSE_DIM: int = 1024
    MILVUS_METRIC_TYPE: str = "COSINE"
    MILVUS_INDEX_TYPE: str = "HNSW"

    # ------------------------------------------------------------------ 检索
    RRF_K: int = 60
    RETRIEVAL_TOP_K: int = 20
    RETRIEVAL_RECALL_K: int = 50
    RERANKER_ENABLED: bool = True
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"

    # ------------------------------------------------------------------ CRAG / 溯源
    CRAG_ENABLED: bool = True
    CRAG_RELEVANCE_THRESHOLD: float = 0.5
    CRAG_AMBIGUOUS_LOW: float = 0.3
    CRAG_REWRITE_MAX_RETRY: int = 3
    GROUNDING_MIN_RATIO: float = 0.8

    # ------------------------------------------------------------------ LLM
    LLM_PROVIDER: str = "deepseek"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL_PLANNER: str = "deepseek-chat"
    LLM_MODEL_EXECUTOR: str = "deepseek-chat"
    LLM_MODEL_REVIEWER: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT: int = 120
    INTENT_CONFIDENCE_THRESHOLD: float = 0.6
    REFLECTION_MAX_REFINE: int = 2
    REPLAN_MAX_ROUNDS: int = 2
    AGENT_MAX_REACT_ROUNDS: int = 4  # Executor 单步内的 ReAct 循环上限
    CONTEXT_MAX_CHARS: int = 12000  # 喂给 LLM 的检索上下文上限
    RETRIEVAL_MIN_SCORE: float = 0.0  # 0=关闭低质检索检查（RRF 分值与余弦不可比，需按实测分布标定）
    GUARDRAIL_MAX_INPUT_CHARS: int = 8000
    GUARDRAIL_MAX_OUTPUT_CHARS: int = 20000

    # ------------------------------------------------------------------ Embedding
    EMBEDDING_BACKEND: Literal["modelscope", "local"] = "modelscope"
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_BATCH_SIZE: int = 16
    EMBEDDING_MAX_LENGTH: int = 8192
    MODELSCOPE_CACHE: str | None = None

    # ------------------------------------------------------------------ MCP
    MCP_TIMEOUT: int = 60
    MCP_EXPOSE_API: bool = True  # 用 fastapi-mcp 把后端 API 暴露为 MCP
    MCP_API_MOUNT: str = "/mcp"
    MCP_TRANSPORT: Literal["inproc", "stdio"] = "inproc"  # 进程内直调（快）| stdio 子进程（隔离）
    MCP_PYTHON: str = "python"  # stdio 模式拉起 Server 用的解释器
    MCP_PYTHONPATH: str = ""  # stdio 模式的 PYTHONPATH（backend 根目录）
    MCP_ARXIV_ENABLED: bool = True
    MCP_PUBMED_ENABLED: bool = True
    MCP_SEMANTIC_SCHOLAR_ENABLED: bool = True
    MCP_PYTHON_EXEC_ENABLED: bool = True
    MCP_WEB_SEARCH_ENABLED: bool = True
    PYTHON_EXEC_TIMEOUT: int = 10
    PYTHON_EXEC_MAX_MEM_MB: int = 512
    SEMANTIC_SCHOLAR_API_KEY: str = ""
    NCBI_API_KEY: str = ""
    TAVILY_API_KEY: str = ""

    # ------------------------------------------------------------------ 派生值
    @property
    def async_database_url(self) -> str:
        """异步驱动（应用运行时用）。"""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_database_url(self) -> str:
        """同步驱动（Alembic 迁移用）。psycopg3 同一个 driver 名即可。"""
        return self.async_database_url

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def upload_path(self) -> Path:
        """PDF 落盘目录（不存在则创建）。"""
        path = Path(self.UPLOAD_DIR).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @field_validator("LOG_LEVEL")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache
def get_settings() -> Settings:
    """带缓存，避免每次读 env。测试里改环境变量后需要 `get_settings.cache_clear()`。"""
    return Settings()


settings = get_settings()
