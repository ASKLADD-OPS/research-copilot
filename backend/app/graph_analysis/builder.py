"""引文图构建 —— 本地 `citations` 表 + Semantic Scholar 补边。

两个数据源各补一半，缺一不可：

- **本地 citations 表**：PDF 参考文献解析出来的边，带正文里的引用片段
  （`context_snippet`）。精确、免费，但只覆盖"参考文献条目能被正确解析"的部分。
- **Semantic Scholar**：按 DOI / arXiv 号回溯 references / citations，
  **只用来在本库内部补边**（两篇都已入库、但 PDF 解析没连上的那种）。
  库外的被引论文不进图 —— 没有标题、没有年份的幽灵节点只会把布局搅乱。

节点 id 一律 `str`：NetworkX 的键可以是任意可哈希对象，但 JSON 序列化、
ECharts 前端、`nx.shortest_path` 的入参最终都要落到字符串上，在入口归一一次
比在五个出口各转一次可靠。

**补边是尽力而为**：单篇缺 DOI、S2 限流、网络不通都只是少几条边，
不让"用本地数据建图"这条路一起失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
from loguru import logger
from sqlalchemy import select

from app.db.session import session_scope
from app.models import Citation, Paper


def norm_title(title: str | None) -> str:
    """标题归一化键：压空白、小写、截断到 80 字符。

    **必须与 `pipeline.rebuild_citation_edges` / `pipeline._link_citations`
    用同一套规则**：三处只要有一处不同，就会出现"重建时连得上、建图时连不上"
    这种没法解释的边丢失。
    """
    return " ".join((title or "").split()).lower()[:80]


def s2_ref(doi: str | None, arxiv_id: str | None) -> str | None:
    """本地标识 → Semantic Scholar 的 paper id。两个都没有就补不了。"""
    if doi:
        return f"DOI:{doi}"
    if arxiv_id:
        return f"ARXIV:{arxiv_id}"
    return None


@dataclass(slots=True)
class LibraryIndex:
    """本地论文库的 id 索引 —— 把 S2 返回的论文对回本库的 paper_id。"""

    by_doi: dict[str, int] = field(default_factory=dict)
    by_arxiv: dict[str, int] = field(default_factory=dict)
    by_title: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: list[tuple[int, str | None, str | None]]) -> LibraryIndex:
        idx = cls()
        for pid, doi, arxiv_id in rows:
            if doi:
                idx.by_doi[str(doi).strip().lower()] = pid
            if arxiv_id:
                idx.by_arxiv[str(arxiv_id).strip().lower()] = pid
        return idx

    def add_title(self, paper_id: int, title: str | None) -> None:
        if title:
            self.by_title.setdefault(norm_title(title), paper_id)

    def resolve(self, *, doi: str | None = None, arxiv_id: str | None = None, title: str | None = None) -> int | None:
        """按"越可靠越先"的顺序匹配：DOI → arXiv → 标题。"""
        if doi and (hit := self.by_doi.get(str(doi).strip().lower())):
            return hit
        if arxiv_id and (hit := self.by_arxiv.get(str(arxiv_id).strip().lower())):
            return hit
        if title and (hit := self.by_title.get(norm_title(title))):
            return hit
        return None


def match_candidates(
    source_id: int, index: LibraryIndex, candidates: list[dict[str, Any]]
) -> list[tuple[int, str | None, dict[str, Any]]]:
    """S2 返回的候选论文 → 可连的边 `(target_id, context_snippet, 原始候选)`。

    **库外的直接丢**：图里放不进没有节点属性的论文（见模块 docstring），
    只有两端都已入库的引用才值得写进 `citations` 表。
    自引（`target == source`）也丢 —— 自环会让 PageRank / 最长路径都变怪。
    """
    out: list[tuple[int, str | None, dict[str, Any]]] = []
    for item in candidates:
        target = index.resolve(doi=item.get("doi"), arxiv_id=item.get("arxiv_id"), title=item.get("title"))
        if target is None or target == source_id:
            continue
        snippet = next((c for c in (item.get("contexts") or []) if c), None)
        out.append((target, (snippet or "")[:500] or None, item))
    return out


class CitationGraphBuilder:
    """把 `paper_ids` 这批论文的引用关系构建成有向图。

    边方向 = `source → target`，即"谁引了谁"（citing → cited）。
    """

    async def build(
        self,
        paper_ids: list[int] | None = None,
        *,
        enrich: bool = False,
        enrich_limit: int = 50,
    ) -> nx.DiGraph:
        """建图。`enrich=True` 时先跑一轮 Semantic Scholar 补边（需要外网）。

        `paper_ids` 为空 = 全库。给定范围时刻意**只把范围内的论文当节点** ——
        否则"只看这 20 篇"的图里会冒出一堆同样在库、但与这批论文无关的节点，
        用户按范围提问却拿到全库的图。
        """
        if enrich and paper_ids:
            try:
                await self.enrich(paper_ids, limit=enrich_limit)
            except Exception:  # noqa: BLE001 - 补边失败不能拖垮建图
                logger.warning("Semantic Scholar 补边失败，退回本地数据建图", exc_info=True)

        async with session_scope() as session:
            stmt = select(
                Citation.source_paper_id,
                Citation.target_paper_id,
                Citation.context_snippet,
            ).where(Citation.target_paper_id.is_not(None))
            meta_stmt = select(Paper.id, Paper.title, Paper.year, Paper.venue, Paper.citation_count, Paper.abstract)
            if paper_ids:
                scope = list(paper_ids)
                stmt = stmt.where(Citation.source_paper_id.in_(scope), Citation.target_paper_id.in_(scope))
                meta_stmt = meta_stmt.where(Paper.id.in_(scope))
            edges = (await session.execute(stmt)).all()
            meta = (await session.execute(meta_stmt)).all()

        graph = nx.DiGraph()
        for pid, title, year, venue, count, abstract in meta:
            graph.add_node(
                str(pid),
                title=(title or "").strip() or str(pid),
                year=year,
                venue=venue or "",
                citation_count=count,
                # 摘要只留前 300 字：它是 hover 卡片上的一行预览，
                # 全量摘要会让 300 节点的图变成几百 KB 的响应体。
                abstract=" ".join((abstract or "").split())[:300],
            )

        for source, target, snippet in edges:
            u, v = str(source), str(target)
            if u not in graph:  # 范围外的源节点（理论上被 SQL 挡掉了，双保险）
                continue
            if graph.has_edge(u, v) and graph[u][v].get("context_snippet"):
                continue  # 同一对论文多次引用：留第一条有正文片段的，不要反复覆盖
            graph.add_edge(u, v, context_snippet=(snippet or None))

        logger.info(
            "引文图构建完成：{} 节点 {} 边（范围={}）",
            graph.number_of_nodes(),
            graph.number_of_edges(),
            "全库" if not paper_ids else len(paper_ids),
        )
        return graph

    # ------------------------------------------------------------------ 补边
    async def enrich(self, paper_ids: list[int], *, limit: int = 50, max_papers: int = 30) -> dict[str, int]:
        """用 S2 的 references / citations / paper 补边并回填书目元数据。

        返回 `{"checked", "edges_added", "meta_filled"}`。

        `max_papers` 是**请求配额护栏**：每篇最多打 3 次 S2 请求，不加限制时
        一次"给全库补边"能把匿名配额打穿（S2 未带 key 时限约 1 req/s）。
        """
        # ponytail: 顺序发请求 + 硬上限。真需要全库补边时改成批量 `/paper/batch`
        # 端点（一次 500 篇），这里先够用。
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Paper.id, Paper.doi, Paper.arxiv_id).where(Paper.id.in_(list(paper_ids))).limit(max_papers)
                )
            ).all()
            lib = (await session.execute(select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title))).all()

        index = LibraryIndex.from_rows([(pid, doi, arxiv) for pid, doi, arxiv, _ in lib])
        for pid, _, _, title in lib:
            index.add_title(pid, title)

        checked = added = filled = 0
        for pid, doi, arxiv_id in rows:
            ref = s2_ref(doi, arxiv_id)
            if ref is None:
                continue
            checked += 1
            candidates: list[dict[str, Any]] = []
            for tool, key in (
                ("semantic_scholar_references", "references"),
                ("semantic_scholar_citations", "citations"),
            ):
                out = await self._call(tool, {"paper_id": ref, "limit": limit})
                candidates.extend((out or {}).get(key) or [])

            # 主体自己的书目：年份/期刊/被引数，本地 PDF 多半解析不出来
            own = await self._call("semantic_scholar_paper", {"paper_id": ref})
            filled += await self._backfill(pid, (own or {}).get("paper"))

            added += await self._link(pid, index, candidates)

        result = {"checked": checked, "edges_added": added, "meta_filled": filled}
        logger.info("Semantic Scholar 补边完成 {}", result)
        return result

    async def _link(self, source_id: int, index: LibraryIndex, candidates: list[dict[str, Any]]) -> int:
        """把 S2 返回的候选里、**本库已有的**那些连成边，返回新增边数。

        顺带回填被引方的书目元数据 —— S2 反正已经给了，不写就得下次再问一遍。
        """
        wanted = match_candidates(source_id, index, candidates)
        if not wanted:
            return 0

        async with session_scope() as session:
            existing = {
                int(t)
                for (t,) in (
                    await session.execute(
                        select(Citation.target_paper_id).where(
                            Citation.source_paper_id == source_id, Citation.target_paper_id.is_not(None)
                        )
                    )
                ).all()
            }
            fresh = [(t, snip) for t, snip, _ in wanted if t not in existing]
            for target, snippet in fresh:
                session.add(Citation(source_paper_id=source_id, target_paper_id=target, context_snippet=snippet))

        for target, _, item in wanted:
            await self._backfill(target, item)
        return len(fresh)

    async def _backfill(self, paper_id: int, paper: dict[str, Any] | None) -> int:
        """把 S2 的书目写回本库（**只填空，不覆盖**已有值）。返回写入的字段数。"""
        if not paper:
            return 0
        async with session_scope() as session:
            row = await session.get(Paper, paper_id)
            if row is None:
                return 0
            n = 0
            if row.year is None and paper.get("year"):
                row.year, n = int(paper["year"]), n + 1
            if not row.venue and paper.get("venue"):
                row.venue, n = str(paper["venue"])[:255], n + 1
            if row.citation_count is None and paper.get("citation_count") is not None:
                row.citation_count, n = int(paper["citation_count"]), n + 1
        return n

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """经 MCP 统一入口调外部工具。

        `call_tool` 已经把异常折成可读字符串回喂给 LLM，这里只关心
        "拿到的是不是结构化结果"；不是就当作没拿到。
        """
        from app.agents.mcp.registry import call_tool

        result = await call_tool(name, args)
        if isinstance(result, dict):
            return result
        logger.info("S2 工具 {} 未返回结构化结果：{}", name, str(result)[:160])
        return None


__all__ = ["CitationGraphBuilder", "LibraryIndex", "match_candidates", "norm_title", "s2_ref"]
