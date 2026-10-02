"""统一 LLM 客户端。

所有模型调用都经过这里：openai SDK + `base_url` 切换端点（DeepSeek / Qwen / GPT-4o 兼容），
按 **角色** 解析模型名，实现 Planner 用强模型、Executor 用性价比模型的成本优化。

四种调用模式
------------
| 方法            | 返回            | 用途 |
|-----------------|-----------------|------|
| `chat()`        | `ChatResult`    | 普通对话。带 `tools` 时自动解析 function calling |
| `complete()`    | `str`           | `chat()` 的纯文本快捷方式（节点内部大量使用） |
| `stream()`      | `AsyncIterator[str]` | SSE 流式增量文本 |
| `structured()`  | `BaseModel`     | JSON Mode + Pydantic 校验（Planner / Reflector 的打分与计划） |

`chat()` 返回对象而不是裸字符串，是因为 function calling 的 `tool_calls` 没法塞进 `str`。
为了少让人踩坑，`ChatResult` 实现了 `__str__` 和 `.text`，`f"{result}"` 与
`result.text` 都能直接拿到正文。

用法：
    llm = get_llm()
    text = await llm.complete(Role.PLANNER, messages)
    async for delta in llm.stream(Role.EXECUTOR, messages):
        ...
    plan = await llm.structured(Role.PLANNER, messages, PlanSchema)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any, TypeVar

import tiktoken
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.errors import LLMError, LLMRateLimitError
from app.core.logging import logger

Message = dict[str, Any]
TModel = TypeVar("TModel", bound=BaseModel)


class Role(StrEnum):
    """模型角色。不同角色可以指向不同模型，这是成本控制的主要旋钮。"""

    INTENT = "intent"  # 意图分类：短输入短输出，可用最便宜的模型
    PLANNER = "planner"  # 规划：需要强推理
    EXECUTOR = "executor"  # 执行/生成：调用量最大，用性价比模型
    REVIEWER = "reviewer"  # 批判性评分：需要稳定判断
    UTILITY = "utility"  # 查询改写、实体抽取等杂活


def resolve_model(role: Role | str) -> str:
    role = Role(role)
    return {
        Role.INTENT: settings.LLM_MODEL_REVIEWER,
        Role.PLANNER: settings.LLM_MODEL_PLANNER,
        Role.EXECUTOR: settings.LLM_MODEL_EXECUTOR,
        Role.REVIEWER: settings.LLM_MODEL_REVIEWER,
        Role.UTILITY: settings.LLM_MODEL_EXECUTOR,
    }[role]


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1
        slot = self.by_model.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        slot["prompt_tokens"] += prompt
        slot["completion_tokens"] += completion
        slot["calls"] += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def snapshot(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total": self.total_tokens,
        }


@dataclass(slots=True)
class ToolCall:
    """模型要求调用的一个函数。`arguments` 保持**原始字符串** —— 是否合法 JSON
    由调用方（tool_node）决定，客户端不替它吞掉解析错误。"""

    id: str
    name: str
    arguments: str

    def parsed_arguments(self) -> dict[str, Any]:
        import json

        try:
            data = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise LLMError(f"工具 {self.name} 的参数不是合法 JSON: {self.arguments[:200]}") from exc
        return data if isinstance(data, dict) else {"value": data}


@dataclass(slots=True)
class ChatResult:
    """一次对话回复。`content` 可能为空（纯 function calling 轮次）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content

    def __str__(self) -> str:  # 让 `str(await llm.chat(...))` 直接可用
        return self.content

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# 离线/测试环境下也要能 import，所以编码器缺失时退化成估算
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENCODER = None


