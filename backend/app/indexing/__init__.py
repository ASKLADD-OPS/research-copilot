"""解析入库流水线 + 进程内后台任务执行器。

- `pipeline.py`：同步的解析/嵌入/入库流水线（不知道自己被谁调度）
- `runner.py`：把流水线丢进线程池，并把进度写进 tasks 表
"""

from app.indexing.pipeline import index_paper, rebuild_citation_edges
from app.indexing.runner import spawn_index, spawn_rebuild_edges, wait_all

__all__ = ["index_paper", "rebuild_citation_edges", "spawn_index", "spawn_rebuild_edges", "wait_all"]
