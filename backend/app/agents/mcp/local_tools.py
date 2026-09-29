"""本地工具实现 —— 与 MCP 工具同名的进程内能力。

为什么不做成 MCP Server：这些工具直接读写本进程的 Milvus 连接、NetworkX 图对象，
走 MCP 协议要先把对象序列化再反序列化，纯亏。对 Agent 的可见接口与 MCP 工具一致
（都经 `registry.call_tool` 分发），所以后续想抽出去只改 registry 的一行。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.llm.client import Role, get_llm


async def retrieve_papers(
    query: str,
    top_k: int = 8,
    paper_ids: list[str] | None = None,
) -> dict[str, Any]:
    """从**本地论文库**做混合检索（Dense + Sparse + RRF + 重排）。

    何时使用：回答关于已入库论文的任何问题 —— 这是获取**证据**的唯一正当来源。
    何时不要用：找库外的新论文（用 arxiv_search / semantic_scholar_search）。

    Args:
        query: 检索式。用论文里可能出现的术语原文（英文更准），不要用口语化转述。
        top_k: 返回片段数，1-20。
        paper_ids: 限定检索范围到这几篇论文（可选）。

    Returns:
        {"n": int, "chunks": [{"index","chunk_id","paper_id","section","page","text"}]}
        其中 `index` 即引用时使用的 `[n]` 编号。
    """
    from app.rag.retriever import HybridRetriever

    chunks = await HybridRetriever().retrieve(query, paper_ids=paper_ids, top_k=max(1, min(top_k, 20)))
    return {
        "n": len(chunks),
        "query": query,
        "chunks": [
            {
                "index": i,
                "chunk_id": c.id,
                "paper_id": c.paper_id,
                "section": c.section or "",
                "page": c.page_start or 0,
                "score": round(c.final_score, 4),
                "text": c.content[:2000],
            }
            for i, c in enumerate(chunks, start=1)
        ],
    }


async def graph_analyze(
    analysis: str = "overview",
    paper_ids: list[str] | None = None,
) -> dict[str, Any]:
    """分析引文 / 合作网络的图结构（基于 NetworkX）。

    何时使用：问"这个方向的关键论文是谁""哪些工作形成了社区""谁被引用最多"。
    何时不要用：问论文内容本身（用 retrieve_papers）。

    Args:
        analysis: `overview`（规模概览）| `pagerank`（影响力排序）| `communities`（社区划分）
            | `path`（两篇之间的引用路径，需配合 paper_ids）。
        paper_ids: 参与分析的论文范围（可选，默认全库）。

    Returns:
        {"analysis": str, "result": Any, "n_nodes": int, "n_edges": int}
    """
    from app.graph_analysis.analyzer import CitationGraphAnalyzer

    analyzer = CitationGraphAnalyzer()
    return await analyzer.run(analysis=analysis, paper_ids=paper_ids)


async def make_chart(
    chart_type: str = "bar",
    title: str = "",
    categories: list[str] | None = None,
    series: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成 ECharts 图表配置，前端直接渲染。

    何时使用：用户要看趋势、对比、分布，且数据已从检索/计算中得到。
    **不要**用它做"帮我分析"—— 数据必须已经在手，本工具只负责把数据变成图。

    Args:
        chart_type: `bar` | `line` | `pie` | `scatter`。
        title: 图表标题。
        categories: X 轴类目（pie 时为扇区名）。
        series: `[{"name": "方法A", "data": [1,2,3]}]`。

    Returns:
        {"option": <ECharts option>, "chart_type": str}
    """
    kind = chart_type if chart_type in ("bar", "line", "pie", "scatter") else "bar"
    cats = categories or []
    sers = series or []

    if kind == "pie":
        option = {
            "title": {"text": title},
            "tooltip": {"trigger": "item"},
            "series": [
                {
                    "type": "pie",
                    "radius": "55%",
                    "data": [
                        {"name": c, "value": (sers[0]["data"][i] if sers and i < len(sers[0]["data"]) else 0)}
                        for i, c in enumerate(cats)
                    ],
                }
            ],
        }
    else:
        option = {
            "title": {"text": title},
            "tooltip": {"trigger": "axis"},
            "legend": {"data": [s.get("name", "") for s in sers]},
            "xAxis": {"type": "category" if kind != "scatter" else "value", "data": cats},
            "yAxis": {"type": "value"},
            "series": [{"name": s.get("name", ""), "type": kind, "data": s.get("data", [])} for s in sers],
        }
    return {"option": option, "chart_type": kind}


async def write_section(
    instruction: str,
    section: str = "related_work",
    language: str = "zh",
    context: str = "",
) -> dict[str, Any]:
    """按学术写作规范撰写指定章节。

    何时使用：用户明确要求"帮我写 / 起草 / 润色"某段文字。
    何时不要用：用户只是问问题 —— 直接回答即可，不要主动生成论文段落。

    硬约束：**只能使用 `context` 里出现过的事实**。没有依据就不要写，
    在正文中说明"该点需补充文献"。

    Args:
        instruction: 写作要求（主题、篇幅、侧重）。
        section: `related_work` | `abstract` | `introduction` | `method` | `rebuttal` | `outline`。
        language: `zh` | `en`。
        context: 可引用的事实材料（通常是 retrieve_papers 的结果）。

    Returns:
        {"text": str, "section": str, "chars": int}
    """
    lang_name = "中文" if language.startswith("zh") else "English"
    prompt = (
        f"你是学术写作助手。用{lang_name}撰写 `{section}` 章节。\n"
        "规则：① 只用「可用材料」里出现的事实，缺依据就写「该点需补充文献」；"
        "② 不要编造引用编号或论文名；③ 不要写空话套话；④ 直接输出正文，不要解释。\n\n"
        f"写作要求：{instruction}\n\n可用材料：\n{context or '（无）'}"
    )
    text = await get_llm().complete(Role.EXECUTOR, [{"role": "user", "content": prompt}], temperature=0.5)
    return {"text": text, "section": section, "chars": len(text)}


async def translate_text(text: str, target: str = "zh", keep_terms: bool = True) -> dict[str, Any]:
    """翻译学术文本，保全公式、术语与引用编号。

    何时使用：用户要求翻译摘要、段落，或做双语对照。
    何时不要用：用户问的是内容而不是要翻译。

    Args:
        text: 待翻译文本（可为 LaTeX 片段）。
        target: `zh` | `en`。
        keep_terms: 是否保留术语英文原文（首次出现时中英对照）。

    Returns:
        {"text": str, "target": str}
    """
    lang_name = "中文" if target.startswith("zh") else "English"
    prompt = (
        f"把下面的学术文本翻译成{lang_name}。\n"
        "规则：① **公式、变量名、引用编号（如 [1]）原样保留**；"
        + ("② 专业术语首次出现时写成「中文（English）」；" if keep_terms else "")
        + "③ 不要增删内容，不要加解释；④ 只输出译文。\n\n原文：\n"
        + text
    )
    out = await get_llm().complete(Role.UTILITY, [{"role": "user", "content": prompt}], temperature=0.2)
    return {"text": out, "target": target}


__all__ = [
    "graph_analyze",
    "make_chart",
    "retrieve_papers",
    "translate_text",
    "write_section",
]


def _log_tools() -> None:
    logger.debug("本地工具已注册：retrieve_papers / graph_analyze / make_chart / write_section / translate_text")
