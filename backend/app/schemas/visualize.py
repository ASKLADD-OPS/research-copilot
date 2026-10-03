"""CSV 数据可视化相关模型。

`/visualize/upload` 只负责**看清这份数据**（列类型、取值域、几行样例），
`/visualize/generate` 才去决定画什么 —— 两者分开是因为"上传"会被人反复点
（换文件、看列名），而"生成"要花钱调模型。上传的结果（`CsvDatasetOut`）
同时也是生成接口的输入回执：`dataset_id` 就是落盘文件的句柄。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

#: 规格里点名的六种图型。`pie` 不在其中 —— 占比图对 CSV 几乎总是被误用
#: （一张表里挑一列画饼，不如柱状图），它只留在 `/writing/diagram` 那条路上。
ChartType = Literal["line", "bar", "scatter", "heatmap", "boxplot", "radar"]

ColumnDtype = Literal["numeric", "datetime", "text"]


class CsvColumnProfile(BaseModel):
    """一列的类型判断与取值域。数值列的统计量只对数值列有值。"""

    name: str
    dtype: ColumnDtype
    missing: int = 0
    unique: int = 0
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    samples: list[str] = Field(default_factory=list, description="最多 3 个示例值")


class CsvDatasetOut(BaseModel):
    """上传回执。字段全部由后端从文件算出来，前端不参与推断。"""

    dataset_id: str = Field(description="落盘文件句柄，回传给 /visualize/generate")
    filename: str
    delimiter: str = ","
    rows: int = Field(description="读入的数据行数（不含表头）")
    total_rows: int = Field(description="文件里的实际数据行数（超过上限时大于 rows）")
    truncated: bool = Field(default=False, description="是否因行数上限被截断")
    columns: list[CsvColumnProfile] = Field(default_factory=list)
    numeric_columns: list[str] = Field(default_factory=list)
    categorical_columns: list[str] = Field(default_factory=list)
    head: list[list[str]] = Field(default_factory=list, description="前 5 行原样，给前端看个大概")
    warning: str = Field(default="", description="解析被降级/跳过时的原因，如实告知")


class ChartPlanOut(BaseModel):
    """Agent 的选图结论 —— 单独返回是为了让界面能显示"为什么选它"。"""

    chart_type: ChartType
    x: str = ""
    y: list[str] = Field(default_factory=list)
    title: str = ""
    ylabel: str = ""
    rationale: str = Field(default="", description="为什么这样画")


class VisualizeRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    chart_type: Literal["line", "bar", "scatter", "heatmap", "boxplot", "radar", "auto"] = Field(
        default="auto", description="auto = 让 Agent 自己选"
    )
    instruction: str = Field(default="", max_length=500, description="额外意图，如'只看 2024 年之后'")
    title: str = Field(default="", max_length=200, description="覆盖 Agent 拟的标题")


class VisualizeResult(BaseModel):
    chart_type: str
    caption: str = Field(description="学术风格图注，形如 'Figure 1: …'")
    image: str | None = Field(default=None, description="base64 PNG（不含 data: 前缀）")
    mime: str = "image/png"
    renderer: str = Field(default="matplotlib")
    warning: str = Field(default="", description="降级/跳过渲染时的原因")
    rationale: str = ""
    plan: dict[str, Any] = Field(default_factory=dict, description="ChartPlanOut 的原始形态")
    spec: dict[str, Any] = Field(default_factory=dict, description="真正喂给 matplotlib 的规格")
    elapsed_ms: int = 0


__all__ = [
    "ChartPlanOut",
    "ChartType",
    "ColumnDtype",
    "CsvColumnProfile",
    "CsvDatasetOut",
    "VisualizeRequest",
    "VisualizeResult",
]
