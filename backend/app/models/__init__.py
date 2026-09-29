"""ORM 模型。导入本模块即完成全部表的注册（Alembic autogenerate 依赖这一点）。"""

from app.models.base import Base, TimestampMixin, UUIDMixin, new_uuid
from app.models.chunk import PaperChunk
from app.models.conversation import Conversation, Message
from app.models.paper import Paper, PaperSection
from app.models.paper_citation import PaperCitation
from app.models.task import Task

__all__ = [
    "Base",
    "UUIDMixin",
    "TimestampMixin",
    "new_uuid",
    "Paper",
    "PaperSection",
    "PaperChunk",
    "PaperCitation",
    "Conversation",
    "Message",
    "Task",
]
