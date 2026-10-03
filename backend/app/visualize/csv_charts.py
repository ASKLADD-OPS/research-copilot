"""CSV → 图表规格 → PNG。

三段流水线，每段都能单独跑、单独断言：

1. `read_csv` —— 定界符嗅探 + 行数上限。**纯 stdlib**（`csv` / `io`）：
   把一张表读成 `list[list[str]]` 只要几十行，为"上传 CSV 看张图"背上整套
   DataFrame 语义（索引、dtype 推断、NaN 传播）不划算。
2. `profile_dataset` —— 逐列判类型（numeric / datetime / text）与取值域。
   类型按**比例**判而不是全量：一列 200 行里 3 行写着 "N/A"，它仍然是数值列。
3. `plan_chart` → `build_spec` → `generate_diagram` —— 模型只决定"画哪种图、用哪两列"，
   **每个数字都从 CSV 里现取**，模型既碰不到数值也碰不到解释器。这段边界与
   `app/writing/diagrams.py` 的模块头是同一条理由，两条路共用同一套 `ChartSpec`。

模型给的列名一律回表核对（`_resolve`）：编错的列名会被丢掉，不会变成一张空图。
"""

from __future__ import annotations

import csv
import io
import re
import time
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.errors import BadRequestError
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured
from app.schemas.visualize import (
    ChartPlanOut,
    ChartType,
    CsvColumnProfile,
    CsvDatasetOut,
    VisualizeRequest,
    VisualizeResult,
)
from app.writing.diagrams import ChartSeries, ChartSpec, generate_diagram

MAX_ROWS = 20_000  # 超过就截断：图里放不下两万个点，读进来的意义只是喂统计量
MAX_FILE_MB = 10
_MAX_POINTS = 64  # 与 ChartSeries.data 的 max_length 对齐
_MAX_HEATMAP_COLS = 24  # 热力图的列（分组值）超过这个数就糊成一片
_MAX_BOX_GROUPS = 8
_MAX_RADAR_GROUPS = 6
_SAMPLE_VALUES = 200  # 判类型只看前 200 个非空值
_NUMERIC_RATIO = 0.8

#: `dataset_id` 是客户端回传的，会拼进文件路径 —— 只允许我们自己生成的字符集。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?")


# ==================================================================== 落盘位置
def csv_dir() -> Path:
    """CSV 落盘目录。挂在 PDF 目录的兄弟位（`data/csv`）—— 同为"用户传上来的文件"。"""
    path = settings.upload_path.parent / "csv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def dataset_path(dataset_id: str) -> Path:
    if not _ID_RE.match(dataset_id):
        raise BadRequestError("dataset_id 不合法")
    path = csv_dir() / f"{dataset_id}.csv"
    if not path.exists():
        raise BadRequestError(f"数据集 {dataset_id} 不存在，请重新上传")
    return path


# ==================================================================== 1. 读
def parse_csv(text: str, max_rows: int | None = None) -> tuple[list[str], list[list[str]], str, int, bool, str]:
    """`(表头, 数据行, 定界符, 总行数, 是否截断, 提示)`。**不抛解析异常**，出问题就降级。

    与 `read_csv` 拆开是为了能被直接喂一段文本（正是测试要干的事）——
    否则每个用例都得先往磁盘写个临时文件。
    """
    max_rows = max_rows or MAX_ROWS
    warning = ""
    delimiter = ","
    sample = "\n".join(text.splitlines()[:20])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # 嗅探失败很常见（单列文件、中文逗号），退化成"哪个定界符切出的列最多"
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        delimiter = max(counts, key=lambda k: counts[k]) if any(counts.values()) else ","
        warning = f"定界符嗅探失败，按 '{delimiter}' 解析"

    raw = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if not raw:
        raise BadRequestError("CSV 是空的（没有解析出任何一行）")

    width = max(len(r) for r in raw)
    header = [(c.strip() or f"col_{i + 1}") for i, c in enumerate(raw[0] + [""] * (width - len(raw[0])))]
    body = [r + [""] * (width - len(r)) for r in raw[1:]]
    total = len(body)
    truncated = total > max_rows
    if truncated:
        body = body[:max_rows]
        warning = f"{warning}；" if warning else ""
        warning += f"只读入前 {max_rows} 行（文件共 {total} 行），统计量基于截断后的数据"
    return header, body, delimiter, total, truncated, warning


