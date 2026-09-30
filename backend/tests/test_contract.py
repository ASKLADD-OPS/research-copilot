"""响应契约与错误码测试。

`{code, data, message}` 是全项目唯一的对外形状，前端 `useApi` 的"拆信封"
逻辑直接依赖它。这里保证：
- 成功恒为 code=0；
- 错误码不重复、分带清晰；
- 分页元信息自洽。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pydantic
import pytest

from app.core import errors as errors_module
from app.core.errors import (
    AppError,
    BadRequestError,
    EmptyRetrievalError,
    GroundingError,
    NotFoundError,
    ToolError,
)
from app.schemas import ApiResponse, Page, PageMeta
from app.schemas.common import CODE_OK
from app.schemas.paper import PaperOut, PaperUpdate
from app.schemas.qa import AskRequest, AskResult, RetrievalDebug, RetrievedChunkOut


# ---------------------------------------------------------------- 统一响应体
@pytest.mark.unit
def test_ok_defaults():
    res = ApiResponse.ok({"x": 1})
    assert res.code == CODE_OK == 0
    assert res.data == {"x": 1}
    assert res.message == "ok"


@pytest.mark.unit
def test_ok_without_data():
    res = ApiResponse.ok()
    assert res.code == 0
    assert res.data is None


@pytest.mark.unit
def test_fail_keeps_code_and_message():
    res = ApiResponse.fail(3002, "检索不到相关资料")
    assert res.code == 3002
    assert res.message == "检索不到相关资料"
    assert res.data is None


@pytest.mark.unit
def test_response_serializes_to_exactly_three_keys():
    """多出字段会破坏前端的形状假设（前端只读这三个键）。"""
    assert set(ApiResponse.ok(1).model_dump()) == {"code", "data", "message"}


@pytest.mark.unit
def test_response_is_generic_over_payload():
    res: ApiResponse[list[int]] = ApiResponse.ok([1, 2, 3])
    assert res.data == [1, 2, 3]


# ---------------------------------------------------------------- 分页
@pytest.mark.unit
def test_page_meta_defaults_and_bounds():
    meta = PageMeta()
    assert (meta.page, meta.page_size, meta.total, meta.has_next) == (1, 20, 0, False)

    with pytest.raises(pydantic.ValidationError):
        PageMeta(page=0)
    with pytest.raises(pydantic.ValidationError):
        PageMeta(page_size=0)
    with pytest.raises(pydantic.ValidationError):
        PageMeta(page_size=201)


@pytest.mark.unit
def test_page_defaults_to_empty_items():
    page = Page[int]()
    assert page.items == []
    assert page.meta.total == 0


# ---------------------------------------------------------------- 论文 schema
@pytest.mark.unit
def test_paper_out_validates_from_attributes():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=1,
        user_id=1,
        title="A Paper",
        authors=[],
        abstract=None,
        doi=None,
        arxiv_id="2401.00001",
        version="v2",
        source_url=None,
        file_path="/tmp/a.pdf",
        file_hash="a" * 64,
        semantic_hash="b" * 64,
        parsed_status="ready",
        parser="mineru",
        page_count=9,
        error=None,
        created_at=now,
    )
    out = PaperOut.model_validate(row)
    assert out.parsed_status == "ready"
    assert out.id == 1  # 主键是 int64（对齐 Milvus 的 INT64），不再是 UUID 字符串
    assert out.arxiv_id == "2401.00001"
    assert out.version == "v2"
    assert out.file_path == "/tmp/a.pdf"


@pytest.mark.unit
def test_paper_status_is_constrained():
    from app.schemas.paper import PaperStatus

    statuses = set(PaperStatus.__args__)
    assert statuses == {"pending", "parsing", "chunking", "embedding", "ready", "failed"}


@pytest.mark.unit
def test_paper_update_only_touches_given_fields():
    """PATCH 语义：没传的字段不参与更新。"""
    patch = PaperUpdate(title="新标题")
    assert patch.model_dump(exclude_unset=True) == {"title": "新标题"}
    assert patch.model_dump(exclude_unset=True, exclude_none=True) == {"title": "新标题"}


# ---------------------------------------------------------------- 问答 schema
@pytest.mark.unit
def test_ask_request_bounds():
    assert AskRequest(query="x").paper_ids == []
    with pytest.raises(pydantic.ValidationError):
        AskRequest(query="")
    with pytest.raises(pydantic.ValidationError):
        AskRequest(query="x" * 4001)
    with pytest.raises(pydantic.ValidationError):
        AskRequest(query="x", top_k=0)
    with pytest.raises(pydantic.ValidationError):
        AskRequest(query="x", top_k=51)


@pytest.mark.unit
def test_ask_result_defaults_are_conservative():
    """默认必须是"未溯源、未通过"，否则漏填字段会伪装成可信答案。"""
    res = AskResult(answer="随便一句")
    assert res.grounding_ratio == 0.0
    assert res.passed_grounding is False
    assert res.citations == []


@pytest.mark.unit
def test_retrieval_debug_level_is_literal():
    assert RetrievalDebug(crag_level="relevant").crag_level == "relevant"
    with pytest.raises(pydantic.ValidationError):
        RetrievalDebug(crag_level="totally-fine")


@pytest.mark.unit
def test_retrieved_chunk_preview_default_is_empty():
    chunk = RetrievedChunkOut(chunk_id=1, paper_id=2)
    assert chunk.preview == ""
    assert chunk.sources == []
    assert chunk.page is None


# ---------------------------------------------------------------- 错误码
@pytest.mark.unit
def test_error_codes_are_unique():
    """错误码重复会让前端没法按码分流处理。"""
    classes = [
        obj
        for _, obj in inspect.getmembers(errors_module, inspect.isclass)
        if issubclass(obj, AppError) and obj is not AppError
    ]
    codes = [c.code for c in classes]
    assert len(codes) == len(set(codes)), f"重复的错误码: {codes}"


@pytest.mark.unit
def test_error_codes_follow_the_band_convention():
    """1xxx 通用 / 2xxx 解析 / 3xxx 检索 / 4xxx Agent / 5xxx 工具 / 9xxx 系统。"""
    assert NotFoundError.code == 1001
    assert BadRequestError.code == 1002
    assert EmptyRetrievalError.code == 3002
    assert GroundingError.code == 3003
    assert ToolError.code == 5001

    for cls in (NotFoundError, EmptyRetrievalError, GroundingError, ToolError):
        assert 1000 <= cls.code < 10000


@pytest.mark.unit
def test_error_subclasses_inherit_app_error():
    for cls in (NotFoundError, BadRequestError, EmptyRetrievalError, GroundingError, ToolError):
        assert issubclass(cls, AppError)


@pytest.mark.unit
def test_app_error_message_override():
    err = AppError("自定义消息")
    assert err.message == "自定义消息"
    assert str(err) == "自定义消息"


@pytest.mark.unit
def test_app_error_code_override_and_data():
    err = EmptyRetrievalError("没查到", data={"query": "x"}, code=3999)
    assert err.code == 3999
    assert err.data == {"query": "x"}
    assert err.message == "没查到"


@pytest.mark.unit
def test_app_error_default_message_comes_from_class():
    assert NotFoundError().message == "资源不存在"
    assert GroundingError().message == "生成内容未能溯源到引用上下文"


@pytest.mark.unit
def test_http_status_mapping():
    from fastapi import status

    assert NotFoundError.http_status == status.HTTP_404_NOT_FOUND
    assert BadRequestError.http_status == status.HTTP_400_BAD_REQUEST
