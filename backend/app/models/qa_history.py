"""问答历史 —— 带完整溯源信息的可追溯记录。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IntPKMixin


class QAHistory(IntPKMixin, CreatedAtMixin, Base):
    """一次问答的完整留痕。

    `sources` 里每一项的结构（前端溯源面板与 NLI 校验都按它解析）：

        {
          "answer_span": [start, end],   # 答案正文中的字符区间，用于高亮
          "chunk_id": 123,               # → chunks.id
          "paper_id": 45,                # → papers.id
          "page": 7,                     # 1-based 页码
          "bbox": [x0, y0, x1, y1],      # 归一化定位框
          "confidence": 0.93,            # NLI 蕴含分数
          "method": "nli"                # nli | lexical | self_citation
        }

    存下来而不只是回传，是为了让"上个月那次回答凭什么这么说"能被复查 ——
    抗幻觉系统如果自己不可追溯，就没法自证。
    """

    __tablename__ = "qa_history"
    __table_args__ = (Index("ix_qa_history_user_created", "user_id", "created_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)  # 本轮检索范围
    intent: Mapped[str | None] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    grounding_ratio: Mapped[float | None] = mapped_column(Float)
    faithfulness: Mapped[float | None] = mapped_column(Float)  # Reviewer 的 faithfulness 维度

    def __repr__(self) -> str:
        return f"<QAHistory {self.id} intent={self.intent} grounding={self.grounding_ratio}>"


__all__ = ["QAHistory"]
