"""论文相关请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 与 papers.parsed_status 的取值域一致（见 app.models.paper.Paper）
PaperStatus = Literal["pending", "parsing", "chunking", "embedding", "ready", "failed"]

ChunkType = Literal["text", "formula", "table", "figure_caption"]


class PaperBase(BaseModel):
    title: str = Field(default="", max_length=2000)
    authors: list[dict[str, Any]] = Field(default_factory=list, description="[{name, affiliation}]")
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    version: str | None = Field(default=None, max_length=16, description="arXiv 版本号，如 v1 / v2")
    source_url: str | None = Field(default=None, description="论文落地页或下载地址")


class PaperCreate(PaperBase):
    """手工登记一篇论文（无 PDF，仅元数据）。"""


class PaperUpdate(BaseModel):
    """部分更新，只有显式给出的字段生效。"""

    title: str | None = None
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    version: str | None = None
    source_url: str | None = None


class PaperOut(PaperBase):
    """列表项。不含正文，避免列表接口返回过大。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    file_path: str | None = None
    file_hash: str | None = None
    semantic_hash: str | None = None
    parsed_status: PaperStatus
    # 实际生效的解析器（mineru / pymupdf / pdfplumber / pypdf）。
    # 放在列表项里而不是只放详情：MinerU 不可用时会静默降级，
    # 用户在文献库里就该一眼看出这篇到底是谁解析的。
    parser: str | None = None
    page_count: int | None = None
    error: str | None = None
    created_at: datetime


class PaperDetail(PaperOut):
    """详情：补上分块数这类派生信息。"""

    chunk_count: int = 0


class PaperUploadResult(BaseModel):
    paper: PaperOut
    index_started: bool = Field(
        default=False,
        description="是否已排入后台解析。进度改看 papers.parsed_status，轮询 GET /papers/{id}",
    )


class ChunkOut(BaseModel):
    """一个检索单元。`bbox` 是归一化坐标，前端据此在 PDF 上画高亮框。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    section: str | None = None
    page: int | None = None
    bbox: Any | None = None
    content: str
    token_count: int = 0
    chunk_type: ChunkType = "text"
    created_at: datetime


__all__ = [
    "ChunkOut",
    "ChunkType",
    "PaperBase",
    "PaperCreate",
    "PaperDetail",
    "PaperOut",
    "PaperStatus",
    "PaperUpdate",
    "PaperUploadResult",
]
