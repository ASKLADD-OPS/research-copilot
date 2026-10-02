"""架构拓扑图 / 数据图生成与渲染（TikZ · Graphviz · Mermaid · Matplotlib）。

四种类型的源码由模型写，能不能出图由**本机有没有对应渲染器**决定，缺什么就如实说，
不假装成功：

| kind | 产出源码 | 本机渲染路径 | 缺渲染器时 |
|---|---|---|---|
| `tikz` | LaTeX `tikzpicture` | 需要 TeX 发行版 | **只返回源码**（见下） |
| `graphviz` | DOT | `dot -Tpng` | 退化为 networkx + matplotlib 的拓扑草图 |
| `mermaid` | Mermaid | `mmdc`（Mermaid CLI，需 Chrome） | 只返回源码 |
| `matplotlib` | 图表**规格**（不是代码） | 进程内 Agg 渲染 | 无（matplotlib 是硬依赖） |

两条与规格字面不同、但方向是**更安全**的取舍
------------------------------------------------
1. **matplotlib 不做 `exec`。** 规格写的是"生成 Matplotlib 代码 → 沙箱执行"，
   但执行模型写的 Python 是这套系统里唯一"模型能碰到解释器"的地方 ——
   为它专门维护一个沙箱，是拿最大的攻击面换一张图。改成让模型只产出一个**图表规格**
   （`ChartSpec`，字段与既有 `make_chart` 工具同一套），绘图代码是我们自己的固定片段：
   **模型不能决定要执行什么**，于是这条边界不依赖沙箱成立。
   顺带的好处是规格可以校验（series 长度对不上直接报错，而不是产出一张空图）。
2. **TikZ 只返回源码。** TikZ 的正确用法就是贴进论文的 `.tex` 让 TeX 编译；
   服务端渲染它要装一整套几 GB 的 TeX 发行版。`graphviz` 缺 `dot` 时退化成拓扑草图
   是因为 DOT 描述的就是"谁连谁"（草图仍是有意义的近似）；TikZ 是**绘图指令**，
   近似渲染等于画一张假图，那比不画更糟。

`renderer` 字段永远告诉你图是从哪来的：`dot` / `mmdc` / `matplotlib` /
`networkx-fallback` / `none`（只有源码）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured
from app.schemas import DiagramRequest, DiagramResult

# matplotlib 无显示环境：必须在 import pyplot 之前定好后端，否则某些发行版会去找 X11。
os.environ.setdefault("MPLBACKEND", "Agg")

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

#: Chrome 常见安装位置（mmdc 要靠它把 Mermaid 画成 PNG）。
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def _strip_fence(text: str) -> str:
    """模型很爱把整段源码裹进 ``` 围栏里 —— Mermaid/DOT 解析器见到围栏直接报语法错。"""
    body = (text or "").strip()
    if body.startswith("```"):
        body = _FENCE_RE.sub("", body)
        lines = body.splitlines()
        if lines and lines[-1].strip() == "```":
            lines.pop()
        body = "\n".join(lines)
    return body.strip()


# ==================================================================== 由模型产出的源码
class DiagramCode(BaseModel):
    source: str = Field(description="图的源码：TikZ 的 tikzpicture 环境 / DOT / Mermaid，不要 markdown 围栏")
    caption: str = Field(default="", max_length=300, description="学术风格的图注，形如 'Figure 1: ...'")


class ChartSeries(BaseModel):
    name: str = Field(default="", max_length=60)
    data: list[float] = Field(default_factory=list, max_length=64)


class ChartSpec(BaseModel):
    """matplotlib 的输入。刻意与 `app.agents.mcp.local_tools.make_chart` 同构。"""

    chart_type: Literal["bar", "line", "pie", "scatter"] = "bar"
    title: str = Field(default="", max_length=200)
    xlabel: str = Field(default="", max_length=80)
    ylabel: str = Field(default="", max_length=80)
    categories: list[str] = Field(default_factory=list, max_length=64)
    series: list[ChartSeries] = Field(default_factory=list, max_length=8)
    caption: str = Field(default="", max_length=300)


_COMMON = """要求：
- 只画「要画的东西」里出现的实体与关系，不要补你不确定的东西。
- 节点文字用简短的名词短语（中文 2~8 字），不要整句。
- 图注 `caption` 写成 'Figure N: …' 的学术风格，一句话。
只输出 JSON，不要解释。"""

