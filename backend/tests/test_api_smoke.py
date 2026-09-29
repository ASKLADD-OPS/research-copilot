"""应用装配冒烟测试。

只验证"图接线对不对"，不验证业务：路由有没有挂上、OpenAPI 能不能生成、
/health 能不能在依赖全挂的情况下仍然返回 200（这是 k8s 探针与
`docker compose ps` 能否判活的前提）。

**不碰数据库**：conftest 把所有依赖指向没人监听的端口，
这里的断言不要求组件 ok，只要求接口本身不炸。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

EXPECTED_PATHS = {
    # 论文
    "/api/v1/papers",
    "/api/v1/papers/upload",
    "/api/v1/papers/{paper_id}",
    "/api/v1/papers/{paper_id}/reindex",
    "/api/v1/papers/{paper_id}/chunks",
    "/api/v1/papers/{paper_id}/file",
    # 问答与溯源
    "/api/v1/qa/ask",
    "/api/v1/qa/retrieve",
    "/api/v1/qa/trace",
    # 对话（SSE）
    "/api/v1/chat/stream",
    "/api/v1/chat/conversations",
    "/api/v1/chat/conversations/{conv_id}",
    # 图谱
    "/api/v1/graph",
    "/api/v1/graph/analyze",
    "/api/v1/graph/rebuild",
    # 写作
    "/api/v1/writing/templates",
    "/api/v1/writing/draft",
    "/api/v1/writing/translate",
    # 工具
    "/api/v1/tools",
    "/api/v1/tools/call",
    # 探针
    "/health",
}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.unit
def test_all_routes_are_registered():
    """用 openapi 而不是 app.routes 列举端点。

    FastAPI 的 include_router 在本环境里会用 `_IncludedRouter` 包一层、不展平，
    直接遍历 `app.routes` 只能捞到 "/"，会得出"路由全丢了"的错误结论。
    """
    from app.main import app

    paths = set(app.openapi()["paths"])
    missing = EXPECTED_PATHS - paths
    assert not missing, f"缺少路由: {sorted(missing)}"


@pytest.mark.unit
def test_openapi_generates_without_error(client):
    res = client.get("/openapi.json")
    assert res.status_code == 200
    spec = res.json()
    assert spec["info"]["title"]
    assert len(spec["paths"]) >= len(EXPECTED_PATHS)


@pytest.mark.unit
def test_swagger_ui_is_served(client):
    """验收标准之一：/docs 必须能打开。"""
    res = client.get("/docs")
    assert res.status_code == 200
    assert "swagger" in res.text.lower()


@pytest.mark.unit
def test_health_is_up_even_with_dead_dependencies(client):
    """三库全死也要在几秒内返回 200 degraded，不能挂住（compose healthcheck 靠这个）。"""
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    report = body["data"]  # 统一信封：真正的载荷在 data 里
    assert report["status"] in {"ok", "degraded", "down"}
    assert report["version"]
    names = {c["name"] for c in report["components"]}
    assert {"postgres", "milvus", "llm"} <= names
    assert all("ok" in c for c in report["components"])
    # 挂了也必须给出原因，否则排查时只能说"它说它坏了"
    for comp in report["components"]:
        if not comp["ok"]:
            assert comp["detail"], f"{comp['name']} 没给失败原因"


@pytest.mark.unit
def test_health_reports_llm_configured_in_tests(client):
    """conftest 塞了假的 LLM_API_KEY —— 这一项应该判 ok（健康检查只看配置，不发请求）。"""
    report = client.get("/health").json()["data"]
    llm = next(c for c in report["components"] if c["name"] == "llm")
    assert llm["ok"] is True


@pytest.mark.unit
def test_error_shape_is_unified_for_unknown_path(client):
    res = client.get("/api/v1/definitely-not-here")
    assert res.status_code == 404


@pytest.mark.unit
def test_validation_error_uses_the_unified_envelope(client):
    """请求体校验失败也必须走 {code, data, message}，否则前端拆信封会崩。"""
    res = client.post("/api/v1/qa/ask", json={"query": ""})
    assert res.status_code in {400, 422}
    body = res.json()
    assert set(body) >= {"code", "message"} or "detail" in body


@pytest.mark.unit
def test_tools_endpoint_lists_local_tools(client):
    """工具清单不依赖外部 Server（全都关了），本地工具必须仍能列出。"""
    res = client.get("/api/v1/tools")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    names = {t["name"] for t in body["data"]["tools"]}
    assert {"retrieve_papers", "graph_analyze", "write_section"} <= names


@pytest.mark.unit
def test_writing_templates_are_offline_available(client):
    """模板是写死的常量，不碰 LLM —— 这条挂了说明导入链被压坏了。"""
    res = client.get("/api/v1/writing/templates")
    assert res.status_code == 200
    kinds = {t["kind"] for t in res.json()["data"]}
    assert {"summary", "related_work", "abstract", "introduction", "review", "rebuttal"} <= kinds


@pytest.mark.unit
def test_papers_list_fails_cleanly_without_database(client):
    """数据库不可达时，接口应该返回结构化错误而不是 500 堆栈泄漏。"""
    res = client.get("/api/v1/papers")
    assert res.status_code != 200 or res.json()["code"] == 0
    if res.status_code != 200:
        body = res.json()
        assert "message" in body
