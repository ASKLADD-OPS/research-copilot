"""写作辅助相关模型。

前半段（templates / draft / translate）是工作台右栏的"单段生成"；
后半段（outline / expand / references / diagram）是阶段 11 的写作台四端点 ——
它的输出契约比单段生成大一号：一次给一整套章节、逐条可核查的引用、以及图片。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.qa import CitationOut

SectionKind = Literal[
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiment",
    "conclusion",
    "rebuttal",
    "review",
    "summary",
]


class WritingTemplateOut(BaseModel):
    kind: SectionKind
    name: str
    description: str
    default_length: int = Field(description="建议字数")
    outline: list[str] = Field(default_factory=list, description="该章节的写作要点")


class WriteRequest(BaseModel):
    kind: SectionKind = "summary"
    topic: str = Field(min_length=1, max_length=2000, description="写什么")
    paper_ids: list[str] = Field(default_factory=list, description="以此为据的论文；空=全库检索")
    language: Literal["zh", "en"] = "zh"
    target_length: int | None = Field(default=None, ge=100, le=8000)
    style: str = Field(default="", description='额外的风格要求，如"学术、克制、避免口语"')
    use_retrieval: bool = Field(default=True, description="是否走 RAG 取证据（关闭则纯生成）")


class WriteResult(BaseModel):
    kind: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    grounding_ratio: float = 0.0
    retrieved_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    target: Literal["zh", "en"] = "zh"
    keep_terms: bool = Field(default=True, description="保留术语原文并在括号内给出译名")
    bilingual: bool = Field(default=False, description="返回逐段对照")


class BilingualPair(BaseModel):
    source: str
    target: str


class TranslateResult(BaseModel):
    text: str
    pairs: list[BilingualPair] = Field(default_factory=list, description="bilingual=true 时有值")
    glossary: list[dict[str, str]] = Field(default_factory=list, description="术语对照表")
    usage: dict[str, Any] = Field(default_factory=dict)


# ==================================================================== 阶段 11：无幻觉 References
# 先定义 Reference*，因为 `OutlineResult.references` 要用它 —— Pydantic v2 在类体创建时
# 就解析注解，前向引用没解析到会直接抛 PydanticUndefinedAnnotation，靠 model_rebuild()
# 补救是补救不了的。
ReferenceStatus = Literal[
    "ok",  # 三关全过
    "weak",  # chunk 存在、数字一致，但语义蕴含不过阈值
    "phantom",  # 编号根本不在检索上下文里（凭空编的）
    "chunk_missing",  # 编号有，但 chunk_id 在库中不存在
    "metadata_missing",  # 论文不在库里 / 元数据缺失（作者、年份、会议一个都没有）
]


class CitationCheck(BaseModel):
    """一条引用的逐项校验结果。三关各自的布尔值都留出来，前端要能解释"为什么没过"。"""

    marker: int
    claim: str = Field(default="", description="引用它的那句话（已去掉编号）")
    chunk_id: int | None = None
    paper_id: int | None = None
    page: int | None = None
    quote: str = ""
    nli_score: float = 0.0
    numbers_ok: bool = False
    chunk_exists: bool = False
    content_consistent: bool = False
    metadata_ok: bool = False
    status: ReferenceStatus = "phantom"
    reason: str = ""


class ReferenceOut(BaseModel):
    """一条参考文献 —— 所有字段都来自 `papers` 表，没有一个是模型写的。"""

    marker: int
    paper_id: int | None = None
    chunk_id: int | None = None
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    section: str | None = None
    page: int | None = None
    quote: str = ""
    formatted: str = Field(default="", description="按 language 排好的条目文本")


class ReferenceRequest(BaseModel):
    content: str = Field(min_length=1, max_length=40000, description="待校验正文（含 [n] 标记）")
    query: str = Field(default="", max_length=1000, description="写这段正文时用的检索式；空则截取 content 前 600 字")
    paper_ids: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="写正文那一步返回的 citations，回传可省一次检索并保证编号映射一致",
    )
    top_k: int = Field(default=20, ge=1, le=50)
    remove_invalid: bool = Field(default=True, description="校验失败：True=摘掉编号，False=原地标成 [citation needed]")
    language: Literal["zh", "en"] = "zh"


class ReferenceResult(BaseModel):
    content: str = Field(description="清洗后的正文")
    references: list[ReferenceOut] = Field(default_factory=list)
    bibliography: list[str] = Field(default_factory=list)
    checks: list[CitationCheck] = Field(default_factory=list)
    total_markers: int = 0
    ok_count: int = 0
    invalid_count: int = 0
    removed_markers: list[int] = Field(default_factory=list)
    flagged_markers: list[int] = Field(default_factory=list)
    grounding_ratio: float = 0.0


# ==================================================================== 阶段 11：论文框架
class OutlineRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=4000, description="Idea 描述：想做什么研究")
    paper_ids: list[str] = Field(default_factory=list, description="只是以此为据的论文；空=全库检索")
    language: Literal["zh", "en"] = "zh"
    sections: list[str] = Field(
        default_factory=list,
        description="自定义章节 key；空=Abstract/Introduction/Related Work/Method/Experiment/Conclusion",
    )
    evidence_per_section: int = Field(default=5, ge=1, le=20, description="每节检索几块证据")
    draft: bool = Field(default=True, description="是否逐节生成草稿（关闭则只出框架，省 token）")


class OutlineSection(BaseModel):
    """大纲里的一节。`draft` 里的 `[n]` 与 `citations` 的 `marker` 一一对应。"""

    key: str
    title: str
    brief: str = Field(default="", description="这一节该写什么")
    points: list[str] = Field(default_factory=list, description="要点清单")
    draft: str = ""
    citations: list[CitationOut] = Field(default_factory=list)
    grounding_ratio: float = 0.0
    evidence_count: int = 0
    removed_markers: list[int] = Field(default_factory=list, description="模型编造、已被强制摘掉的引用编号")


class OutlineResult(BaseModel):
    title: str = ""
    idea: str
    language: str = "zh"
    rationale: str = Field(default="", description="Planner 为什么这样分节")
    sections: list[OutlineSection] = Field(default_factory=list)
    references: list[ReferenceOut] = Field(default_factory=list, description="全篇去重后的参考文献")
    grounding_ratio: float = Field(default=0.0, description="各节按篇幅加权后的有据率")
    phantom_markers: list[int] = Field(default_factory=list, description="全篇检出并清除的幻觉引用")
    usage: dict[str, Any] = Field(default_factory=dict)


class ExpandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000, description="要扩写的段落 / 编辑器选区")
    context: str = Field(default="", max_length=8000, description="选区前后的正文，供模型衔接")
    instruction: str = Field(default="", max_length=1000, description="额外要求，如'补一组消融对比'")
    section: SectionKind = "related_work"
    language: Literal["zh", "en"] = "zh"
    paper_ids: list[str] = Field(default_factory=list)
    query: str = Field(default="", max_length=1000, description="检索式；空则截取 text 前 400 字")
    target_length: int | None = Field(default=None, ge=100, le=4000)
    evidence_top_k: int = Field(default=8, ge=1, le=20)
    marker_offset: int = Field(
        default=0,
        ge=0,
        le=999,
        description="把本节引用编号整体后移这么多；编辑器里插入到已有正文之后时，传文档当前最大编号",
    )


class ExpandResult(BaseModel):
    text: str
    citations: list[CitationOut] = Field(default_factory=list)
    grounding_ratio: float = 0.0
    retrieved_count: int = 0
    removed_markers: list[int] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


# ==================================================================== 阶段 11：图表
DiagramKind = Literal["tikz", "graphviz", "mermaid", "matplotlib"]


class DiagramRequest(BaseModel):
    kind: DiagramKind = "mermaid"
    instruction: str = Field(min_length=1, max_length=2000, description="要画什么，如'系统的四层架构'")
    context: str = Field(default="", max_length=6000, description="据以作画的材料（如方法段落的要点）")
    data: dict[str, Any] | None = Field(
        default=None,
        description="matplotlib 专用：{chart_type, title, xlabel, ylabel, categories, series:[{name,data}]}；"
        "给了就不让模型现编数据",
    )


class DiagramResult(BaseModel):
    kind: DiagramKind
    source: str = Field(description="TikZ / DOT / Mermaid / 绘图脚本源码 —— 无论有没有渲染出来都有")
    caption: str = ""
    image: str | None = Field(default=None, description="base64 PNG（不含 data: 前缀）；渲染器缺失时为 null")
    mime: str = "image/png"
    renderer: str = Field(default="", description="实际用的渲染器；none = 只给了源码")
    warning: str = Field(default="", description="渲染被降级/跳过时的原因，如实告知而不是假装成功")
    elapsed_ms: int = 0


__all__ = [
    "BilingualPair",
    "CitationCheck",
    "DiagramKind",
    "DiagramRequest",
    "DiagramResult",
    "ExpandRequest",
    "ExpandResult",
    "OutlineRequest",
    "OutlineResult",
    "OutlineSection",
    "ReferenceOut",
    "ReferenceRequest",
    "ReferenceResult",
    "ReferenceStatus",
    "SectionKind",
    "TranslateRequest",
    "TranslateResult",
    "WriteRequest",
    "WriteResult",
    "WritingTemplateOut",
]