_SYSTEMS: dict[str, str] = {
    "tikz": """你是 LaTeX 绘图专家。输出一段 TikZ 架构图。

硬规则：
1. 只输出 `\\begin{tikzpicture}...\\end{tikzpicture}`，**不要** documentclass /
   \\begin{document} / \\usepackage —— 它会被贴进论文已有的 .tex 里。
2. 用 `\\node[draw,rounded corners,minimum width=2.4cm,align=center]` 画模块，
   `\\draw[->]` 画箭头，用 `positioning` 库的相对定位（right=of / below=of）。
3. 需要分层时自建 style（如 `layer/.style={draw,fill=blue!5}`），不要用未定义的样式。
4. 中文节点要能被 XeLaTeX 编译：不要在 TikZ 里写 \\usepackage，但要保证括号与逗号配对。
"""
    + _COMMON,
    "graphviz": """你是 Graphviz 专家。输出一段 DOT。

硬规则：
1. 输出完整 `digraph G { ... }`，以 `rankdir=LR;` 或 `rankdir=TB;` 开头。
2. 用 `node [shape=box, style="rounded,filled", fillcolor="#eef"]` 统一节点外观；
   子模块用 `subgraph cluster_x { label="…"; … }` 分组。
3. 边写 `a -> b [label="…"];`，标签要短。
4. 节点 id 用 ASCII 标识符（a1 / parser / indexer），中文放进 `label="…"`。
"""
    + _COMMON,
    "mermaid": """你是 Mermaid 专家。输出一段 Mermaid 流程图。

硬规则：
1. 第一行必须是 `flowchart TD` 或 `flowchart LR`。
2. 节点写 `A[名称]`，边写 `A --> B`，条件分支用 `B -->|是| C`。
3. 子图用 `subgraph 分组名 ... end`，不要嵌套超过两层。
4. 不要写样式类（classDef 容易把渲染搞崩），不要用 HTML 标签。
"""
    + _COMMON,
    "matplotlib": """你是数据可视化专家。根据要画的东西给出**图表规格**（不是代码）。

硬规则：
1. `chart_type` 取 bar / line / pie / scatter 之一；趋势用 line，对比用 bar，占比用 pie。
2. **数据只能来自「要画的东西」里已经给出的数字**。一个数字都没有时，
   `series` 留空数组 —— 不要编造数值（编出来的图比没有图更糟）。
3. `series[i].data` 的长度必须等于 `categories` 的长度（scatter 时两两成对）。
4. `caption` 写成 'Figure N: …'。
只输出 JSON，不要解释。""",
}


async def _generate_source(kind: str, instruction: str, context: str) -> DiagramCode | ChartSpec:
    prompt = f"要画的东西：{instruction}\n" + (f"\n可依据的材料：\n{context}" if context.strip() else "")
    schema: type[BaseModel] = ChartSpec if kind == "matplotlib" else DiagramCode
    return await complete_structured(
        schema,
        [{"role": "system", "content": _SYSTEMS[kind]}, {"role": "user", "content": prompt}],
        role=Role.EXECUTOR,
        temperature=0.2,
    )


# ==================================================================== 渲染器探测
def _find(configured: str, name: str, *extra: str) -> str | None:
    """配置优先，其次 PATH，最后几个常见安装路径。找不到返回 None（不是错误）。"""
    if configured.strip():
        path = Path(configured.strip())
        return str(path) if path.exists() else None
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        if Path(cand).exists():
            return cand
    return None


def find_chrome() -> str | None:
    return _find(settings.DIAGRAM_CHROME, "chrome", *_CHROME_CANDIDATES)


def find_dot() -> str | None:
    return _find(
        settings.DIAGRAM_GRAPHVIZ_DOT,
        "dot",
        r"C:\Program Files\Graphviz\bin\dot.exe",
        r"C:\Program Files (x86)\Graphviz\bin\dot.exe",
        "/usr/bin/dot",
        "/opt/homebrew/bin/dot",
    )


