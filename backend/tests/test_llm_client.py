"""LLM 客户端测试 —— 全部 mock，不打真模型、不烧 token。

用 `httpx.MockTransport` 注入到 openai SDK 里，所以走的是**真的** SDK 解析路径
（真的 `ChatCompletion` 对象、真的 `tool_calls` 结构、真的 SSE 解帧），
只是网络那一层被换掉了。

为什么不用 respx：本机有全局 HTTP 代理（`HTTP_PROXY` 指向 127.0.0.1），
respx 的拦截会被绕过，请求真的发到网络上再失败 —— 那样测的就不是自己的逻辑了。
MockTransport 在 transport 层直接短路，与代理无关。

覆盖验收标准 5：chat() / stream() / structured() / function calling 四个模式。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.core.errors import LLMError
from app.llm.client import ChatResult, LLMClient, Role, ToolCall, _strip_code_fence, count_tokens

pytestmark = pytest.mark.unit

BASE = "https://mock-llm.test/v1"
ENDPOINT = "/v1/chat/completions"

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """记录每次请求的 body，方便断言"我们到底发出去了什么"。"""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("mock 收到的请求比预期多")
        return self.responses.pop(0)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]

    @property
    def call_count(self) -> int:
        return len(self.requests)


def make_client(handler: Handler, **kw) -> LLMClient:
    return LLMClient(
        api_key="sk-mock",
        base_url=BASE,
        timeout=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kw,
    )


def completion(
    content: str | None = "hi",
    *,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


# ---------------------------------------------------------------- chat()
async def test_chat_returns_content_and_usage():
    client = make_client(Recorder([completion("你好，这是回答")]))

    result = await client.chat([{"role": "user", "content": "你好"}])

    assert isinstance(result, ChatResult)
    assert result.content == "你好，这是回答"
    assert str(result) == "你好，这是回答"  # 让 f"{result}" 也能直接用
    assert result.text == "你好，这是回答"
    assert result.tool_calls == []
    assert result.wants_tools is False
    assert result.usage["prompt_tokens"] == 11
    # 用量记在客户端上，供 /qa/ask 与前端展示
    assert client.usage.calls == 1
    assert client.usage.total_tokens == 18


async def test_complete_is_plain_text_shortcut():
    client = make_client(Recorder([completion("纯文本")]))
    assert await client.complete(Role.EXECUTOR, [{"role": "user", "content": "x"}]) == "纯文本"


async def test_request_body_carries_model_base_url_and_auth():
    """base_url 与模型名必须真的发出去 —— 否则"切 DeepSeek / Qwen"就只是幻觉。"""
    rec = Recorder([completion()])

    await make_client(rec).chat([{"role": "user", "content": "问题"}], model="deepseek-chat", temperature=0.1)

    body = rec.bodies[0]
    assert body["model"] == "deepseek-chat"
    assert body["temperature"] == 0.1
    assert body["messages"] == [{"role": "user", "content": "问题"}]
    assert rec.requests[0].url.path == ENDPOINT
    assert rec.requests[0].url.host == "mock-llm.test"
    assert rec.requests[0].headers["authorization"] == "Bearer sk-mock"


# ---------------------------------------------------------------- function calling
async def test_chat_parses_tool_calls_and_arguments():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_arxiv", "arguments": '{"query": "rag", "top_k": 5}'},
        }
    ]
    client = make_client(Recorder([completion(None, tool_calls=tool_calls, finish_reason="tool_calls")]))

    tools = [{"type": "function", "function": {"name": "search_arxiv", "parameters": {"type": "object"}}}]
    result = await client.chat([{"role": "user", "content": "找几篇 RAG 论文"}], tools=tools)

    assert result.content == ""  # 纯 function calling 轮次没有正文
    assert result.wants_tools is True
    assert result.finish_reason == "tool_calls"
    call = result.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert (call.name, call.id) == ("search_arxiv", "call_1")
    assert call.parsed_arguments() == {"query": "rag", "top_k": 5}


async def test_tools_are_sent_in_request_body():
    rec = Recorder([completion("ok")])
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]

    await make_client(rec).chat([{"role": "user", "content": "x"}], tools=tools, tool_choice="auto")

    body = rec.bodies[0]
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"


def test_tool_call_arguments_must_be_valid_json():
    """参数不是合法 JSON 时要抛业务异常，而不是悄悄返回 {} ——
    后者会让 tool_node 拿着空参数去调工具，失败的现场离病因很远。"""
    call = ToolCall(id="1", name="f", arguments="{不是 json")
    with pytest.raises(LLMError, match="合法 JSON"):
        call.parsed_arguments()


# ---------------------------------------------------------------- stream()
async def test_stream_yields_incremental_text():
    sse = (
        'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"混"},"finish_reason":null}]}\n\n'
        'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"m",'
        '"choices":[{"index":0,"delta":{"content":"合"},"finish_reason":null}]}\n\n'
        'data: {"id":"1","object":"chat.completion.chunk","created":0,"model":"m",'
        '"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )
    rec = Recorder([httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})])
    client = make_client(rec)

    pieces = [p async for p in client.stream(Role.EXECUTOR, [{"role": "user", "content": "x"}])]

    assert pieces == ["混", "合"]
    assert "".join(pieces) == "混合"
    # stream_options.include_usage 让最后一帧带上真实用量
    assert client.usage.completion_tokens == 2
    assert rec.bodies[0]["stream"] is True


# ---------------------------------------------------------------- structured()
from pydantic import BaseModel  # noqa: E402


class PlanStepSchema(BaseModel):
    action: str
    tool: str


class PlanSchema(BaseModel):
    steps: list[PlanStepSchema]
    rationale: str = ""


async def test_structured_parses_into_pydantic_model():
    payload = {"steps": [{"action": "检索", "tool": "search_arxiv"}], "rationale": "先取证据"}
    client = make_client(Recorder([completion(json.dumps(payload))]))

    plan = await client.structured([{"role": "user", "content": "规划一下"}], PlanSchema)

    assert isinstance(plan, PlanSchema)
    assert plan.steps[0].tool == "search_arxiv"
    assert plan.rationale == "先取证据"


async def test_structured_sends_json_object_response_format_and_schema_hint():
    rec = Recorder([completion(json.dumps({"steps": []}))])

    await make_client(rec).structured([{"role": "user", "content": "x"}], PlanSchema)

    body = rec.bodies[0]
    assert body["response_format"] == {"type": "json_object"}
    # system 提示里必须带 schema：JSON Mode 只保证"是 JSON"，不保证字段对
    assert "JSON Schema" in body["messages"][0]["content"]
    assert "steps" in body["messages"][0]["content"]


async def test_structured_strips_markdown_fence():
    """模型偶尔仍会包一层 ```json —— 直接 model_validate_json 会炸，必须先剥。"""
    fenced = '```json\n{"steps": [{"action": "a", "tool": "t"}]}\n```'
    client = make_client(Recorder([completion(fenced)]))

    plan = await client.structured([{"role": "user", "content": "x"}], PlanSchema)
    assert plan.steps[0].action == "a"


async def test_structured_repairs_after_validation_failure():
    """第一次返回缺字段，第二次修正 —— 应当重试并把校验错误回喂给模型。"""
    bad = json.dumps({"steps": [{"action": "a"}]})  # 缺 tool
    good = json.dumps({"steps": [{"action": "a", "tool": "t"}]})
    rec = Recorder([completion(bad), completion(good)])

    plan = await make_client(rec).structured([{"role": "user", "content": "x"}], PlanSchema, repair_attempts=1)

    assert plan.steps[0].tool == "t"
    assert rec.call_count == 2
    repair = json.dumps(rec.bodies[1], ensure_ascii=False)
    assert "JSON Schema 校验" in repair


async def test_structured_gives_up_with_llm_error():
    """repair_attempts=1 ⇒ 一共问 2 次（初次 + 1 次修正），两次都不合格才抛。"""
    bad = '{"steps": "不是数组"}'
    rec = Recorder([completion(bad), completion(bad)])
    client = make_client(rec)
    with pytest.raises(LLMError, match="结构化输出"):
        await client.structured([{"role": "user", "content": "x"}], PlanSchema, repair_attempts=1)
    assert rec.call_count == 2  # 不能无限重试


async def test_structured_json_schema_mode_is_opt_in():
    rec = Recorder([completion(json.dumps({"steps": []}))])
    await make_client(rec).structured([{"role": "user", "content": "x"}], PlanSchema, use_json_schema=True)
    assert rec.bodies[0]["response_format"]["type"] == "json_schema"


# ---------------------------------------------------------------- 错误映射
async def test_api_error_becomes_llm_error_with_status():
    client = make_client(Recorder([httpx.Response(401, json={"error": {"message": "bad key"}})]))
    with pytest.raises(LLMError, match="401"):
        await client.chat([{"role": "user", "content": "x"}])


async def test_connection_error_becomes_llm_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMError, match="无法连接模型端点"):
        await make_client(boom).chat([{"role": "user", "content": "x"}])


async def test_missing_api_key_raises_before_request():
    client = LLMClient(api_key="", base_url=BASE)
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        await client.chat([{"role": "user", "content": "x"}])


# ---------------------------------------------------------------- 纯函数
def test_strip_code_fence_variants():
    assert _strip_code_fence('{"a":1}') == '{"a":1}'
    assert _strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_code_fence('```\n{"a":1}\n```') == '{"a":1}'
    assert _strip_code_fence("  ```json\n[1,2]\n```  ") == "[1,2]"


def test_count_tokens():
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0


def test_role_resolution_uses_settings():
    """Planner 与 Executor 可以指向不同模型 —— 这是成本优化的主要旋钮。"""
    from app.llm.client import resolve_model

    assert resolve_model(Role.PLANNER) == resolve_model("planner")
    assert isinstance(resolve_model(Role.EXECUTOR), str)