def read_csv(
    path: Path, max_rows: int | None = None
) -> tuple[list[str], list[list[str]], str, int, bool, str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise BadRequestError(f"读取 CSV 失败：{exc}") from exc
    return parse_csv(text, max_rows)


def save_csv(filename: str, content: bytes) -> tuple[str, Path]:
    """落盘并返回 `dataset_id`。文件名只用于展示，落盘名是随机串（避免路径穿越）。"""
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise BadRequestError(f"CSV 超过上限 {MAX_FILE_MB}MB")
    if not content.strip():
        raise BadRequestError("上传的文件是空的")

    import hashlib
    import uuid

    dataset_id = f"{Path(filename).stem[:24].replace(' ', '_')[:24]}-{uuid.uuid4().hex[:8]}"
    dataset_id = re.sub(r"[^A-Za-z0-9_-]", "", dataset_id) or hashlib.sha1(content).hexdigest()[:12]
    dest = csv_dir() / f"{dataset_id}.csv"
    dest.write_bytes(content)
    return dataset_id, dest


# ==================================================================== 2. 判类型
def _as_float(value: str) -> float | None:
    v = (value or "").strip()
    return float(v) if _NUMBER_RE.match(v) else None


def _is_date(value: str) -> bool:
    return bool(_DATE_RE.match((value or "").strip()))


def _profile_column(name: str, values: list[str]) -> CsvColumnProfile:
    non_empty = [v for v in values if v.strip()]
    sample = non_empty[:_SAMPLE_VALUES]
    numeric_hits = sum(1 for v in sample if _as_float(v) is not None)
    date_hits = sum(1 for v in sample if _is_date(v))

    dtype: Literal["numeric", "datetime", "text"] = "text"
    if sample and numeric_hits / len(sample) >= _NUMERIC_RATIO:
        dtype = "numeric"
    elif sample and date_hits / len(sample) >= _NUMERIC_RATIO:
        dtype = "datetime"

    minimum = maximum = mean = None
    if dtype == "numeric":
        numbers = [n for n in (_as_float(v) for v in non_empty) if n is not None]
        if numbers:
            minimum, maximum, mean = min(numbers), max(numbers), fmean(numbers)

    distinct: list[str] = []
    for v in non_empty:
        if v not in distinct:
            distinct.append(v)
        if len(distinct) == 3:
            break

    return CsvColumnProfile(
        name=name,
        dtype=dtype,
        missing=len(values) - len(non_empty),
        unique=len(set(non_empty)),
        min=minimum,
        max=maximum,
        mean=mean,
        samples=distinct,
    )


def profile_dataset(
    dataset_id: str,
    filename: str,
    header: list[str],
    rows: list[list[str]],
    delimiter: str,
    total_rows: int,
    truncated: bool,
    warning: str,
) -> CsvDatasetOut:
    columns = [_profile_column(header[i], [r[i] for r in rows]) for i in range(len(header))]
    return CsvDatasetOut(
        dataset_id=dataset_id,
        filename=filename,
        delimiter=delimiter,
        rows=len(rows),
        total_rows=total_rows,
        truncated=truncated,
        columns=columns,
        numeric_columns=[c.name for c in columns if c.dtype == "numeric"],
        categorical_columns=[c.name for c in columns if c.dtype == "text" and c.unique <= max(50, len(rows) // 4)],
        head=[[c.strip() for c in header]] + [list(r) for r in rows[:5]],
        warning=warning,
    )


# ==================================================================== 3. 选图型
class _ChartPlan(BaseModel):
    """给模型的最小决策面。**只有"画什么"没有"画成什么样"** —— 数值它一个也拿不到。"""

    chart_type: ChartType = "bar"
    x: str = Field(default="", description="分类轴/分组列的名称，必须来自列清单")
    y: list[str] = Field(default_factory=list, description="数值列名称，必须来自列清单（radar 需 ≥3）")
    title: str = Field(default="", max_length=200)
    ylabel: str = Field(default="", max_length=80)
    caption: str = Field(default="", max_length=280, description="学术风格图注，形如 'Figure 1: …'")
    rationale: str = Field(default="", max_length=200)


_PLAN_SYSTEM = """你是数据可视化专家。给你一份 CSV 的**列清单与统计量**（没有原始数据），选一种图表并指定用哪两列。

选型规则：
- `line` 趋势（x 是时间列时首选）；`bar` 类别间对比；`scatter` 两个数值列的相关性；
  `heatmap` 一个分组列 × 多列数值的矩阵；`boxplot` 分组后的分布差异；`radar` ≥3 个指标的多实体对比。
- `x` / `y` 只能填**列清单里出现过的列名**，一个字都不能改（写错会导致这列被丢弃）。
- `y` 必须是数值列；`heatmap` / `boxplot` / `radar` 的 `x` 尽量用分类列。
- `caption` 写成 'Figure 1: …' 的学术风格，一句话，说明画的是什么、以什么为轴。
- **图注只能描述屏幕上真有的东西**：`line` / `bar` 在同一个 x 上有好几行时会先取均值再作图，
  所以不要写成"各区域…"这类分组明细的说法（实测踩过：图画的是全区均值，图注却说"各区域趋势"）。
- 中文标签，克制，不要形容词。
只输出 JSON，不要解释。"""


def _heuristic_plan(profile: CsvDatasetOut) -> _ChartPlan:
    """模型不可用时的兜底选型。纯规则，够用就不算降级。"""
    has_time = any(c.dtype == "datetime" for c in profile.columns)
    nums = profile.numeric_columns
    cats = profile.categorical_columns
    if has_time and nums:
        kind: ChartType = "line"
    elif cats and nums:
        kind = "bar"
    elif len(nums) >= 2:
        kind = "scatter"
    else:
        kind = "bar"
    return _ChartPlan(chart_type=kind, y=nums[:3], rationale=f"未调用模型，按列类型直接判为 {kind}")


def _describe(profile: CsvDatasetOut) -> str:
    lines = [f"文件：{profile.filename}（{profile.rows} 行 × {len(profile.columns)} 列）", "列清单："]
    for c in profile.columns:
        stats = ""
        if c.dtype == "numeric" and c.min is not None:
            stats = f", 范围 {c.min:g}~{c.max:g}, 均值 {c.mean:.3g}" if c.mean is not None else ""
        lines.append(f"- {c.name}（{c.dtype}, {c.unique} 个不同值{stats}）示例: {' / '.join(c.samples)}")
    return "\n".join(lines)


async def plan_chart(profile: CsvDatasetOut, req: VisualizeRequest) -> tuple[_ChartPlan, str]:
    """选图型。`chart_type` 被指定时不调模型（省一次调用，也让"我就想看这个"立刻生效）。"""
    if req.chart_type != "auto":
        return _ChartPlan(chart_type=req.chart_type, y=profile.numeric_columns[:3]), ""
    prompt = _describe(profile) + (f"\n\n额外要求：{req.instruction}" if req.instruction.strip() else "")
    try:
        plan = await complete_structured(
            _ChartPlan, [{"role": "user", "content": prompt}], role=Role.EXECUTOR, temperature=0.2
        )
        return plan, ""
    except Exception as exc:  # noqa: BLE001 - 选型失败不该让整张图失败
        logger.warning("图表选型失败，改用规则兜底：{}", exc)
        return _heuristic_plan(profile), f"选型模型调用失败（{exc}），已按列类型直接选图"


# ==================================================================== 4. 规格构建
def _distinct(values: list[str], cap: int | None = None) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
            if cap and len(seen) >= cap:
                break
    return seen


def _even_sample(items: list, size: int) -> list:
    if len(items) <= size:
        return items
    step = len(items) / size
    return [items[int(i * step)] for i in range(size)]


def _pick_x(profile: CsvDatasetOut, requested: str) -> str:
    names = [c.name for c in profile.columns]
    if requested and requested in names:
        return requested
    for c in profile.columns:  # 时间是折线图的天然 x
        if c.dtype == "datetime":
            return c.name
    if profile.categorical_columns:
        return profile.categorical_columns[0]
    return names[0] if names else ""


def _pick_group(profile: CsvDatasetOut, requested: str) -> str:
    if requested in profile.categorical_columns:
        return requested
    return profile.categorical_columns[0] if profile.categorical_columns else ""


def _pick_y(profile: CsvDatasetOut, requested: list[str], least: int) -> list[str]:
    kept = [y for y in requested if y in profile.numeric_columns]
    for name in profile.numeric_columns:  # 模型没给够就按列顺序补齐
        if len(kept) >= least:
            break
        if name not in kept:
            kept.append(name)
    return kept[:8]


def _axis_spec(kind: str, profile: CsvDatasetOut, rows: list[list[str]], plan: _ChartPlan) -> ChartSpec:
    """line / bar。同一 x 上有多行时取均值；x 的取值太多（连续量）则按行均匀抽样。"""
    names = [c.name for c in profile.columns]
    xcol = _pick_x(profile, plan.x)
    xi = names.index(xcol)
    ycols = _pick_y(profile, plan.y, 1)
    yi = {y: names.index(y) for y in ycols}
    cats_all = _distinct([r[xi] for r in rows])

    if len(cats_all) <= _MAX_POINTS:
        index = {c: i for i, c in enumerate(cats_all)}
        acc: dict[str, list[list[float]]] = {y: [[] for _ in cats_all] for y in ycols}
        for r in rows:
            j = index[r[xi]]
            for y in ycols:
                v = _as_float(r[yi[y]])
                if v is not None:
                    acc[y][j].append(v)
        # 一个值都没有的 x 直接丢掉：画成贴地的 0 会被读成"这个类别的值就是 0"
        keep = [j for j in range(len(cats_all)) if any(acc[y][j] for y in ycols)]
        cats = [cats_all[j] for j in keep]
        series = [ChartSeries(name=y, data=[round(fmean(acc[y][j]), 6) for j in keep]) for y in ycols]
    else:
        picked = _even_sample(rows, _MAX_POINTS)
        cats = [r[xi] for r in picked]
        series = [ChartSeries(name=y, data=[_as_float(r[yi[y]]) or 0.0 for r in picked]) for y in ycols]

    # 兜底标题会**直接变成图注正文**（模型没参与时 `_caption` 拿它当 fallback），
    # 所以它得是一句读得通的话，不能是"month 上的 revenue"这种列名拼接。
    fallback_title = (
        f"{', '.join(ycols)} 随 {xcol} 的变化" if kind == "line" else f"各 {xcol} 的 {', '.join(ycols)}"
    ) if ycols else xcol
    return ChartSpec(
        chart_type=kind,  # type: ignore[arg-type]
        title=plan.title or fallback_title,
        xlabel=xcol,
        ylabel=plan.ylabel or (ycols[0] if len(ycols) == 1 else ""),
        categories=cats[: _MAX_POINTS * 4],
        series=series,
    )


def _scatter_spec(profile: CsvDatasetOut, rows: list[list[str]], plan: _ChartPlan) -> ChartSpec:
    names = [c.name for c in profile.columns]
    ycols = _pick_y(profile, plan.y, 1)
    xcol = plan.x if plan.x in profile.numeric_columns and plan.x not in ycols else ""
    if not xcol:
        rest = [c for c in profile.numeric_columns if c not in ycols]
        xcol = rest[0] if rest else (ycols[0] if ycols else "")
        # x 是我们替它挑的 —— 说明没人指定这对坐标，那就只留一条 y。
        # 真跑出来的教训：revenue(120) / cost(75) / users(1100) 三个量纲差两个数量级的系列
        # 挤在同一根 y 轴上，谁高谁低全是刻度幻觉（实测 caption 会写成
        # "latency_ms vs revenue, cost, users"，那张图没有可读的含义）。
        ycols = ycols[:1]
    xi = names.index(xcol) if xcol in names else 0
    yi = {y: names.index(y) for y in ycols if y in names}

    picked = _even_sample(rows, _MAX_POINTS)
    cats = [r[xi] for r in picked]
    series = [
        ChartSeries(name=y, data=[v for v in (_as_float(r[i]) for r in picked) if v is not None]) for y, i in yi.items()
    ]
    return ChartSpec(
        chart_type="scatter",
        title=plan.title or f"{xcol} vs {', '.join(yi)}",
        xlabel=xcol,
        ylabel=plan.ylabel or (next(iter(yi), "") if len(yi) == 1 else ""),
        categories=cats,
        series=series,
    )


def _heatmap_spec(profile: CsvDatasetOut, rows: list[list[str]], plan: _ChartPlan) -> ChartSpec:
    """分组列 × 数值列 的均值矩阵。行是数值列（series），列是分组值（categories）。"""
    names = [c.name for c in profile.columns]
    group = _pick_group(profile, plan.x)
    gi = names.index(group)
    ycols = _pick_y(profile, plan.y, 1)
    yi = {y: names.index(y) for y in ycols}
    groups = _distinct([r[gi] for r in rows], _MAX_HEATMAP_COLS)
    gindex = {g: i for i, g in enumerate(groups)}

    acc: dict[str, list[list[float]]] = {y: [[] for _ in groups] for y in ycols}
    for r in rows:
        j = gindex.get(r[gi])
        if j is None:
            continue
        for y in ycols:
            v = _as_float(r[yi[y]])
            if v is not None:
                acc[y][j].append(v)
    series = [
        ChartSeries(
            name=y,
            data=[round(fmean(acc[y][j]), 6) if acc[y][j] else 0.0 for j in range(len(groups))],
        )
        for y in ycols
    ]
    return ChartSpec(
        chart_type="heatmap",
        title=plan.title or f"{group} × {', '.join(ycols)} 均值矩阵",
        xlabel=group,
        ylabel=plan.ylabel or "指标",
        categories=groups,
        series=series,
    )


def _boxplot_spec(profile: CsvDatasetOut, rows: list[list[str]], plan: _ChartPlan) -> ChartSpec:
    names = [c.name for c in profile.columns]
    group = _pick_group(profile, plan.x)
    gi = names.index(group)
    ycol = _pick_y(profile, plan.y, 1)[0]
    value_i = names.index(ycol)

    groups = _distinct([r[gi] for r in rows], _MAX_BOX_GROUPS)
    series = [
        ChartSeries(
            name=g,
            data=_even_sample([v for v in (_as_float(r[value_i]) for r in rows if r[gi] == g) if v is not None], 64),
        )
        for g in groups
    ]
    series = [s for s in series if s.data]
    return ChartSpec(
        chart_type="boxplot",
        title=plan.title or f"各 {group} 的 {ycol} 分布",
        xlabel=group,
        ylabel=plan.ylabel or ycol,
        categories=[s.name for s in series],
        series=series,
    )


def _radar_spec(profile: CsvDatasetOut, rows: list[list[str]], plan: _ChartPlan) -> ChartSpec:
    """多指标雷达图。**按轴归一化到 0~1**：不做的话"引用数 1200 / 准确率 0.9"两个轴
    会各自撑满半径，读出来的形状纯属刻度幻觉（`ponytail:` 只做除以最大值，够用）。"""
    names = [c.name for c in profile.columns]
    axes = _pick_y(profile, plan.y, 3)
    if len(axes) < 3:
        raise ValueError("雷达图至少需要 3 个数值列")
    idx = {a: names.index(a) for a in axes}
    group = _pick_group(profile, plan.x)
    groups = _distinct([r[names.index(group)] for r in rows], _MAX_RADAR_GROUPS) if group else ["全体"]
    gi = names.index(group) if group else -1

    raw: dict[str, dict[str, float]] = {}
    for g in groups:
        bucket = [r for r in rows if (gi < 0 or r[gi] == g)]
        raw[g] = {}
        for a in axes:
            vals = [v for v in (_as_float(r[idx[a]]) for r in bucket) if v is not None]
            raw[g][a] = fmean(vals) if vals else 0.0
    tops = {a: max((raw[g][a] for g in groups), default=0.0) for a in axes}

    series = [
        ChartSeries(
            name=g, data=[round(raw[g][a] / tops[a], 4) if tops[a] else 0.0 for a in axes]
        )
        for g in groups
    ]
    return ChartSpec(
        chart_type="radar",
        title=plan.title or f"{', '.join(axes)} 的归一化对比",
        categories=axes,
        ylabel=plan.ylabel or "归一化值（各轴除以最大值）",
        series=series,
    )


def _caption(text: str, fallback: str, index: int = 1) -> str:
    """图注一定要是 `Figure N: …` 的形状 —— 这是验收标准里唯一一处格式硬要求。"""
    body = re.sub(r"\s+", " ", (text or "").strip())
    if not re.match(r"^figure\s*\d", body, re.IGNORECASE):
        body = f"Figure {index}: {body.lstrip(':：. ')}" if body else f"Figure {index}: {fallback}"
    return body[:300]


def build_spec(plan: _ChartPlan, profile: CsvDatasetOut, rows: list[list[str]]) -> tuple[ChartSpec, str]:
    """把"选型"落成可渲染的规格。**任何一步不成立都退回柱状图**，不产出空图。"""
    builders = {
        "line": lambda: _axis_spec("line", profile, rows, plan),
        "bar": lambda: _axis_spec("bar", profile, rows, plan),
        "scatter": lambda: _scatter_spec(profile, rows, plan),
        "heatmap": lambda: _heatmap_spec(profile, rows, plan) if _pick_group(profile, plan.x) else None,
        "boxplot": lambda: _boxplot_spec(profile, rows, plan) if _pick_group(profile, plan.x) else None,
        "radar": lambda: _radar_spec(profile, rows, plan),
    }
    # 有日期列就退成折线（连续趋势），否则退成柱状 —— 两者都比空图有用
    fallback = "line" if any(c.dtype == "datetime" for c in profile.columns) else "bar"
    warning = ""
    spec: ChartSpec | None = None
    try:
        spec = builders[plan.chart_type]()
    except Exception as exc:  # noqa: BLE001 - 规格构建失败就降级，不要把 500 抛给用户
        warning = f"{plan.chart_type} 图不适用于这份数据（{exc}），已改用{fallback}图"
    if spec is None:
        if not warning:
            warning = f"数据里找不到 {plan.chart_type} 图需要的分组列，已改用{fallback}图"
        # 提示里的图型必须与**真画出来的那个**一致：降级信息与实际图不符比不降级更误导
        spec = _axis_spec(fallback, profile, rows, plan)

    spec.caption = _caption(plan.caption, spec.title, 1)
    return spec, warning


# ==================================================================== 入口
def load_dataset(dataset_id: str) -> tuple[CsvDatasetOut, list[list[str]]]:
    """按 `dataset_id` 把一份 CSV 读回来：`(列画像, 数据行)`。

    读 + 判类型这两步合成一个入口，是因为**两个端点都要它**（上传时回执、生成时重建），
    分成两处调用迟早会漂移出两套口径。
    """
    path = dataset_path(dataset_id)
    header, rows, delimiter, total, truncated, warning = read_csv(path)
    profile = profile_dataset(dataset_id, path.stem, header, rows, delimiter, total, truncated, warning)
    return profile, rows


async def generate_chart(req: VisualizeRequest, profile: CsvDatasetOut, rows: list[list[str]]) -> VisualizeResult:
    """选型 → 规格 → 渲染。渲染复用 `/writing/diagram` 那条路（同一套 `ChartSpec`），
    所以缺字体、缺数值、渲染报错时的降级行为两条路完全一致。"""
    started = time.perf_counter()

    plan, plan_warning = await plan_chart(profile, req)
    spec, spec_warning = build_spec(plan, profile, rows)
    if req.title:
        spec.title = req.title

    from app.schemas import DiagramRequest

    result = await generate_diagram(
        DiagramRequest(kind="matplotlib", instruction=req.instruction or "CSV 数据图表", data=spec.model_dump())
    )

    warnings = [w for w in (plan_warning, spec_warning, result.warning) if w]
    # `plan.x` 如实回传模型的原始选择（可能是编错的列名）——界面要能解释"为什么图长这样"
    plan_out = ChartPlanOut(
        chart_type=spec.chart_type,  # type: ignore[arg-type]
        x=plan.x,
        y=plan.y,
        title=spec.title,
        ylabel=spec.ylabel,
        rationale=plan.rationale,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    logger.info(
        "CSV 图表 kind={} renderer={} rows={} 列={} 用时={}ms",
        spec.chart_type,
        result.renderer,
        profile.rows,
        len(spec.series),
        elapsed,
    )
    return VisualizeResult(
        chart_type=spec.chart_type,
        caption=result.caption,
        image=result.image,
        mime=result.mime,
        renderer=result.renderer,
        warning="；".join(warnings),
        rationale=plan.rationale,
        plan=plan_out.model_dump(),
        spec=spec.model_dump(),
        elapsed_ms=elapsed,
    )


__all__ = [
    "MAX_FILE_MB",
    "MAX_ROWS",
    "build_spec",
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
