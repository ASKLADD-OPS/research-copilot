"""主题探索闭环 + 订阅抓取的测试。

**不碰真库、不拉模型、不发外网。** 闭环的五个外部依赖（两路检索、嵌入打分、
PDF 下载、规划/评审两个 LLM 节点）全部被 `closed_loop` 夹具换掉，于是这里
断言的是**闭环自己的控制流**——事件协议、阈值过滤、下载配额、轮次与重规划——
而不是"arXiv 今天通不通"。这是唯一能让这几条验收标准稳定可测的办法：
真跑一遍要下 5 篇 PDF + 算 20 条向量，单测里既慢又不可复现。

验收标准对应的用例：
| 标准 | 用例 |
|---|---|
| 1. 自动下载 ≥5 篇 | `test_explore_downloads_at_least_five` |
| 2. 执行轨迹完整展示 | `test_explore_emits_three_part_trace` / `test_explore_sse_stream_carries_full_trace` |
| 3. 定时任务可触发 | `test_seconds_until_*` / `test_scheduler_status_*` |
| 4. 推荐相关性 ≥0.7 | `test_recommendations_respect_threshold` |
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.agents import explore as X
from app.workers import scheduler as S

# ==================================================================== 造数据


def _arxiv_payload(n: int) -> dict:
    """假的 arXiv 返回。标题刻意与主题同域，好让阈值断言有意义。"""
    return {
        "results": [
            {
                "arxiv_id": f"2401.{i:05d}",
                "title": f"Load Balancing for Mixture-of-Experts Routing, Part {i}",
                "abstract": "We study expert routing balance in sparse MoE layers.",
                "authors": ["A. Author", "B. Author"],
                "published": "2024-01-15",
                "url": f"https://arxiv.org/abs/2401.{i:05d}",
            }
            for i in range(n)
        ]
    }


def _cand(title: str, score: float, *, downloaded: bool = False, arxiv_id: str = "2401.00001") -> dict:
    """一个归一化后的候选（字段与 `Candidate` TypedDict 对齐）。"""
    return {
        "key": arxiv_id or title,
        "source": "arxiv",
        "arxiv_id": arxiv_id,
        "paper_id": "",
        "paper_id_int": 7 if downloaded else None,
        "title": title,
        "authors": [],
        "abstract": "",
        "year": 2024,
        "venue": "",
        "url": "",
        "citation_count": 0,
        "score": score,
        "downloaded": downloaded,
        "is_new": downloaded,
        "note": "",
    }


# ==================================================================== 夹具
@pytest.fixture
def closed_loop(monkeypatch):
    """把闭环的五个外部依赖换成确定性假实现，返回被 patch 的模块。

    注意 patch 的是**模块命名空间里的名字**（`explore.plan_queries` 等），
    因为 `explore()` 调用的就是它们 —— 这也正是它们被放在模块级的原因之一。
    """
    from app.agents.explore import ExploreReflection, SearchPlan

    async def fake_plan(topic: str, *, feedback: str = "", round_no: int = 1) -> SearchPlan:
        return SearchPlan(arxiv_query=f'ti:"{topic}"', s2_query=topic, reasoning="假规划")

    async def fake_call_tool(name: str, args: dict) -> dict:
        # S2 一路故意返回空：真实环境里没有 API key 时就是这样，闭环必须能靠单路跑完。
        return _arxiv_payload(6) if name.startswith("arxiv") else {"results": []}

    async def fake_score(topic: str, candidates: list) -> None:
        for c in candidates:
            c["score"] = 0.88

    async def fake_reflect(topic, candidates, *, downloaded, min_score, used_queries):
        return ExploreReflection(coverage=0.9, relevance=0.9, verdict="accept", critique="够用")

    async def fake_download(cand):
        return True, 42, "已下载并排入后台解析", True

    monkeypatch.setattr(X, "plan_queries", fake_plan)
    monkeypatch.setattr(X, "call_tool", fake_call_tool)
    monkeypatch.setattr(X, "score_candidates", fake_score)
    monkeypatch.setattr(X, "reflect", fake_reflect)
    monkeypatch.setattr(X, "_download", fake_download)
    return X


async def _drain(gen) -> list[tuple[str, dict]]:
    return [evt async for evt in gen]


def _parse_sse(text: str) -> list[tuple[str, str]]:
    """把整段 SSE 响应拆成 `(事件名, data 原文)`。与前端 parseFrame 同逻辑。"""
    out: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        name = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
        if name:
            out.append((name, "\n".join(data_lines)))
    return out


# ==================================================================== 闭环控制流
@pytest.mark.unit
async def test_explore_emits_three_part_trace(closed_loop):
    """轨迹是 Thought → Action → Observation 三段式，且**每个 action 后面紧跟它的 observation**。

    这个相邻性断言不是吹毛求疵：前端就是靠它把两条事件渲染成一组
    （"调了 arxiv_search" + "拿回了 6 条"）。中间夹别的帧会让渲染错位。
    """
    events = await _drain(X.explore("MoE 专家路由负载均衡", max_papers=6, min_score=0.7, max_rounds=1))
    names = [e for e, _ in events]

    assert names[0] == "progress"  # 第一个 progress 让前端立刻有进度条可画
    assert names[-1] == "done"  # 硬约定：流必须以 done 收尾
    assert "thought" in names
    assert "action" in names
    assert "observation" in names
    assert "recommend" in names

    for i, name in enumerate(names[:-1]):
        if name == "action":
            assert names[i + 1] == "observation", f"第 {i} 个 action 后面不是 observation：{names[i + 1]}"


@pytest.mark.unit
async def test_explore_progress_denominator_is_fixed(closed_loop):
    """进度分母固定为 6（STAGES）而不是"实际步数"——重规划会让实际步数变化，
    分母跟着变的话进度条会往回跳，那比"不准"更糟。"""
    events = await _drain(X.explore("MoE routing", max_papers=6, min_score=0.7, max_rounds=1))
    progresses = [d for e, d in events if e == "progress"]
    assert progresses, "没有任何 progress 帧"
    assert all(p["total"] == len(X.STAGES) == 6 for p in progresses)
    # index 必须在 1..6 内且单调不减
    idx = [p["index"] for p in progresses]
    assert all(1 <= i <= 6 for i in idx)
    assert idx == sorted(idx)


@pytest.mark.unit
async def test_explore_downloads_at_least_five(closed_loop):
    """验收标准 1：给定主题，自动下载 ≥5 篇。"""
    events = await _drain(X.explore("MoE 专家路由负载均衡", max_papers=6, min_score=0.7, max_rounds=1))
    done = next(d for e, d in events if e == "done")
    assert done["downloaded"] >= 5
    assert done["new"] >= 5  # 新入库数：订阅角标看的就是它
    assert done["passed"] >= 5


@pytest.mark.unit
async def test_explore_replans_when_below_floor(monkeypatch):
    """下载数低于硬下限（EXPLORE_MIN_PAPERS=5）时必须再搜一轮，
    哪怕 Reflector 说 accept —— 否则交不满 5 篇的结果。"""
    from app.agents.explore import ExploreReflection, SearchPlan

    calls = {"rounds": 0}

    async def fake_plan(topic, *, feedback="", round_no=1):
        calls["rounds"] = round_no
        return SearchPlan(arxiv_query="q", s2_query="q")

    async def fake_call_tool(name, args):
        return _arxiv_payload(1)  # 每轮只有 1 篇

    async def fake_score(topic, candidates):
        for c in candidates:
            c["score"] = 0.9

    async def fake_reflect(topic, candidates, *, downloaded, min_score, used_queries):
        # 故意说 accept：硬规则必须压过模型判定
        return ExploreReflection(coverage=0.5, relevance=0.5, verdict="accept", critique="")

    async def fake_download(cand):
        return True, 1, "ok", True

    monkeypatch.setattr(X, "plan_queries", fake_plan)
    monkeypatch.setattr(X, "call_tool", fake_call_tool)
    monkeypatch.setattr(X, "score_candidates", fake_score)
    monkeypatch.setattr(X, "reflect", fake_reflect)
    monkeypatch.setattr(X, "_download", fake_download)

    events = await _drain(X.explore("t", max_papers=8, min_score=0.7, max_rounds=3))
    assert calls["rounds"] == 3, "低于硬下限却没继续重规划"
    assert any(e == "thought" and d.get("stage") == "replan" for e, d in events)


# ==================================================================== 推荐阈值
@pytest.mark.unit
def test_recommendations_respect_threshold():
    """验收标准 4：推荐结果相关性 ≥0.7（阈值可配，这里用默认值）。"""
    cands = [_cand("达标 A", 0.93, downloaded=True), _cand("达标 B", 0.75), _cand("不达标 C", 0.42)]
    out = X.build_recommendations(cands, min_score=0.7, limit=10)
    assert [o["title"] for o in out] == ["达标 A", "达标 B"]
    assert all(o["score"] >= 0.7 for o in out)


@pytest.mark.unit
def test_recommendations_put_downloaded_first():
    """已下载的排前面 —— 它们才是能点的。分数更高的"仅推荐"排在后面是故意的。"""
    cands = [_cand("未下载高分", 0.99), _cand("已下载低分", 0.71, downloaded=True)]
    out = X.build_recommendations(cands, min_score=0.7, limit=10)
    assert [o["title"] for o in out] == ["已下载低分", "未下载高分"]


@pytest.mark.unit
def test_recommendations_carry_paper_id_only_when_ingested():
    """`paper_id` 与 `downloaded` 分开：能推荐 ≠ 能下载（S2 只有摘要）。"""
    ingested = _cand("已入库", 0.9, downloaded=True)
    only_recommended = {**_cand("仅推荐", 0.8), "arxiv_id": "", "paper_id_int": None}
    out = X.build_recommendations([ingested, only_recommended], min_score=0.7, limit=10)
    by_title = {o["title"]: o for o in out}
    assert by_title["已入库"]["paper_id"] == 7
    assert by_title["仅推荐"]["paper_id"] is None
    assert by_title["仅推荐"]["downloaded"] is False


# ==================================================================== 归一化
@pytest.mark.unit
def test_candidates_of_normalizes_both_sources():
    """arXiv 与 S2 的字段名不同，归一化只做一次、只在这里做。"""
    arxiv = X.candidates_of(
        "arxiv_search", {"results": [{"arxiv_id": "2401.00001", "title": "T", "published": "2023-05-01"}]}
    )
    assert arxiv[0]["source"] == "arxiv"
    assert arxiv[0]["key"] == "2401.00001"  # 有 arxiv_id 就用它当去重键
    assert arxiv[0]["year"] == 2023

    s2 = X.candidates_of("semantic_scholar_search", {"results": [{"paper_id": "abc", "title": "T2", "year": 2022}]})
    assert s2[0]["source"] == "semantic_scholar"
    assert s2[0]["key"] == "t2"  # 没 arxiv_id，退化为规范化标题
    assert s2[0]["year"] == 2022


@pytest.mark.unit
def test_candidates_of_skips_titleless_and_junk():
    """没标题的条目既没法去重也没法给人看，直接丢。"""
    assert X.candidates_of("arxiv_search", {"results": [{"arxiv_id": "x"}, "junk", None]}) == []
    assert X.candidates_of("arxiv_search", "工具 arxiv_search 不可用") == []


# ==================================================================== 定时调度
@pytest.mark.unit
def test_seconds_until_target_later_today():
    now = datetime(2026, 10, 2, 7, 0, 0)
    assert S.seconds_until(8, 0, now=now) == 3600


@pytest.mark.unit
def test_seconds_until_rolls_to_tomorrow_when_already_passed():
    now = datetime(2026, 10, 2, 9, 0, 0)
    assert S.seconds_until(8, 0, now=now) == pytest.approx(23 * 3600)


@pytest.mark.unit
def test_seconds_until_exact_moment_is_zero():
    """恰好卡在 8:00:00 时立刻跑，而不是傻等一整天。"""
    now = datetime(2026, 10, 2, 8, 0, 0)
    assert S.seconds_until(8, 0, now=now) == 0


@pytest.mark.unit
def test_humanize_is_for_logs_only():
    assert S.humanize(43200) == "12h0m"
    assert S.humanize(5400) == "1h30m"


# ==================================================================== HTTP 层
@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.unit
def test_explore_routes_are_registered():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert {
        "/api/v1/tools/explore",
        "/api/v1/tools/subscribe",
        "/api/v1/tools/subscriptions",
        "/api/v1/tools/subscriptions/{sub_id}",
        "/api/v1/tools/subscriptions/{sub_id}/run",
        "/api/v1/tools/scheduler",
    } <= paths


@pytest.mark.unit
def test_explore_rejects_too_short_topic(client):
    res = client.post("/api/v1/tools/explore", json={"topic": "x"})
    assert res.status_code in {400, 422}


@pytest.mark.unit
def test_explore_rejects_out_of_range_max_papers(client):
    res = client.post("/api/v1/tools/explore", json={"topic": "valid topic", "max_papers": 999})
    assert res.status_code in {400, 422}


@pytest.mark.unit
def test_explore_sse_stream_carries_full_trace(client, closed_loop):
    """验收标准 2：执行轨迹完整展示 —— 走 HTTP 层再验一遍 SSE 帧与事件名。

    上面那条生成器用例验的是"闭环会产出什么"，这条验的是"它有没有原样穿过 SSE 层"
    （`_explore_frames` 的转发与 `sse()` 的编码）。两者都可能单独坏。
    """
    res = client.post("/api/v1/tools/explore", json={"topic": "MoE 专家路由", "max_papers": 6, "max_rounds": 1})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(res.text)
    names = [n for n, _ in events]
    assert names[-1] == "done"
    assert {"thought", "action", "observation", "progress", "recommend"} <= set(names)

    done = json.loads(next(d for n, d in events if n == "done"))
    assert done["downloaded"] >= 5
    assert done["topic"] == "MoE 专家路由"
    assert all(r["score"] >= 0.7 for r in done["recommendations"])


@pytest.mark.unit
def test_subscribe_rejects_empty_topic(client):
    res = client.post("/api/v1/tools/subscribe", json={"topic": ""})
    assert res.status_code in {400, 422}


@pytest.mark.unit
def test_scheduler_status_exposes_next_run(client):
    """验收标准 3：定时任务可触发 —— 用 `next_run_at` 当场核对调度是否就绪。

    conftest 关掉了 SUBSCRIBE_ENABLED（不然每个用例都会留下一个睡到明早 8:00 的任务），
    所以这里断言的是"关掉时如实报告为关闭"，而不是假报一个 next_run_at。
    """
    res = client.get("/api/v1/tools/scheduler")
    assert res.status_code == 200
    body = res.json()
    data = body["data"]
    assert data["hour"] == 8
    assert data["minute"] == 0
    assert data["engine"]
    if data["enabled"]:
        assert data["next_run_at"]
    else:
        assert data["next_run_at"] is None
        assert "SUBSCRIBE_ENABLED" in body["message"]


@pytest.mark.unit
def test_subscriptions_list_fails_cleanly_without_database(client):
    """数据库不可达时返回结构化错误，不能 500 泄漏堆栈（这些端点没有离线兜底数据）。"""
    res = client.get("/api/v1/tools/subscriptions")
    assert res.status_code != 200 or res.json()["code"] == 0
    if res.status_code != 200:
        assert "message" in res.json()
