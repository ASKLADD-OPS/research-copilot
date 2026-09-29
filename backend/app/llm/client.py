"""统一 LLM 客户端。

所有模型调用都经过这里：openai SDK + `base_url` 切换端点（DeepSeek / Qwen / GPT-4o 兼容），
按 **角色** 解析模型名，实现 Planner 用强模型、Executor 用性价比模型的成本优化。

用法：
    llm = LLMClient()
    text = await llm.complete("planner", messages)
    async for delta in llm.stream("executor", messages):
        ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

import tiktoken
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.errors import LLMError, LLMRateLimitError
from app.core.logging import logger

Message = dict[str, Any]


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


# 离线/测试环境下也要能 import，所以编码器缺失时退化成估算
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENCODER = None


def count_tokens(text: str) -> int:
    """估算 token 数。tiktoken 不可用（离线）时按 4 字符 ≈ 1 token 粗估。"""
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
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.LLM_API_KEY
        self._base_url = base_url or settings.LLM_BASE_URL
        self._timeout = timeout or settings.LLM_TIMEOUT
        self._client: AsyncOpenAI | None = None
        self.usage = Usage()

    # ---------------------------------------------------------------- 连接
    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._api_key:
                raise LLMError("未配置 LLM_API_KEY，请在 .env 中填写")
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,  # 重试交给 tenacity，便于统一策略
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---------------------------------------------------------------- 调用
    @retry(
        retry=retry_if_exception_type((APIConnectionError, RateLimitError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _create(self, **kwargs: Any) -> Any:
        try:
            return await self.client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            logger.warning("LLM 限流，退避重试: {}", exc)
            raise LLMRateLimitError("模型限流，请稍后重试") from exc
        except APIStatusError as exc:
            raise LLMError(f"模型返回 {exc.status_code}: {exc.message}") from exc
        except APIConnectionError as exc:
            raise LLMError(f"无法连接模型端点 {self._base_url}") from exc

    def _record(self, model: str, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.add(model, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)

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
        """一次性返回完整文本。"""
        model = model or resolve_model(role)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
            **extra,
        }
        if response_format:
            kwargs["response_format"] = response_format
        resp = await self._create(**kwargs)
        self._record(model, resp)
        return resp.choices[0].message.content or ""

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
        """流式产出增量文本。最后一块之后产出空串用于收尾，调用方只需 `async for`。"""
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
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece

    async def stream_with_usage(
        self, role: Role | str, messages: list[Message], **kwargs: Any
    ) -> AsyncIterator[tuple[str, Usage]]:
        """流式产出 (增量文本, 当前累计用量)。用量含 `stream_options.include_usage` 返回的真实值。"""
        async for piece in self.stream(role, messages, **kwargs):
            yield piece, self.usage


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient()
