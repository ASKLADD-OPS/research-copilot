"""问答（RAG）相关请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    paper_ids: list[str] = Field(default_factory=list, description="限定检索范围；空=全库")
    top_k: int | None = Field(default=None, ge=1, le=50)
    intent: str | None = Field(default=None, description="跳过意图识别，直接指定（调试用）")
    debug: bool = False


class RetrievedChunkOut(BaseModel):
    chunk_id: str
    paper_id: str
    title: str = ""
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    score: float = 0.0
    rerank_score: float | None = None
    sources: list[str] = Field(default_factory=list)
    preview: str = Field(default="", description="正文前 300 字，避免整块回传")


class CitationOut(BaseModel):
    """一条可追溯的引用。`verified` 来自 NLI/词法蕴含校验。"""

    marker: int | None = None
    chunk_id: str = ""
    paper_id: str = ""
    title: str = ""
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    quote: str = ""
    nli_score: float = 0.0
    verified: bool = False


class RetrievalDebug(BaseModel):
    """检索链路的中间量，只在 debug=true 时返回。"""

    crag_level: Literal["relevant", "ambiguous", "irrelevant"] | None = None
    rewrite_round: int = 0
    rewritten_query: str = ""
    rerank_applied: bool = False
    top_k: int = 0
    trace: list[dict[str, Any]] = Field(default_factory=list)


class AskResult(BaseModel):
    answer: str
    intent: str = ""
    intent_confidence: float = 0.0
    citations: list[CitationOut] = Field(default_factory=list)
    grounding_ratio: float = 0.0
    passed_grounding: bool = Field(default=False, description="grounding_ratio ≥ 阈值且无幻觉引用编号")
    unsupported_claims: list[str] = Field(default_factory=list)
    guardrail_flags: list[str] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    reflections: list[dict[str, Any]] = Field(default_factory=list)
    retrieved: list[RetrievedChunkOut] = Field(default_factory=list)
    debug: RetrievalDebug | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class TraceRequest(BaseModel):
    """对一段既有文本做溯源校验（不生成，只校验）。"""

    answer: str = Field(min_length=1)
    chunk_ids: list[str] = Field(default_factory=list, description="允许被引用的 chunk 白名单")
    query: str = ""


class TraceResult(BaseModel):
    grounding_ratio: float = 0.0
    sentences_total: int = 0
    sentences_supported: int = 0
    passed: bool = False
    phantom_markers: list[int] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    uncited_claims: list[str] = Field(default_factory=list)
    number_mismatches: list[str] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
