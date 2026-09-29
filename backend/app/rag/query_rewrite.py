"""查询改写。

CRAG 判为 ambiguous 时，问题往往出在"用户的话和论文的话不是一个说法"：
用户问"这篇的注意力机制怎么改的"，论文里写的是"we replace multi-head attention with ..."。
改写就是把这个 gap 补上。

三种策略
--------
expand  —— 同义扩展 / 术语对齐（默认，最稳）
decompose —— 把复合问题拆成子查询（"A 和 B 的方法差异" → 分别问 A、问 B）
hyde   —— 生成一段假想答案再拿它去检索（HyDE）。对语义检索有效，
          但会**引入模型幻觉进检索条件**，所以默认关闭，仅在 expand 连续失败时启用
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import logger
from app.llm.client import LLMClient, Message, Role, get_llm
from app.llm.structured import complete_structured

_SYSTEM = (
    "你是学术检索助手。用户的提问将用于检索一篇或多篇论文的正文片段。"
    "你的任务是把口语化、含糊或术语不统一的提问，改写成**适合向量检索**的形式：\n"
    "1. 保留原问题的全部约束（时间、方法、数据集、对比对象），不要扩写问题范围；\n"
    "2. 把口语词替换成论文里更可能出现的学术表述（中英术语同时给出）；\n"
    "3. 不要回答问题，不要加解释，不要加引号；\n"
    "4. 只输出改写后的检索式，一行。"
)


class RewritePlan(BaseModel):
    query: str = Field(description="改写后的检索式")
    rationale: str = Field(default="", description="改写理由，一句话")


class SubQueries(BaseModel):
    queries: list[str] = Field(default_factory=list, description="拆解出的子查询，2~4 条")


async def rewrite_query(
    query: str,
    *,
    history: list[Message] | None = None,
    llm: LLMClient | None = None,
) -> str:
    """同义扩展 / 术语对齐。失败时**原样返回**，保证检索至少还能跑一次。"""
    llm = llm or get_llm()
    messages: list[Message] = [{"role": "system", "content": _SYSTEM}]
    if history:
        messages.extend(history[-4:])  # 只带最近两轮，避免把历史噪声带进检索式
    messages.append({"role": "user", "content": f"原始提问：{query}\n\n请输出改写后的检索式。"})
    try:
        plan = await complete_structured(RewritePlan, messages, role=Role.UTILITY, llm=llm)
        if plan.query.strip():
            logger.debug("查询改写: {!r} → {!r}", query[:60], plan.query[:60])
            return plan.query.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询改写失败，沿用原查询：{}", exc)
    return query


async def decompose_query(query: str, *, max_sub: int = 4, llm: LLMClient | None = None) -> list[str]:
    """把复合问题拆成子查询。用于 cross_paper_reasoning —— 每个子查询独立检索再合并。"""
    llm = llm or get_llm()
    prompt = (
        "把下面的复合提问拆成 2~4 个可独立检索的子查询，覆盖全部约束条件。\n"
        "若提问本身就是单一问题，就只返回它自己。\n\n"
        f"提问：{query}"
    )
    try:
        out = await complete_structured(SubQueries, [{"role": "user", "content": prompt}], role=Role.UTILITY, llm=llm)
        subs = [q.strip() for q in out.queries if q.strip()][:max_sub]
        return subs or [query]
    except Exception as exc:  # noqa: BLE001
        logger.warning("子查询拆解失败，退回原查询：{}", exc)
        return [query]


async def hyde_query(query: str, *, llm: LLMClient | None = None) -> str:
    """HyDE：先让模型编一段"假想答案"，用它检索。

    注意：假想答案里的具体数字/结论是**模型编的**，只能当检索用的语义向量，
    绝不能进上下文或引用。因此返回的是拼接后的检索式，原文另行丢弃。
    """
    llm = llm or get_llm()
    prompt = (
        "请写一段 2~3 句话的学术风格段落，读起来像是这个问题的标准答案。"
        "不需要事实准确，只需要用词贴近论文正文。\n\n"
        f"问题：{query}"
    )
    try:
        hypothetical = await llm.complete(Role.UTILITY, [{"role": "user", "content": prompt}], temperature=0.7)
        return f"{query}\n{hypothetical.strip()}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("HyDE 生成失败，退回原查询：{}", exc)
        return query
