"""主题探索闭环与订阅抓取的请求/响应模型。

与 `app/agents/explore.py` 的分工：**这里只放形状，不放默认值**。
三个阈值（篇数上限 / 相关性阈值 / 轮次上限）的缺省值来自配置
（`EXPLORE_MAX_PAPERS` 等），所以请求体里一律是可选的 `None` ——
把 `settings` 引进 schemas 会让这一层不再是纯类型。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExploreRequest(BaseModel):
    """一个探索主题。"""

    topic: str = Field(
        min_length=2,
        max_length=300,
        description="研究主题，如「MoE 专家路由的负载均衡」",
    )
    max_papers: int | None = Field(default=None, ge=1, le=20, description="最多下载几篇；缺省取 EXPLORE_MAX_PAPERS")
    min_score: float | None = Field(
        default=None, ge=0.0, le=1.0, description="相关性阈值（bge-m3 余弦）；缺省取 EXPLORE_MIN_SCORE"
    )
    max_rounds: int | None = Field(default=None, ge=1, le=5, description="检索轮次上限；缺省取 EXPLORE_MAX_ROUNDS")


class RecommendationOut(BaseModel):
    """推荐列表里的一条。

    `paper_id` 与 `downloaded` 分开：能推荐不等于能下载 —— Semantic Scholar
    只给标题与摘要，我们没有"按任意 URL 下 PDF"的工具，所以 S2 命中的条目
    通常是"仅推荐"（`downloaded=False`，`note` 里说明）。
    """

    arxiv_id: str | None = None
    paper_id: int | None = Field(default=None, description="已入库时的 int64 主键；仅推荐未入库为 None")
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str = ""
    url: str = ""
    source: str = Field(default="", description="arxiv | semantic_scholar")
    score: float = Field(default=0.0, description="与主题的余弦相似度，0~1")
    citation_count: int = 0
    abstract: str = ""
    downloaded: bool = False
    note: str = ""


class ExploreDoneOut(BaseModel):
    """`done` 帧的载荷 —— 探索结束后一次性交付的东西。

    挂在一个帧里而不是拆成多个事件：收尾帧是唯一保证会发出去的帧
    （见 `app/llm/streaming.py` 的 `done_event`），中途断流时前端拿部分
    结果没有意义，不如让"结果"和"结束"是一件事。
    """

    topic: str
    rounds: int = 1
    searched: int = Field(default=0, description="去重后的候选总数")
    scored: int = 0
    downloaded: int = 0
    new: int = Field(default=0, description="其中本次新入库的篇数（订阅通知看的就是它）")
    passed: int = Field(default=0, description="达到 min_score 的条数")
    min_score: float = 0.7
    quality: float = Field(default=0.0, description="Reflector 的综合评分")
    verdict: str = ""
    critique: str = ""
    recommendations: list[RecommendationOut] = Field(default_factory=list)
    elapsed_ms: int = 0


class SubscribeRequest(BaseModel):
    """新建一个定时抓取订阅。默认每天 8:00 跑（`SUBSCRIBE_HOUR`）。"""

    topic: str = Field(min_length=2, max_length=300)
    max_papers: int = Field(default=8, ge=1, le=20)
    min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    enabled: bool = True


class SubscriptionOut(BaseModel):
    """订阅的对外视图。`last_*` 五个字段就是"通知用户"的出口 ——
    桌面端单用户场景下，一个带角标的列表比推一条系统通知更不容易被忽略。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    topic: str
    max_papers: int = 8
    min_score: float = 0.7
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str = "pending"
    last_error: str | None = None
    last_new: int = 0
    total_new: int = 0
    created_at: datetime | None = None


class SchedulerOut(BaseModel):
    """定时抓取的调度状态。

    这个端点的用途只有一个：让"定时任务到底会不会触发"这件事**可被直接观察**，
    而不必等到明天 8:00 才知道。也顺手把配置里的时刻暴露给界面显示。
    """

    enabled: bool = True
    hour: int = 8
    minute: int = 0
    next_run_at: datetime | None = None
    engine: str = "in-process asyncio（无 Celery）"


__all__ = [
    "ExploreDoneOut",
    "ExploreRequest",
    "RecommendationOut",
    "SchedulerOut",
    "SubscribeRequest",
    "SubscriptionOut",
]
