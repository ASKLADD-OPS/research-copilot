"""写作辅助相关模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
