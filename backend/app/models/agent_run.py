"""Agent 运行轨迹 —— Reflexion 机制的落盘载体。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IntPKMixin


class AgentRun(IntPKMixin, CreatedAtMixin, Base):
    """一轮 Agent 图执行的轨迹。

    `steps` 按时间顺序记录每个节点的输入输出摘要，节点名与 LangGraph 图里的一致：

        [{"node": "intent", "status": "ok", "intent": "...", "confidence": 0.91,
          "elapsed_ms": 320, "detail": {...}},
         {"node": "planner", "status": "ok", "steps": [...], "round": 0},
         {"node": "tool", "status": "ok", "tool": "search_arxiv", "args": {...}},
         {"node": "reflector", "status": "ok", "scores": {...}, "verdict": "accept"}]

    `session_id` 是**会话聚合键**：同一轮对话的多条 run 共享它，
    前端左侧历史列表就是按它 group by 出来的（规格里没有 conversations 表）。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_session_created", "session_id", "created_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    intent: Mapped[str | None] = mapped_column(String(32))
    mode: Mapped[str | None] = mapped_column(String(32))  # react | plan_execute | reflect | pipeline
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running", index=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<AgentRun {self.id} session={self.session_id} {self.status} steps={len(self.steps or [])}>"


__all__ = ["AgentRun"]
