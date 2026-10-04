"""生成技术文档用的实验图表（浅色主题，与前端设计令牌一致）。

跑法（backend 根目录）：
    python benchmarks/make_figures.py
输出到 ../docs/figures/*.png
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIGDIR = ROOT / "docs" / "figures"
FIGDIR.mkdir(parents=True, exist_ok=True)

INK = "#0f161e"
MUTED = "#6b7280"
GRID = "#e5e7eb"
ACCENT = "#805ce5"
CANVAS = "#f7f7f8"

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "font.size": 10,
    "axes.titlesize": 12,
    "figure.dpi": 160,
})

try:  # 中文标签
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
except Exception:  # noqa: BLE001
    pass


def _grid(ax) -> None:
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def fig_routes(data: dict) -> None:
    s = data["summary"]
    routes = ["dense-only", "sparse-only", "RRF(k=60)"]
    metrics = ["recall@5", "recall@20", "mrr@10", "ndcg@10"]
    x = np.arange(len(metrics))
    w = 0.26
    colors = ["#c9c9d1", "#8f8f9e", ACCENT]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    for i, (r, c) in enumerate(zip(routes, colors)):
        vals = [s[r][m] for m in metrics]
        bars = ax.bar(x + (i - 1) * w, vals, w, label=r, color=c, zorder=3)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.015, f"{v:.3f}",
                    ha="center", fontsize=8, color=INK)
    ax.set_xticks(x, metrics)
    ax.set_ylim(0, 1.12)
    ax.set_ylabel("指标值")
    ax.set_title("检索链路消融：单路召回 vs RRF 融合（40 chunk / 12 query，BAAI/bge-m3）")
    ax.legend(frameon=False, ncols=3, loc="upper center", bbox_to_anchor=(0.5, -0.13))
    _grid(ax)
    fig.tight_layout()
    fig.savefig(FIGDIR / "fig_retrieval_routes.png", bbox_inches="tight")
    plt.close(fig)


def fig_query_type(data: dict) -> None:
    t = data["by_type_ndcg@10"]
    types = ["lexical", "semantic", "mixed"]
    routes = ["dense-only", "sparse-only", "RRF(k=60)"]
    colors = ["#c9c9d1", "#8f8f9e", ACCENT]
    x = np.arange(len(types))
    w = 0.26
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    for i, (r, c) in enumerate(zip(routes, colors)):
        vals = [t[k][r] for k in types]
        bars = ax.bar(x + (i - 1) * w, vals, w, label=r, color=c, zorder=3)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}",
                    ha="center", fontsize=8, color=INK)
    ax.set_xticks(x, [f"{k}\n({n} query)" for k, n in zip(types, (5, 5, 2))])
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("nDCG@10")
    ax.set_title("按查询类型拆分：稀疏路只在词法型查询上追平稠密路")
    ax.legend(frameon=False, ncols=3, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    _grid(ax)
    fig.tight_layout()
    fig.savefig(FIGDIR / "fig_query_type.png", bbox_inches="tight")
    plt.close(fig)


def fig_k(data: dict) -> None:
    k = data["k_sensitivity"]
    ks = sorted(int(x) for x in k)
    ndcg = [k[str(i)]["ndcg@10"] for i in ks]
    rec5 = [k[str(i)]["recall@5"] for i in ks]
    rec20 = [k[str(i)]["recall@20"] for i in ks]
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ax.plot(ks, ndcg, "o-", color=ACCENT, linewidth=2, label="nDCG@10", zorder=3)
    ax.plot(ks, rec5, "s--", color="#4b5563", linewidth=1.6, label="Recall@5", zorder=3)
    ax.plot(ks, rec20, "^:", color="#9ca3af", linewidth=1.6, label="Recall@20（候选召回）", zorder=3)
    ax.axvline(60, color="#e5a04e", linewidth=1.2, linestyle="--", zorder=2)
    ax.text(60, 0.36, " 文献默认 k=60", color="#b07d2e", fontsize=9, va="bottom")
    for i, v in zip(ks, ndcg):
        ax.text(i, v + 0.02, f"{v:.3f}", ha="center", fontsize=8, color=ACCENT)
    ax.set_xlabel("RRF 常数 k")
    ax.set_ylabel("指标值")
    ax.set_ylim(0.3, 1.05)
    ax.set_xticks(ks)
    ax.set_title("RRF 常数 k 敏感性：本语料上候选召回已饱和，k 只影响精排")
    ax.legend(frameon=False, ncols=3, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    _grid(ax)
    fig.tight_layout()
    fig.savefig(FIGDIR / "fig_k_sensitivity.png", bbox_inches="tight")
    plt.close(fig)


# 公开评测数据：DeepSeek-V3 技术报告 / Qwen2.5 模型卡 / OpenAI 官方
LLM_BENCH = {
    "DeepSeek-V3": {"MMLU": 88.5, "HumanEval": 82.6, "GSM8K": 95.5, "MATH-500": 90.2, "GPQA-D": 59.1},
    "Qwen2.5-72B": {"MMLU": 85.3, "HumanEval": 76.5, "GSM8K": 91.8, "MATH-500": 72.1, "GPQA-D": 49.0},
    "GPT-4o": {"MMLU": 87.2, "HumanEval": 90.2, "GSM8K": 92.2, "MATH-500": 74.6, "GPQA-D": 49.9},
}

# 牌价（每百万 tokens，标准时段；来源见技术文档脚注）
LLM_PRICE = {
    "DeepSeek-V3": (2.0, 8.0),
    "Qwen2.5-72B": (4.0, 12.0),
    "GPT-4o": (18.0, 72.0),  # $2.5/$10 按 1 USD ≈ 7.2 CNY 折算
}


def fig_llm() -> None:
    models = list(LLM_BENCH)
    dims = ["MMLU", "HumanEval", "GSM8K", "MATH-500", "GPQA-D"]
    x = np.arange(len(dims))
    w = 0.26
    colors = [ACCENT, "#4b8fd6", "#c9c9d1"]
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 3.6), gridspec_kw={"width_ratios": [2.1, 1]})

    ax = axes[0]
    for i, (m, c) in enumerate(zip(models, colors)):
        vals = [LLM_BENCH[m][d] for d in dims]
        bars = ax.bar(x + (i - 1) * w, vals, w, label=m, color=c, zorder=3)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.8, f"{v:.1f}",
                    ha="center", fontsize=7.2, color=INK)
    ax.set_xticks(x, dims)
    ax.set_ylim(0, 108)
    ax.set_ylabel("分数（%）")
    ax.set_title("公开基准对比（数值越高越好）")
    ax.legend(frameon=False, ncols=3, loc="upper center", bbox_to_anchor=(0.5, -0.13))
    _grid(ax)

    ax = axes[1]
    x2 = np.arange(len(models))
    w2 = 0.34
    inp = [LLM_PRICE[m][0] for m in models]
    outp = [LLM_PRICE[m][1] for m in models]
    b1 = ax.bar(x2 - w2 / 2, inp, w2, label="输入", color="#c9c9d1", zorder=3)
    b2 = ax.bar(x2 + w2 / 2, outp, w2, label="输出", color=ACCENT, zorder=3)
    for bs in (b1, b2):
        for b in bs:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() * 1.06, f"{b.get_height():g}",
                    ha="center", fontsize=7.5, color=INK)
    ax.set_yscale("log")
    ax.set_xticks(x2, ["DS-V3", "Qwen2.5", "GPT-4o"])
    ax.set_ylabel("¥ / 百万 tokens（对数轴）")
    ax.set_title("牌价对比")
    ax.legend(frameon=False, ncols=2, loc="upper center", bbox_to_anchor=(0.5, -0.13))
    _grid(ax)

    fig.tight_layout()
    fig.savefig(FIGDIR / "fig_llm_selection.png", bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    data = json.loads((FIGDIR / "retrieval_ablation.json").read_text(encoding="utf-8"))
    fig_routes(data)
    fig_query_type(data)
    fig_k(data)
    fig_llm()
    print("figures ->", FIGDIR)
    for p in sorted(FIGDIR.glob("*.png")):
        print("  ", p.name, f"{p.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