def count_tokens(text: str) -> int:
    """估算 token 数。tiktoken 不可用（离线）时按 4 字符 ≈ 1 token 粗估。

    空串返回 0 —— 它有明确的语义（没有 token），不要为了让断言好看而改成 1。
    """
    if not text:
        return 0
    if _ENCODER is None:
        return max(1, len(text) // 4)
    return len(_ENCODER.encode(text))


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self._base_url = base_url or settings.LLM_BASE_URL
        self._timeout = timeout or settings.LLM_TIMEOUT
        # 注入 httpx 客户端只为一件事：单测里塞 MockTransport。生产走 SDK 自建连接池。
        self._http_client = http_client
        self._client: AsyncOpenAI | None = None
        self.usage = Usage()

    # ---------------------------------------------------------------- 连接
    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._api_key:
                raise LLMError("未配置 LLM_API_KEY，请在 .env 中填写")
            extra: dict[str, Any] = {"http_client": self._http_client} if self._http_client is not None else {}
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,  # 重试交给 tenacity，便于统一策略
                **extra,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------------------------------------------------------- 底层
    @retry(
        retry=retry_if_exception_type((APIConnectionError, RateLimitError)),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _create_raw(self, **kwargs: Any) -> Any:
        """只负责"发请求 + 退避重试"。

        重试必须包在**映射之前**：一旦把 `APIConnectionError` / `RateLimitError`
        转成业务异常，`retry_if_exception_type` 就再也匹配不到，重试变成死代码。
        """
        return await self.client.chat.completions.create(**kwargs)

    async def _create(self, **kwargs: Any) -> Any:
        try:
            return await self._create_raw(**kwargs)
        except RateLimitError as exc:
            logger.warning("LLM 限流，重试耗尽: {}", exc)
            raise LLMRateLimitError("模型限流，请稍后重试") from exc
        except APIStatusError as exc:
            raise LLMError(f"模型返回 {exc.status_code}: {exc.message}") from exc
        except APIConnectionError as exc:
            raise LLMError(f"无法连接模型端点 {self._base_url}") from exc

    def _record(self, model: str, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.add(model, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)

    @staticmethod
    def _build_kwargs(
        model: str,
        messages: list[Message],
        *,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
            **extra,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        return kwargs

    # ---------------------------------------------------------------- chat
    async def chat(
        self,
        messages: list[Message],
        *,
        role: Role | str = Role.EXECUTOR,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **extra: Any,
    ) -> ChatResult:
        """普通对话。传了 `tools` 就走 function calling，`tool_calls` 一并解析出来。

        参数顺序刻意把 `messages` 放第一位 —— 绝大多数调用点关心的只是"说什么"。
        """
        model = model or resolve_model(role)
        resp = await self._create(
            **self._build_kwargs(
                model,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                extra=extra,
            )
        )
        self._record(model, resp)
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(
                id=str(getattr(tc, "id", "") or ""),
                name=str(getattr(tc.function, "name", "") or ""),
                arguments=str(getattr(tc.function, "arguments", "") or ""),
            )
            for tc in (getattr(msg, "tool_calls", None) or [])
        ]
        u = getattr(resp, "usage", None)
        return ChatResult(
            content=getattr(msg, "content", None) or "",
            tool_calls=calls,
            model=model,
            finish_reason=getattr(choice, "finish_reason", None),
            usage={
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            }
            if u
            else {},
        )

    async def complete(
        self,
        role: Role | str,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **extra: Any,
    ) -> str:
        """纯文本快捷方式。内部就是 `chat()`，只是把正文抽出来。"""
        result = await self.chat(
            messages,
            role=role,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **extra,
        )
        return result.content

    # ---------------------------------------------------------------- stream
    async def stream(
        self,
        role: Role | str,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> AsyncIterator[str]:
        """流式产出增量文本。调用方只需 `async for`。"""
        model = model or resolve_model(role)
        stream = await self._create(
            model=model,
            messages=messages,
            temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
            **extra,
        )
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                self._record(model, chunk)
            if not chunk.choices:
                continue
            piece = getattr(chunk.choices[0].delta, "content", None)
            if piece:
                yield piece

    async def stream_with_usage(
        self, role: Role | str, messages: list[Message], **kwargs: Any
    ) -> AsyncIterator[tuple[str, Usage]]:
        """流式产出 (增量文本, 当前累计用量)。"""
        async for piece in self.stream(role, messages, **kwargs):
            yield piece, self.usage

    # ---------------------------------------------------------------- structured
    @staticmethod
    def _schema_hint(schema: type[BaseModel]) -> str:
        """把 Pydantic schema 塞进 system 提示。

        为什么需要这层：`response_format={"type": "json_object"}` 只保证**是合法 JSON**，
        不保证**字段对**。DeepSeek / Qwen 的 JSON Mode 都只到这一步，
        所以必须同时给出字段说明，再用 Pydantic 校验兜住剩下的错。
        """
        import json

        raw = schema.model_json_schema()
        return (
            "你必须只输出一个 JSON 对象，不要输出任何解释、前言或 Markdown 代码块围栏。\n"
            f"JSON 必须严格符合以下 JSON Schema：\n{json.dumps(raw, ensure_ascii=False, indent=2)}"
        )

    async def structured(
        self,
        messages: list[Message],
        schema: type[TModel],
        *,
        role: Role | str = Role.PLANNER,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        repair_attempts: int = 1,
        use_json_schema: bool = False,
        **extra: Any,
    ) -> TModel:
        """JSON Mode + Pydantic 校验。Planner 出计划、Reflector 打分都走这里。

        校验失败会带着**原始输出与校验错误**再问一次模型（`repair_attempts` 次），
        而不是直接把 ValidationError 抛给上层 —— 结构化输出失败最常见的原因就是
        模型多加了一句"好的，以下是 JSON："，纠正一次基本就对了。

        `use_json_schema=True` 时改发 `json_schema` 格式（部分端点支持，
        由服务端强约束），否则用兼容性最好的 `json_object`。
        """
        model = model or resolve_model(role)
        convo: list[Message] = [{"role": "system", "content": self._schema_hint(schema)}, *messages]

        response_format: dict[str, Any] = (
            {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
            }
            if use_json_schema
            else {"type": "json_object"}
        )

        last_error: Exception | None = None
        for attempt in range(repair_attempts + 1):
            raw = await self.complete(
                role,
                convo,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                **extra,
            )
            try:
                return schema.model_validate_json(_strip_code_fence(raw))
            except (ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning("结构化输出校验失败（第 {} 次）：{}", attempt + 1, str(exc)[:300])
                if attempt >= repair_attempts:
                    break
                convo = [
                    *convo,
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": (
                            "上面的输出无法通过 JSON Schema 校验，错误如下：\n"
                            f"{str(exc)[:1000]}\n\n"
                            "请只重新输出修正后的 JSON 对象，不要任何其他文字。"
                        ),
                    },
                ]

        raise LLMError(f"结构化输出在 {repair_attempts + 1} 次尝试后仍不合法: {last_error}")


def _strip_code_fence(raw: str) -> str:
    """剥掉模型自作主张加的 ```json 围栏。

    不是洁癖：DeepSeek / Qwen 在 JSON Mode 下偶尔仍会包一层 ```，
    而 `model_validate_json` 遇到 ``` 会直接报 JSONDecodeError。
    """
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient()


def usage_snapshot() -> dict[str, int]:
    """`get_llm().usage` 的快照。

    **`get_llm()` 是 `@lru_cache` 单例，它的 `usage` 是进程级累计。**
    任何"这一轮/这一次花了多少 token"都必须用 `usage_snapshot()` +
    `usage_delta()` 取增量，不能直接读 `usage.total_tokens` ——
    那会随服务运行时长单调增长。实测同一进程里连打两次闲聊，`done` 帧分别是
    481719 / 484886 tokens，差值 3167 才是第二轮的**真实**消耗；
    直接上报会表现为"问一句闲聊花了 48 万 token"，落库的 `run.tokens_used` 同样失真。

    注：delta 依赖"同一时刻只有一个在跑的轮次"。本机是单人桌面部署，该前提成立；
    将来若并发跑多路流式，需要改成每请求独立的累加器（ContextVar）。
    """
    return get_llm().usage.snapshot()


def usage_delta(base: dict[str, int]) -> dict[str, int]:
    """相对 `usage_snapshot()` 结果的增量，出口统一用这个形状。"""
    after = get_llm().usage
    return {
        "total_tokens": after.total_tokens - base["total"],
        "calls": after.calls - base["calls"],
    }


__all__ = [
    "ChatResult",
    "LLMClient",
    "Message",
    "Role",
    "ToolCall",
    "Usage",
    "get_llm",
    "usage_delta",
    "usage_snapshot",
    "count_tokens",
    "get_llm",
    "resolve_model",
]
