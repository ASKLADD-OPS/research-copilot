"""问答（RAG）相关请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

# 一条溯源的认定方式：白名单自引 | NLI 模型蕴含 | 两者融合（词法蕴含 + 数字硬规则）
AttributionMethod = Literal["self_citation", "nli", "hybrid"]


class AskRequest(BaseModel):
    # `question` / `mode` 是规格里给的对外字段名，`query` / `intent` 是本项目内部的既有叫法。
    # 两个都收（AliasChoices 而不是改字段名）：内部状态、图、提示词、既有测试全都用
    # query/intent，重命名是零收益的大范围改动，兼容一层别名就够了。
    query: str = Field(
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("query", "question"),
        description="用户问题（也接受 question 别名）",
    )
    paper_ids: list[int] = Field(default_factory=list, description="限定检索范围；空=全库")
    top_k: int | None = Field(default=None, ge=1, le=50)
    intent: str | None = Field(
        default=None,
        validation_alias=AliasChoices("intent", "mode"),
        description="跳过意图识别，直接指定 single_paper_qa / cross_paper_reasoning（也接受 mode 别名）",
    )
    debug: bool = False


class TimelineItem(BaseModel):
    """跨篇对比的时间轴节点。

    只用**已有元数据**组装（标题 + 能从 arxiv_id 推出的年份 + 命中片段数），
    不让模型再生成一份时间线：`papers` 表里根本没有 year 列，让模型复述年份
    等于请它编年份 —— 而"方法演进"这条线本来就要靠正文引用去支撑。
    """

    paper_id: int | None = None
    title: str = ""
    year: int | None = Field(default=None, description="从 arxiv_id 的 YYMM 前缀推导；推不出为 null")
    arxiv_id: str | None = None
    chunks: int = Field(default=0, description="本次对比中该论文命中的片段数")


class RetrievedChunkOut(BaseModel):
    chunk_id: int
    paper_id: int
    title: str = ""
    section: str | None = None
    page: int | None = None
    bbox: Any | None = None
    score: float = 0.0
    rerank_score: float | None = None
    sources: list[str] = Field(default_factory=list)
    preview: str = Field(default="", description="正文前 300 字，避免整块回传")


class CitationOut(BaseModel):
    """一条可追溯的引用。`verified` 来自 NLI/词法蕴含校验。"""

    marker: int | None = None
    chunk_id: int | None = None
    paper_id: int | None = None
    title: str = ""
    section: str | None = None
    page: int | None = None
    bbox: Any | None = None
    quote: str = ""
    answer_span: str = Field(default="", description="答案里引用它的那句话（去掉编号）")
    nli_score: float = 0.0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="= nli_score，按规格另给一个名字")
    verified: bool = False
    attribution_method: AttributionMethod = "self_citation"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_start(self) -> int | None:
        """`page` 的别名。

        规格里前端按 `page_start` 读页码，而本模型历史上叫 `page`；两边都认才不会
        出现"引用页码显示为空、点引用跳不到页"（阶段 5 遗留第 1 条）。
        """
        return self.page


class SourceTraceOut(BaseModel):
    """对外溯源契约 —— 前端 PDF 高亮定位所需的一切都在这里。"""

    answer_span: str = Field(default="", description="答案中被该证据支撑的文本片段")
    chunk_id: int | None = None
    paper_id: int | None = None
    page: int | None = Field(default=None, description="PDF 页码，用于跳转")
    bbox: Any | None = Field(default=None, description="命中块坐标，用于 PDF.js 框选")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    attribution_method: AttributionMethod = "self_citation"


class SourceSpan(BaseModel):
    """qa_history.sources 里的一项 —— 答案某段文字与某个 chunk 的对应关系。"""

    answer_span: tuple[int, int] | None = Field(default=None, description="答案正文中的字符区间，用于高亮")
    chunk_id: int | None = None
    paper_id: int | None = None
    page: int | None = None
    bbox: Any | None = None
    confidence: float = 0.0
    method: Literal["nli", "lexical", "self_citation", "hybrid"] = "nli"


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
    sources: list[SourceTraceOut] = Field(default_factory=list, description="溯源列表（规格契约，含 bbox/confidence）")
    grounding_ratio: float = 0.0
    faithfulness: float | None = Field(default=None, description="Reflector 的忠实度评分；无评审时为 null")
    passed_grounding: bool = Field(default=False, description="grounding_ratio ≥ 阈值且无幻觉引用编号")
    unsupported_claims: list[str] = Field(default_factory=list)
    guardrail_flags: list[str] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    reflections: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list, description="按年份升序的方法演进时间轴（跨篇对比用）")
    retrieved: list[RetrievedChunkOut] = Field(default_factory=list)
    debug: RetrievalDebug | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    history_id: int | None = Field(default=None, description="本次问答在 qa_history 里的主键")


class TraceRequest(BaseModel):
    """对一段既有文本做溯源校验（不生成，只校验）。"""

    answer: str = Field(min_length=1)
    chunk_ids: list[int] = Field(default_factory=list, description="允许被引用的 chunk 白名单")
    query: str = ""


class TraceResult(BaseModel):
    grounding_ratio: float = 0.0
    sentences_total: int = 0
    sentences_supported: int = 0
    terms_total: int = 0
    terms_supported: int = 0
    passed: bool = False
    phantom_markers: list[int] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    uncited_claims: list[str] = Field(default_factory=list)
    number_mismatches: list[str] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    sources: list[SourceTraceOut] = Field(default_factory=list)


class QAHistoryOut(BaseModel):
    """一条问答留痕。`sources` 是完整的溯源数组（结构见 app.models.qa_history）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    paper_ids: list[int] = Field(default_factory=list)
    intent: str | None = None
    question: str
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    grounding_ratio: float | None = None
    faithfulness: float | None = None
    created_at: datetime


__all__ = [
    "AskRequest",
    "AskResult",
    "AttributionMethod",
    "CitationOut",
    "QAHistoryOut",
    "RetrievalDebug",
    "RetrievedChunkOut",
    "SourceSpan",
    "SourceTraceOut",
    "TimelineItem",
    "TraceRequest",
    "TraceResult",
]
