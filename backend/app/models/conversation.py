"""会话与消息。消息上挂着引用、溯源评分与反思日志 —— 这三样是可追踪性的载体。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin


class Conversation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    title: Mapped[str] = mapped_column(String(512), default="新对话")
    # 会话级默认意图（消息级意图可覆盖）
    default_intent: Mapped[str | None] = mapped_column(String(32))
    # 会话绑定的论文范围；空表示全库
    paper_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )

    def __repr__(self) -> str:
        return f"<Conversation {self.id[:8]} {self.title[:30]!r}>"


class Message(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system | tool

    content: Mapped[str] = mapped_column(Text, default="")

    # ---- 意图识别结果 ----
    intent: Mapped[str | None] = mapped_column(String(32))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    slot_filling: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    target_papers: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # ---- 引用与溯源 ----
    # [{chunk_id, paper_id, page, section, quote, nli_score, supported}]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    grounding_ratio: Mapped[float | None] = mapped_column(Float)
    unsupported_claims: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # ---- Reflection 记录（Reflexion 机制：历史行为 / 假设 / 反思）----
    # [{round, faithfulness, relevance, coherence, completeness, verdict, notes}]
    reflections: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    # ---- 执行轨迹 ----
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_conv_created", "conversation_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<Message {self.id[:8]} {self.role} intent={self.intent}>"
