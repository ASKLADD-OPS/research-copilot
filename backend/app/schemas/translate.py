"""学术翻译相关模型。

与 `/writing/translate`（工作台右栏那个"顺手翻一段"）的区别在**粒度**：
那边是整段进整段出，这边**逐段翻译并回传段落 ID**，前端才能把左右两栏按段落对齐、
滚动同步。多出来的 `glossary` / `passive` 也是同一条线 —— 它们服务的是"改论文"，
不是"看一眼译文"。

`TranslateParagraphsRequest` 与写作那套的 `TranslateRequest` 名字刻意不同：
两者字段几乎一样但语义不同（一个 `bilingual` 出对照对，一个出段落映射），
共用一个模型会让两边的改动互相绊住。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Lang = Literal["zh", "en"]


class GlossaryEntry(BaseModel):
    """一条强制术语映射。"""

    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)


class GlossaryParseResult(BaseModel):
    """术语表文件的解析结果。解析失败的行走 `skipped`，不静默吞掉。"""

    entries: list[GlossaryEntry] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list, description="认不出成对关系的行，原样回传")
    total_lines: int = 0


class TranslateParagraphsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=40000, description="整篇原文，按空行分段")
    target: Lang = "zh"
    glossary: list[GlossaryEntry] = Field(default_factory=list, description="术语表（可由 /translate/glossary 解析上传文件得到）")
    keep_terms: bool = Field(default=True, description="术语首次出现时中英对照")
    passive: bool = Field(default=False, description="偏好被动语态与无人称表述")


class TranslatedParagraph(BaseModel):
    """一段原文与它的译文。`id` 是前端做滚动同步时的锚点。"""

    id: str = Field(description="段落 ID，形如 p1 / p2，与 index 一一对应")
    index: int = Field(description="段落序号，从 0 起")
    source: str
    target: str


class TranslateParagraphsResult(BaseModel):
    language: str = Field(description="译文语言 zh | en")
    paragraphs: list[TranslatedParagraph] = Field(default_factory=list)
    source_text: str = Field(default="", description="原文（按分割结果重新拼接）")
    target_text: str = Field(default="", description="译文拼接")
    glossary: list[GlossaryEntry] = Field(default_factory=list, description="本次实际生效的术语表")
    unused_terms: list[str] = Field(default_factory=list, description="术语表里没在原文出现的词条（原文侧）")
    usage: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "GlossaryEntry",
    "GlossaryParseResult",
    "Lang",
    "TranslateParagraphsRequest",
    "TranslateParagraphsResult",
    "TranslatedParagraph",
]