def find_mmdc() -> str | None:
    return _find(settings.DIAGRAM_MERMAID_CMD, "mmdc", "/usr/local/bin/mmdc", "/opt/homebrew/bin/mmdc")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    """跑外部渲染器。**剥离环境变量**：渲染器不需要任何凭据，就不该看得到它们。"""
    try:
        proc = subprocess.run(  # noqa: S603 - 命令与参数都由本模块拼装，不含模型输入
            cmd, cwd=str(cwd), env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return -1, f"渲染超时（>{timeout}s）"
    except OSError as exc:
        return -1, f"无法执行 {cmd[0]}：{exc}"
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return proc.returncode or 0, err[-1500:]


def _child_env() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME", "USERPROFILE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ==================================================================== matplotlib
_FONT_CANDIDATES = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans")


def _use_cjk_fonts() -> list[str]:
    """只把**本机真装了**的字体交给 matplotlib。

    把候选名一股脑塞进 `rcParams` 会让 matplotlib 每次排字都找不到 'PingFang SC' 就警告一次 ——
    中文标签一多，日志就被 findfont 刷满，真正的错误反而看不见（实测：一张图刷 30 行）。
    返回实际选中的字体列表，`draw_networkx_labels` 直接用它（它接的是 font_family 而不是 rcParams）。
    """
    import matplotlib

    matplotlib.rcParams["axes.unicode_minus"] = False  # 中文字体下负号会变成方块
    try:
        from matplotlib import font_manager

        installed = {f.name for f in font_manager.fontManager.ttflist}
        chosen = [name for name in _FONT_CANDIDATES if name in installed]
    except Exception:  # noqa: BLE001 - 字体表读不出来不该挡住画图
        chosen = []
    chosen = chosen or ["DejaVu Sans"]
    matplotlib.rcParams["font.sans-serif"] = chosen
    return chosen


def _chart_png(spec: ChartSpec) -> bytes:
    """把规格画成 PNG。**代码是固定的**，模型只能决定数据与类型（见模块头第 1 条）。"""
    _use_cjk_fonts()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=160)
    cats = spec.categories
    if spec.chart_type == "pie":
        values = list(spec.series[0].data) if spec.series else []
        labels = cats[: len(values)] or [f"#{i + 1}" for i in range(len(values))]
        if values:
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
    elif spec.chart_type == "scatter":
        for s in spec.series:
            xs = list(range(len(s.data))) if not cats else list(range(min(len(s.data), len(cats))))
            ax.scatter(xs, s.data[: len(xs)], label=s.name or None, s=28)
    else:
        n = len(spec.series)
        width = 0.8 / max(1, n)
        for i, s in enumerate(spec.series):
            xs = list(range(len(s.data)))
            if spec.chart_type == "bar":
                ax.bar([x + (i - (n - 1) / 2) * width for x in xs], s.data, width=width, label=s.name or None)
            else:
                ax.plot(xs, s.data, marker="o", label=s.name or None)
        if cats:
            ax.set_xticks(range(len(cats)))
            ax.set_xticklabels(cats, rotation=0, fontsize=8)
    if spec.title:
        ax.set_title(spec.title, fontsize=11)
    if spec.xlabel:
        ax.set_xlabel(spec.xlabel, fontsize=9)
    if spec.ylabel:
        ax.set_ylabel(spec.ylabel, fontsize=9)
    if len(spec.series) > 1:
        ax.legend(fontsize=8)
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ==================================================================== DOT 兜底草图
_NODE_DEF = re.compile(r'^\s*"?([^"\[]+?)"?\s*\[(.*)\]\s*$')
_ARROW = re.compile(r'"?\s*(?:->|--)\s*"?')
_LABEL = re.compile(r'label\s*=\s*"([^"]*)"')
_HEADER = re.compile(r"^\s*(?:strict\s+)?(?:di)?graph\b[^{]*\{", re.IGNORECASE)
_SKIP = ("subgraph", "node ", "edge ", "rankdir", "ranksep", "nodesep", "graph ", "digraph")
_BARE = re.compile(r"^[{}]+$")


