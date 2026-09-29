"""结构化输出：把 LLM 的自由文本收敛成 Pydantic 模型。

LLM 返回 JSON 是不可靠的（多包裹一层 ```json、尾随逗号、字段名漂移）。
这里做三件事：① 强制 JSON 模式；② 解析失败时自动修复重试一次；
③ 校验失败把错误回喂给模型（judge → repair），最终仍失败则抛业务异常。

Agent 节点用它拿意图分类、计划、评审分数这类必须结构化的结果。
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import LLMError
from app.core.logging import logger
from app.llm.client import LLMClient, Message, Role, get_llm

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """从可能带 markdown 围栏或前后缀说明的文本里取出 JSON 主体。"""
    text = (text or "").strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _loose_loads(text: str) -> dict | list:
    """容忍常见 JSON 瑕疵：尾随逗号、中文引号、单引号。"""
    body = extract_json(text)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        patched = re.sub(r",\s*([}\]])", r"\1", body)  # 去尾随逗号
        patched = patched.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        return json.loads(patched)


async def complete_structured(
    schema: type[T],
    messages: list[Message],
    *,
    role: Role | str = Role.UTILITY,
    llm: LLMClient | None = None,
    max_repair: int = 1,
    temperature: float = 0.0,
    **kwargs: object,
) -> T:
    """要一个符合 `schema` 的结构化结果。

    失败路径：解析/校验失败 → 把错误与原输出回喂一次 → 再失败则抛 LLMError。
    """
    llm = llm or get_llm()
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)

    sys_hint = {
        "role": "system",
        "content": (
            f"你只输出一个 JSON 对象，不要任何解释、不要 markdown 围栏。必须严格符合以下 JSON Schema：\n{schema_json}"
        ),
    }
    convo: list[Message] = [sys_hint, *messages]

    raw = await llm.complete(
        role,
        convo,
        temperature=temperature,
        response_format={"type": "json_object"},
        **kwargs,  # type: ignore[arg-type]
    )

    last_error: Exception | None = None
    for attempt in range(max_repair + 1):
        try:
            return schema.model_validate(_loose_loads(raw))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            logger.warning("结构化输出解析失败（第 {} 次）: {}", attempt + 1, exc)
            if attempt >= max_repair:
                break
            convo = [
                *convo,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (f"上面的输出无法通过校验：{exc}\n请只重新输出修正后的 JSON，不要解释。"),
                },
            ]
            raw = await llm.complete(
                role,
                convo,
                temperature=0.0,
                response_format={"type": "json_object"},
                **kwargs,  # type: ignore[arg-type]
            )

    raise LLMError(f"结构化输出失败（schema={schema.__name__}）: {last_error}")
