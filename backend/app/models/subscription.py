"""订阅抓取 —— 一个主题 + 一组阈值 + 上一次跑成什么样。

为什么单独一张表而不是复用 `agent_runs`：那张表记的是"某一轮图执行的过程"，
按 `session_id` 聚合给前端当会话列表用；订阅要的是**可枚举的长期配置**
（后台要遍历它才知道该抓哪些主题），以及"上次抓到几篇"这种跨轮状态。
两者的寿命完全不同 —— run 用完即弃，订阅一直在。

为什么不在 `SPEC_TABLES` 里：那 8 张是阶段 1 数据持久层的验收清单
（`app/models/__init__.py` 写死，验收脚本直接断言）。这张是阶段 9 新增的功能表，
加进去会让阶段 1 的验收口径漂移，所以单列。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IntPKMixin, TimestampMixin


class Subscription(IntPKMixin, TimestampMixin, Base):
    """定时抓取订阅。

    状态机只有三个值，且都写在 `last_status` 上（没有单独的任务表 —— 与
    `papers.parsed_status` 同一套思路，见 `app/indexing/runner.py` 的头注释）：

        pending → running → ok | failed
    """

    __tablename__ = "subscriptions"
    # 调度器唯一的查询就是"enabled 的订阅有哪些"，顺带按主题排一下让日志稳定
    __table_args__ = (Index("ix_subscriptions_enabled_topic", "enabled", "topic"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    topic: Mapped[str] = mapped_column(Text, nullable=False)
    # 阈值与上限**按订阅存**而不是全局一份：一个主题要 5 篇、另一个只要 2 篇是常态
    max_papers: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    min_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ---- 上一次执行的结果 ----
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 上次抓到几篇新的
    total_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 累计入库篇数

    def __repr__(self) -> str:
        return f"<Subscription {self.id} {self.topic[:32]!r} status={self.last_status} new={self.last_new}>"


__all__ = ["Subscription"]
