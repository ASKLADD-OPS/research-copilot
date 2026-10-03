"""CSV 数据可视化。

| 端点 | 干什么 | 收什么 / 给什么 |
|---|---|---|
| `POST /visualize/upload` | 收下 CSV、判列类型 | 文件 → `CsvDatasetOut`（列画像 + 前 5 行）；`dataset_id` 是后续句柄 |
| `POST /visualize/generate` | 选图型 → 出 PNG + 学术图注 | `dataset_id` → `VisualizeResult` |

两个端点分开的理由：「上传」会被人反复点（换文件、看列名），而「生成」要花钱调模型。

**渲染器固定是 matplotlib，且不 exec 任何模型写的代码** —— 模型只产出"画哪种图、用哪两列"，
数值一律从 CSV 现取。这条边界与 `/writing/diagram` 共用（见 `app/writing/diagrams.py` 模块头），
两条路的降级行为因此完全一致：缺字体、列里没数、渲染报错，都如实写进 `warning` 而不假装成功。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from loguru import logger

from app.schemas import ApiResponse, CsvDatasetOut, VisualizeRequest, VisualizeResult
from app.visualize import csv_charts

router = APIRouter(prefix="/visualize", tags=["可视化"])


@router.post(
    "/upload",
    response_model=ApiResponse[CsvDatasetOut],
    summary="上传 CSV 并分析列类型",
    operation_id="visualize_upload",
)
async def upload_csv(file: Annotated[UploadFile, File(description="CSV 文件（逗号/分号/制表符分隔）")]) -> ApiResponse[CsvDatasetOut]:
    """读入 CSV 并逐列判类型（numeric / datetime / text），返回列画像与前 5 行。

    **这一步不调模型**：判类型是纯规则（按比例看前 200 个非空值），所以上传是即时返回的。
    """
    raw = await file.read()
    filename = file.filename or "data.csv"
    dataset_id, _ = csv_charts.save_csv(filename, raw)
    profile, _ = csv_charts.load_dataset(dataset_id)

    parts = [f"{profile.rows} 行 × {len(profile.columns)} 列"]
    if profile.numeric_columns:
        parts.append(f"数值列 {len(profile.numeric_columns)}")
    if profile.categorical_columns:
        parts.append(f"分类列 {len(profile.categorical_columns)}")
    if profile.truncated:
        parts.append(f"已截断（原 {profile.total_rows} 行）")
    return ApiResponse.ok(profile, message=" · ".join(parts))


@router.post(
    "/generate",
    response_model=ApiResponse[VisualizeResult],
    summary="生成图表",
    operation_id="visualize_generate",
)
async def generate(payload: VisualizeRequest) -> ApiResponse[VisualizeResult]:
    """`chart_type=auto` 时由 Agent 读列画像选图型，否则按指定的图型直接画。

    支持折线 / 柱状 / 散点 / 热力图 / 箱线图 / 雷达图。**任何图型不适用于这份数据都不算请求失败**：
    自动退到柱状图，原因写在 `warning` 里 —— 一张说明白的错图比一个 500 有用。
    """
    profile, rows = csv_charts.load_dataset(payload.dataset_id)
    result = await csv_charts.generate_chart(payload, profile, rows)

    logger.info("可视化完成 kind={} renderer={} 用时={}ms", result.chart_type, result.renderer, result.elapsed_ms)
    message = result.chart_type
    if result.image:
        message += f" · 已出图 {len(result.image) // 1024}KB(base64)"
    else:
        message += " · 仅规格（未渲染）"
    if result.warning:
        message += f" · {result.warning}"
    return ApiResponse.ok(result, message=message)
