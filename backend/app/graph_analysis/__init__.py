"""图分析 —— NetworkX 引文/合作网络。

对外只有三样东西：

- `CitationGraphAnalyzer`：分析门面（`graph_analyze` 工具与 /graph 端点都走它）
- `build_survey` / `suggest_future_directions`：图上跑的两次 LLM 调用
- `analysis` 模块本身：纯图论函数，无副作用，可逐条断言
"""

from app.graph_analysis.analyzer import ANALYSIS_KINDS, CitationGraphAnalyzer
from app.graph_analysis.builder import CitationGraphBuilder, LibraryIndex
from app.graph_analysis.future_ideas import FutureIdea, filter_ideas, suggest_future_directions
from app.graph_analysis.survey import Survey, build_survey

__all__ = [
    "ANALYSIS_KINDS",
    "CitationGraphAnalyzer",
    "CitationGraphBuilder",
    "FutureIdea",
    "LibraryIndex",
    "Survey",
    "build_survey",
    "filter_ideas",
    "suggest_future_directions",
]
