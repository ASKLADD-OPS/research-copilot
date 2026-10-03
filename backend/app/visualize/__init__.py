"""CSV 数据可视化。

- `read_csv` / `profile_dataset` —— 看清一份 CSV（类型、取值域、样例）
- `plan_chart` / `build_spec` —— 选图型、把选型落成 `ChartSpec`
- `generate_chart` —— 渲染成 PNG + 学术图注（复用 `app.writing.diagrams` 的渲染路径）

对外只有 `app/api/v1/visualize.py` 两个端点用得到的东西。
"""

from app.visualize.csv_charts import (
    csv_dir,
    dataset_path,
    generate_chart,
    load_dataset,
    parse_csv,
    plan_chart,
    profile_dataset,
    read_csv,
    save_csv,
)

__all__ = [
    "csv_dir",
    "dataset_path",
    "generate_chart",
    "load_dataset",
    "parse_csv",
    "plan_chart",
    "profile_dataset",
    "read_csv",
    "save_csv",
]
