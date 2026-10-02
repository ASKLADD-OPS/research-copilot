"""ORM 模型 —— 阶段 1 规格的 8 张表。

导入本模块即完成全部表的注册（Alembic autogenerate 与 `create_all` 都依赖这一点）。

    users           账号
    papers          论文（内容去重 + 版本识别）
    paper_versions  arXiv 版本谱系
    chunks          检索与溯源的最小单元
    citations       引文边
    qa_history      问答留痕（含 sources 溯源数组）
    graph_snapshots 引文图快照
    agent_runs      Agent 轨迹 + 会话聚合键

阶段 9 新增（**不在** SPEC_TABLES 里，见下）：

    subscriptions   定时抓取订阅
"""

from app.models.agent_run import AgentRun
from app.models.base import Base, CreatedAtMixin, IntPKMixin, TimestampMixin
from app.models.chunk import CHUNK_TYPES, Chunk
from app.models.citation import Citation
from app.models.graph_snapshot import GraphSnapshot
from app.models.paper import Paper, PaperVersion
from app.models.qa_history import QAHistory
from app.models.subscription import Subscription
from app.models.user import User

#: 规格要求的 8 张表，刻意写死一份 —— 验收脚本与单测直接断言它，
#: 免得"表建全了吗"这种问题只能靠人肉数。
#:
#: **不要往这里加表**：它是阶段 1 数据持久层的验收口径。后续阶段新增的功能表
#: （如 `subscriptions`）单列在下面，改这个元组会让"阶段 1 是否完成"这个
#: 已经结案的问题重新变得可疑。
SPEC_TABLES = (
    "users",
    "papers",
    "paper_versions",
    "chunks",
    "citations",
    "qa_history",
    "graph_snapshots",
    "agent_runs",
)

#: 规格之外、后续阶段新增的表。`/health/db` 与 `init_db` 只校验 SPEC_TABLES，
#: 这份清单供人工核对与迁移检查用。
EXTRA_TABLES = ("subscriptions",)

__all__ = [
    "Base",
    "CreatedAtMixin",
    "IntPKMixin",
    "TimestampMixin",
    "SPEC_TABLES",
    "EXTRA_TABLES",
    "User",
    "Paper",
    "PaperVersion",
    "Chunk",
    "CHUNK_TYPES",
    "Citation",
    "QAHistory",
    "GraphSnapshot",
    "AgentRun",
    "Subscription",
]
