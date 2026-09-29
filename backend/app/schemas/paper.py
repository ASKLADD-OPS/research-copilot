"""论文相关请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PaperStatus = Literal["pending", "parsing", "indexing", "ready", "failed"]


class PaperBase(BaseModel):
    title: str = Field(default="", max_length=2000)
    authors: list[dict[str, Any]] = Field(default_factory=list, description="[{name, affiliation}]")
    abstract: str | None = None
    year: int | None = Field(default=None, ge=1500, le=2200)
    venue: str | None = Field(default=None, max_length=512)
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    tags: list[str] = Field(default_factory=list)


class PaperCreate(PaperBase):
    """手工登记一篇论文（已有 PDF 或仅元数据）。"""

    source: Literal["upload", "arxiv", "pubmed", "s2"] = "upload"


class PaperUpdate(BaseModel):
    """部分更新，只有非 None 字段生效。"""

    title: str | None = None
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    tags: list[str] | None = None


class PaperOut(PaperBase):
    """列表项。不含正文，避免列表接口返回过大。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    status: PaperStatus
    num_chunks: int = 0
    page_count: int | None = None
    file_size: int | None = None
    # 实际生效的解析器（mineru / pymupdf / pdfplumber / pypdf）。
    # 放在列表项里而不是只放详情：MinerU 不可用时会静默降级，
    # 用户在文献库里就该一眼看出这篇到底是谁解析的。
    parser: str | None = None
    error: str | None = None
    indexed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PaperDetail(PaperOut):
    """详情：在列表字段之外补上本地文件路径与解析元数据。"""

    pdf_path: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class PaperUploadResult(BaseModel):
    paper: PaperOut
    task_id: str | None = Field(
        default=None,
        description="后台解析任务 id（进程内执行）；轮询 /tasks/{task_id} 看进度。index=false 时为 null",
    )


class PaperChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chunk_index: int
    content: str
    section_name: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0
