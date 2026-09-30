"""三库健康检查的响应模型。

「三库」= PostgreSQL（关系库）+ Milvus（向量库）+ Redis（缓存/预留）。
来源是阶段 0 的 compose 服务清单，以及阶段 1 验收标准 3。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

StoreStatus = Literal["ok", "degraded", "down"]

#: 三个存储的名字，顺序即返回顺序 —— 前端按固定顺序铺卡片，别让它们跳
STORE_NAMES = ("postgres", "milvus", "redis")


class StoreHealth(BaseModel):
    """单个存储的状态。

    `detail` 是给人和编排层看的短句（"8 张表齐全" / "连接被拒绝"），
    `info` 是给前端/排障用的结构化数据；两者都不该包含连接串或密码。
    """

    name: str
    ok: bool
    latency_ms: float | None = None
    detail: str = ""
    info: dict[str, Any] = Field(default_factory=dict)


class DbHealthReport(BaseModel):
    status: StoreStatus = Field(description="ok | degraded | down")
    components: list[StoreHealth] = Field(default_factory=list)
    #: 规格要求但库里缺的表。非空即说明迁移没跑到位 —— 比"status=degraded"更有指向性
    missing_tables: list[str] = Field(default_factory=list)
    spec_tables: list[str] = Field(default_factory=list)


__all__ = ["DbHealthReport", "STORE_NAMES", "StoreHealth", "StoreStatus"]