def dot_topology(source: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """从 DOT 里抠出「节点名 → 显示名」与连边。**只认最常用的一小撮语法**。

    这是一个明确的近似：它不认识 record 形状、rank=same、端口（`a:f0 -> b`）、
    HTML 标签标签。目的只是"没有 Graphviz 时也能看一眼谁连谁"，
    真排版还得靠 `dot`（装了就会被自动用上，见 `find_dot`）。

    **先剥掉最外层的 `digraph G { … }` 再按语句切**，而不是逐行扫：模型很爱把整张图
    写成一行 `digraph G { a -> b; }`，逐行扫会因为首行以 `digraph` 开头而整行跳过，
    结果一张图都解析不出来（实测踩过）。
    """
    text = re.sub(r"//[^\n]*", "", source or "")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = _HEADER.sub("", text, count=1)

    edges: list[tuple[str, str]] = []
    labels: dict[str, str] = {}
    for raw in re.split(r"[;\n]", text):
        line = raw.strip()
        if not line or _BARE.match(line):
            continue
        low = line.lower()
        if low.startswith(_SKIP):
            continue

        # 先判边：`a -> b [label="x"]` 的属性段里也有方括号，先当节点定义去匹配会把
        # "a -> b" 整个当成节点名。
        head = line.split("[")[0]
        if "->" in head or "--" in head:
            parts = [p.strip().strip('"') for p in _ARROW.split(head) if p.strip()]
            edges.extend((parts[i], parts[i + 1]) for i in range(len(parts) - 1))
            continue

        match = _NODE_DEF.match(line)
        if match:
            name = match.group(1).strip()
            label = _LABEL.search(match.group(2))
            labels[name] = label.group(1) if label else name

    for a, b in edges:
        labels.setdefault(a, a)
        labels.setdefault(b, b)
    return edges, labels


def _topology_png(edges: list[tuple[str, str]], labels: dict[str, str], title: str) -> bytes:
    chosen = _use_cjk_fonts()
    import matplotlib.pyplot as plt
    import networkx as nx

    graph = nx.DiGraph()
    graph.add_edges_from(edges)
    if graph.number_of_nodes() == 0:
        raise ValueError("DOT 源里没解析出任何节点或连边")
    pos = nx.spring_layout(graph, seed=7, k=1.2 / max(1, graph.number_of_nodes() ** 0.5))
    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=160)
    nx.draw_networkx_edges(graph, pos, ax=ax, arrows=True, arrowstyle="-|>", edge_color="#8a8f98", width=1.0)
    nx.draw_networkx_nodes(
        graph, pos, ax=ax, node_color="#efeaff", node_size=1600, edgecolors="#805ce5", linewidths=1.0
    )
    nx.draw_networkx_labels(
        graph, pos, labels={n: labels.get(n, n) for n in graph.nodes}, ax=ax, font_size=8, font_family=chosen
    )
    if title:
        ax.set_title(title, fontsize=10)
    ax.axis("off")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ==================================================================== 三个外部渲染器
def render_graphviz(source: str) -> tuple[str | None, str, str]:
    """`(base64|None, renderer, warning)`。有 `dot` 就用它，没有就画拓扑草图。"""
    dot = find_dot()
    if dot:
        with tempfile.TemporaryDirectory(prefix="dot_") as tmp:
            work = Path(tmp)
            (work / "g.dot").write_text(source, encoding="utf-8")
            code, err = _run(
                [dot, "-Tpng", "-Gdpi=160", "g.dot", "-o", "g.png"],
                cwd=work,
                env=_child_env(),
                timeout=settings.DIAGRAM_TIMEOUT,
            )
            png = work / "g.png"
            if code == 0 and png.exists() and png.stat().st_size:
                return _b64(png.read_bytes()), "dot", ""
            warning = f"Graphviz 渲染失败（exit={code}）：{err or '无 stderr'}"
            logger.warning("{}", warning)
    else:
        warning = "本机未安装 Graphviz（找不到 dot），已改用 networkx 画拓扑草图（不看 rank/端口等排版语义）"

    try:
        edges, labels = dot_topology(source)
        return _b64(_topology_png(edges, labels, "")), "networkx-fallback", warning
    except Exception as exc:  # noqa: BLE001 - 兜底再失败就只给源码
        logger.warning("DOT 兜底渲染失败：{}", exc)
        return None, "none", f"{warning}；兜底渲染也失败：{exc}"


def render_mermaid(source: str) -> tuple[str | None, str, str]:
    mmdc = find_mmdc()
    if not mmdc:
        return None, "none", "本机未安装 Mermaid CLI（mmdc），只返回源码"
    chrome = find_chrome()
    if not chrome:
        return None, "none", "mmdc 需要 Chrome 才能出图，但在常见位置都没找到；只返回源码"

    with tempfile.TemporaryDirectory(prefix="mmd_") as tmp:
        work = Path(tmp)
        (work / "d.mmd").write_text(source, encoding="utf-8")
        # --no-sandbox：在容器/受限环境里 Chrome 的沙箱起不来；这个 Chrome 只渲染我们自己的
        # 静态 SVG，不加载外部页面，所以关掉沙箱的收益大于风险。
        (work / "puppeteer.json").write_text(
            json.dumps({"args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]}),
            encoding="utf-8",
        )
        env = _child_env()
        env["PUPPETEER_EXECUTABLE_PATH"] = chrome
        code, err = _run(
            [mmdc, "-i", "d.mmd", "-o", "d.png", "-b", "white", "-p", "puppeteer.json"],
            cwd=work,
            env=env,
            timeout=settings.DIAGRAM_TIMEOUT,
        )
        png = work / "d.png"
        if code == 0 and png.exists() and png.stat().st_size:
            return _b64(png.read_bytes()), "mmdc", ""
        return None, "none", f"Mermaid 渲染失败（exit={code}）：{err or '无 stderr'}"


# ==================================================================== 入口
async def generate_diagram(req: DiagramRequest) -> DiagramResult:
    """生成一张图。**任何渲染失败都不抛异常** —— 源码一定带回去，让用户至少能拿去编译。"""
    started = time.perf_counter()
    kind = req.kind

    spec: ChartSpec | None = None
    code: DiagramCode | None = None
    if kind == "matplotlib" and isinstance(req.data, dict) and req.data:
        # 调用方把数据给全了就别再问模型 —— 图里的数字必须是用户给的
        spec = ChartSpec.model_validate(req.data)
    else:
        generated = await _generate_source(kind, req.instruction, req.context)
        if isinstance(generated, ChartSpec):
            spec = generated
        else:
            code = generated

    if kind == "matplotlib":
        assert spec is not None  # noqa: S101 - 分支上只有这两种可能
        if not spec.series or not any(s.data for s in spec.series):
            return DiagramResult(
                kind=kind,
                source=spec.model_dump_json(indent=2, exclude_none=True),
                caption=spec.caption,
                renderer="none",
                warning="没有可用的数值数据，未作图（不编造数值）",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        try:
            # matplotlib 是纯本地计算、无网络无进程，放线程里跑就够了 —— 不必为它起子进程。
            png = await asyncio.to_thread(_chart_png, spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("matplotlib 渲染失败：{}", exc)
            return DiagramResult(
                kind=kind,
                source=spec.model_dump_json(indent=2, exclude_none=True),
                caption=spec.caption,
                renderer="none",
                warning=f"matplotlib 渲染失败：{exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return DiagramResult(
            kind=kind,
            source=spec.model_dump_json(indent=2, exclude_none=True),
            caption=spec.caption,
            image=_b64(png),
            renderer="matplotlib",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    assert code is not None  # noqa: S101 - 源码型分支上只可能是 DiagramCode
    source = _strip_fence(code.source)
    if kind == "graphviz":
        image, renderer, warning = await asyncio.to_thread(render_graphviz, source)
    elif kind == "mermaid":
        image, renderer, warning = await asyncio.to_thread(render_mermaid, source)
    else:  # tikz：刻意不装 TeX（见模块头第 2 条）
        image, renderer, warning = (
            None,
            "none",
            "TikZ 由 LaTeX 编译：把下面这段贴进论文的 .tex 里即可（本机未安装 TeX 工具链，故不做预览）",
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    logger.info("图表生成 kind={} renderer={} chars={} 用时={}ms", kind, renderer, len(source), elapsed)
    return DiagramResult(
        kind=kind,
        source=source,
        caption=code.caption,
        image=image,
        renderer=renderer,
        warning=warning,
        elapsed_ms=elapsed,
    )


__all__ = [
    "ChartSeries",
    "ChartSpec",
    "DiagramCode",
    "dot_topology",
    "find_chrome",
    "find_dot",
    "find_mmdc",
    "generate_diagram",
    "render_graphviz",
    "render_mermaid",
]
