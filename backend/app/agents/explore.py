"""主题探索闭环 —— 检索 → 下载 → 评分 → 反思 → 重规划 → 推荐。

为什么不在 LangGraph 主图里加节点
---------------------------------
主图（`app/agents/graph.py`）服务的是"对话问答"：它的状态、路由与四层循环被
531 条测试钉住，动作集合由意图决定；而这个闭环的动作集合是**固定**的
（写检索词 → 两路检索 → 打分 → 下 PDF → 评审 → 换词重来），不需要意图识别、
不需要并行 DAG、不需要溯源。两者塞进同一个 state 会把两套不等价的执行语义混在一起，
且任何改动都会波及主图的验收。所以这里实现成一个独立的、可观测的异步生成器：

- **每一步都 yield 一条 `(事件, 数据)`** —— SSE 层直接转发，定时调度器直接丢弃。
  轨迹不是"事后拼出来的日志"，而是这条流水线本身的输出。
- **任何一条流都以 `done` 或 `error` 收尾**（与 `app/llm/streaming.py` 的约定一致）。

事件协议（前端 `frontend/pages/app/tools/index.vue` 按此消费）
------------------------------------------------------------
    thought      {"stage": "...", "text": "...", "round": n}      这一轮在想什么
    action       {"name": "arxiv_search", "args": {...}, "round": n}
    observation  {"name": "...", "ok": bool, "text": "...", ...}    调完拿到了什么
    progress     {"stage": "...", "index": 1..6, "total": 6, "round": n}
    recommend    {RecommendationOut}
    done         {ExploreDoneOut}

进度分母固定为 6（`STAGES`）而不是"本次实际步数"：重规划会让篇数变化，
分母跟着变的话进度条会往回跳，那是比"不准"更糟的观感。

为什么 Reflector 与 Replanner 共用一次 LLM 调用
----------------------------------------------
Replanner 唯一的产出就是"下一轮该试什么检索词"，而那正是评审时最有信息量的判断 ——
把它拆成两次调用等于让第二个模型在看不到评审理由的情况下重新猜一遍。所以
`ExploreReflection.queries` 直接就是重规划结果，整条闭环只有 3 次 LLM 往返
（规划 / 评审 / —— 评审里含重规划），没有第 4 次。
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.agents.mcp.registry import call_tool
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured

#: 生成器产出的事件。`str` 而不是 `Event` 枚举 —— 与 `llm/streaming.py` 的
#: `Event.THOUGHT` 等取值一一对应，但这里不做导入，避免循环依赖。
ExploreEvent = tuple[str, dict[str, Any]]

#: 进度条的六个阶段。顺序即 index。
STAGES = ("plan", "search", "score", "download", "reflect", "recommend")
_TOTAL_STAGES = len(STAGES)

#: 打分时截断的正文字符数。bge-m3 编码成本随长度线性上涨，而"主题 × 标题+摘要前 600 字"
#: 已经足够判断是否同域 —— 再多也只是把方法细节算进去，反而稀释主题信号。
_SCORE_TEXT_CHARS = 600


class Candidate(TypedDict, total=False):
    """归一化后的一个候选（arXiv 与 S2 的字段名不同，先归一再用）。"""

    key: str  # 去重键：arxiv_id 优先，否则规范化标题
    source: str  # arxiv | semantic_scholar
    arxiv_id: str
    paper_id: str
    paper_id_int: int  # 入库后的 int64 主键（下载成功才有）
    title: str
    authors: list[str]
    abstract: str
    year: int | None
    venue: str
    url: str
    citation_count: int
    score: float
    downloaded: bool
    is_new: bool  # 是本次新入库（而不是复用库里已有的记录）
    note: str


class SearchPlan(BaseModel):
    """一轮的检索词。

    两路**分开写**：arXiv 吃布尔语法（`ti:"..." AND abs:...`），Semantic Scholar
    只吃自然语言短语（见两个 Server 的 docstring）。让模型写一份再由代码翻译过去，
    等于把"两种查询语言的差异"硬编码进来；直接把差异交给模型更省事也更准。
    """

    arxiv_query: str = Field(min_length=1, max_length=300, description="arXiv 检索式，支持布尔语法")
    s2_query: str = Field(min_length=1, max_length=300, description="Semantic Scholar 检索式，自然语言英文短语")
    reasoning: str = ""


class ExploreReflection(BaseModel):
    """Reflector 的评审结果。`queries` 同时是 Replanner 的产出（见模块头）。"""

    coverage: float = Field(ge=0.0, le=1.0, description="本轮结果对该主题的覆盖度")
    relevance: float = Field(ge=0.0, le=1.0, description="结果与主题的整体相关度")
    verdict: Literal["accept", "replan"] = "accept"
    critique: str = ""
    queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="下一轮该试的检索词（verdict=replan 时给 1~3 条，accept 时可空）",
    )

    @property
    def overall(self) -> float:
        return round((self.coverage + self.relevance) / 2, 4)


# ==================================================================== 事件构造
def _thought(stage: str, text: str, *, round_no: int) -> ExploreEvent:
    return ("thought", {"stage": stage, "text": text, "round": round_no})


def _action(name: str, args: dict[str, Any], *, round_no: int) -> ExploreEvent:
    return ("action", {"name": name, "args": args, "round": round_no})


def _observation(name: str, *, ok: bool, text: str, **extra: Any) -> ExploreEvent:
    return ("observation", {"name": name, "ok": ok, "text": text, **extra})


def _progress(stage: str, *, round_no: int, detail: str = "") -> ExploreEvent:
    return (
        "progress",
        {
            "stage": stage,
            "index": STAGES.index(stage) + 1,
            "total": _TOTAL_STAGES,
            "round": round_no,
            "detail": detail,
        },
    )


# ==================================================================== 工具返回归一
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_NON_WORD = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _year_of(value: Any) -> int | None:
    """`published="2024-01-15"` 与 `year=2024` 都要认。"""
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def _norm_title(title: str) -> str:
    return _NON_WORD.sub("", title.lower())


def candidates_of(tool_name: str, payload: Any) -> list[Candidate]:
    """把 MCP 工具的返回整成统一的 `Candidate` 列表。

    两个 Server 的字段名不同（arXiv 给 `arxiv_id`，S2 给 `paper_id`），
    下游的评分与推荐不应该关心这个差异，所以归一化只做一次、只在这里做。
    """
    if not isinstance(payload, dict):
        return []
    source = "arxiv" if tool_name.startswith("arxiv") else "semantic_scholar"

    out: list[Candidate] = []
    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:  # 没标题的条目既没法去重也没法给人看
            continue
        arxiv_id = str(raw.get("arxiv_id") or "").strip()
        out.append(
            Candidate(
                key=arxiv_id or _norm_title(title),
                source=source,
                arxiv_id=arxiv_id,
                paper_id=str(raw.get("paper_id") or ""),
                title=title,
                authors=[str(a) for a in (raw.get("authors") or [])][:12],
                abstract=str(raw.get("abstract") or ""),
                year=_year_of(raw.get("published") or raw.get("year")),
                venue=str(raw.get("venue") or ""),
                url=str(raw.get("url") or ""),
                citation_count=int(raw.get("citation_count") or 0),
                score=0.0,
                downloaded=False,
                note="",
            )
        )
    return out


def _score_text(cand: Candidate) -> str:
    return f"{cand.get('title', '')}\n{cand.get('abstract', '')}".strip()[:_SCORE_TEXT_CHARS]


async def score_candidates(topic: str, candidates: list[Candidate]) -> None:
    """就地写入 `score`（与主题的 bge-m3 稠密余弦）。

    为什么是稠密余弦而不是让 LLM 打分：LLM 打分不可复现、按 token 计费、且无法
    给出"0.7"这种可以写进验收标准的阈值。bge-m3 的查询与文档共用同一语义空间
    （见 `BGEM3Embedder.encode_query` 的注释），`normalize_dense` 已把向量单位化，
    所以点积就是余弦 —— 不需要额外的归一化步骤。
    """
    if not candidates:
        return
    # 懒加载：`app.embeddings` 会拉起 torch / FlagEmbedding，模块级导入会让
    # `import app.agents.explore` 就付这个代价（单测里尤其不可接受）。
    from app.embeddings import aencode_dense

    vectors = await aencode_dense([topic, *(_score_text(c) for c in candidates)])
    if len(vectors) != len(candidates) + 1:  # pragma: no cover - 编码器不该改变长度
        raise RuntimeError(f"嵌入返回条数不符：期望 {len(candidates) + 1}，实得 {len(vectors)}")
    base = vectors[0]
    for cand, vec in zip(candidates, vectors[1:], strict=True):
        cand["score"] = round(max(0.0, min(1.0, sum(a * b for a, b in zip(base, vec, strict=True)))), 4)


# ==================================================================== Planner
_PLAN_SYSTEM = """你是学术检索策略助手。给定一个研究主题，产出两路**互补**的检索式。

硬规则：
1. `arxiv_query` 用 arXiv 语法，术语保留英文原文；可用 `ti:` / `abs:` 限定字段，
   需要多概念组合时用 `AND` / `OR`（例如 `ti:"mixture of experts" AND abs:routing`）。
2. `s2_query` 用**自然语言英文短语** —— Semantic Scholar 不支持布尔语法。
3. 两路要有分工：arXiv 一路偏**精确**（抓住该主题的核心术语），
   Semantic Scholar 一路偏**宽泛**（抓住同义表述与相邻方向），不要写成同一句话。
4. 不要带年份、期刊名、"survey"、"review" 这类套话 —— 它们会把召回拉偏。
5. 只输出 JSON，不要解释。"""


async def plan_queries(topic: str, *, feedback: str = "", round_no: int = 1) -> SearchPlan:
    """生成（或改写）检索词。失败时退化为主题原文 —— 一步不搜好过整条闭环崩掉。"""
    hint = f"\n上一轮存在的问题（本轮必须避开）：{feedback}" if feedback else ""
    try:
        return await complete_structured(
            SearchPlan,
            [
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": f"研究主题：{topic}\n这是第 {round_no} 轮检索。{hint}"},
            ],
            role=Role.PLANNER,
        )
    except Exception as exc:  # noqa: BLE001 - 模型不可用不该让探索彻底失败
        logger.warning("探索检索词生成失败，退化为主题原文: {}", exc)
        return SearchPlan(arxiv_query=topic, s2_query=topic, reasoning=f"规划失败退化为主题原文：{exc}")


# ==================================================================== Reflector
_REFLECT_SYSTEM = """你是学术检索质量评审员。看一批"主题 + 检索到的论文标题与相关度分数"，
判断这轮结果够不够用。

评分口径（0~1）：
- `coverage`：这批结果有没有覆盖该主题的**主要子方向**。全是同一个子方向 → 低。
- `relevance`：按标题看，有多少条是真的在做这个主题，而不是引用了同一个词。

判定口径：
- `accept`：结果里已经有一批明确对题的论文，再看下去也是重复。
- `replan`：明显缺了某个关键子方向，或者召回被同义词带偏了 —— 这时必须给出
  1~3 条**新的**检索词（`queries`），指向上一轮没覆盖到的表述。

`queries` 要写成英文，不要重复上一轮已经用过的词。只输出 JSON。"""


def _digest(candidates: list[Candidate], limit: int = 12) -> str:
    ranked = sorted(candidates, key=lambda c: c.get("score") or 0.0, reverse=True)[:limit]
    return "\n".join(
        f"- [{c.get('score', 0.0):.2f}] {c.get('title', '')[:120]}{f' ({c.get("year")})' if c.get('year') else ''}"
        for c in ranked
    )


async def reflect(
    topic: str,
    candidates: list[Candidate],
    *,
    downloaded: int,
    min_score: float,
    used_queries: list[str],
) -> ExploreReflection:
    """评审本轮结果。LLM 不可用时退化为确定性判定（由调用方的硬规则兜底）。"""
    passed = sum(1 for c in candidates if (c.get("score") or 0.0) >= min_score)
    mean = (sum(c.get("score") or 0.0 for c in candidates) / len(candidates)) if candidates else 0.0
    fallback = ExploreReflection(
        coverage=0.0,
        relevance=round(mean, 4),
        verdict="accept",
        critique="评审模型不可用，仅给出确定性统计（按阈值判定是否继续）。",
    )
    if not candidates:
        return ExploreReflection(coverage=0.0, relevance=0.0, verdict="replan", critique="本轮没有任何候选。")

    try:
        return await complete_structured(
            ExploreReflection,
            [
                {"role": "system", "content": _REFLECT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"研究主题：{topic}\n"
                        f"阈值：相关度 ≥ {min_score} 才算达标\n"
                        f"本轮候选 {len(candidates)} 条，达标 {passed} 条，已下载入库 {downloaded} 篇\n"
                        f"上一轮用过的检索词（不要重复）：{used_queries}\n\n"
                        f"候选（按相关度降序）：\n{_digest(candidates)}"
                    ),
                },
            ],
            role=Role.REVIEWER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("探索评审失败，退化为确定性判定: {}", exc)
        return fallback


# ==================================================================== 下载
async def _download(cand: Candidate) -> tuple[bool, int | None, str, bool]:
    """下载并入库一篇。返回 `(是否成功, paper_id, 说明, 是否新入库)`。

    复用上传端点里那条链路（`app/api/v1/papers.py::_ingest_arxiv`）而不是重写一遍：
    它已经处理了"先查库避免重复下载几十 MB"、"内容哈希去重"、"先 commit 再排后台解析"
    这三件容易做错的事。代价是下载必须**串行** —— 那里用的是一条会话，
    并发跑同一份 AsyncSession 是不合法的，而 arXiv 本身也不欢迎并发抓取。
    """
    from app.api.v1.papers import _ingest_arxiv  # noqa: PLC2701 - 复用上传端点的同一条链路
    from app.core.errors import AppError
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            result = await _ingest_arxiv(session, str(cand.get("arxiv_id") or ""), cand.get("title", ""), True)
    except AppError as exc:
        return False, None, exc.message, False
    except Exception as exc:  # noqa: BLE001 - 单篇失败不该中断整轮
        logger.warning("探索下载失败 arxiv_id={}: {}", cand.get("arxiv_id"), exc)
        return False, None, f"{type(exc).__name__}: {exc}", False

    paper_id = result.paper.id
    if result.index_started:
        return True, paper_id, "已下载并排入后台解析", True
    return True, paper_id, "库中已有（复用既有记录）", False


# ==================================================================== 推荐
def build_recommendations(
    candidates: list[Candidate],
    *,
    min_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    """达标候选 → 推荐列表。已下载的排在前面（它们才是可点的）。"""
    passed = [c for c in candidates if (c.get("score") or 0.0) >= min_score]
    passed.sort(key=lambda c: (bool(c.get("downloaded")), c.get("score") or 0.0), reverse=True)

    out: list[dict[str, Any]] = []
    for c in passed[:limit]:
        downloaded = bool(c.get("downloaded"))
        note = c.get("note", "")
        if not downloaded and not note:
            note = "无 arXiv 全文，仅推荐" if c.get("source") == "semantic_scholar" else "未下载"
        out.append(
            {
                "arxiv_id": c.get("arxiv_id") or None,
                "paper_id": c.get("paper_id_int"),
                "title": c.get("title", ""),
                "authors": c.get("authors", []),
                "year": c.get("year"),
                "venue": c.get("venue", ""),
                "url": c.get("url", ""),
                "source": c.get("source", ""),
                "score": c.get("score", 0.0),
                "citation_count": c.get("citation_count", 0),
                "abstract": (c.get("abstract") or "")[:1200],
                "downloaded": downloaded,
                "note": note,
            }
        )
    return out


# ==================================================================== 主闭环
async def explore(
    topic: str,
    *,
    max_papers: int | None = None,
    min_score: float | None = None,
    max_rounds: int | None = None,
) -> AsyncIterator[ExploreEvent]:
    """跑完一条探索闭环，逐步 yield 轨迹事件。

    调用方负责收尾：
    - `app/api/v1/tools.py` 转成 SSE 帧，并在异常时补 `error` 帧；
    - `app/workers/scheduler.py` 只排空它并取 `done` 载荷。
    """
    topic = topic.strip()
    ceiling = _clamp(max_papers, settings.EXPLORE_MAX_PAPERS, 1, 20)
    floor = max(1, min(settings.EXPLORE_MIN_PAPERS, ceiling))
    threshold = float(settings.EXPLORE_MIN_SCORE if min_score is None else min_score)
    rounds_cap = _clamp(max_rounds, settings.EXPLORE_MAX_ROUNDS, 1, 5)
    recall = max(1, min(int(settings.EXPLORE_RECALL_K), 50))

    started = time.perf_counter()
    seen: dict[str, Candidate] = {}
    scored: list[Candidate] = []
    used_queries: list[str] = []
    reflection: ExploreReflection | None = None
    feedback = ""
    round_no = 1

    for round_no in range(1, rounds_cap + 1):
        # ---------------------------------------------------------- 1. 规划
        yield _progress("plan", round_no=round_no)
        yield _thought("plan", f"第 {round_no} 轮：先定检索式（arXiv 走布尔语法，S2 走自然语言）。", round_no=round_no)
        plan = await plan_queries(topic, feedback=feedback, round_no=round_no)
        yield _thought(
            "plan",
            f"arXiv `{plan.arxiv_query}` / S2 `{plan.s2_query}`。"
            + (f"（{plan.reasoning}）" if plan.reasoning else ""),
            round_no=round_no,
        )
        used_queries.extend([plan.arxiv_query, plan.s2_query])

        # ---------------------------------------------------------- 2. 检索
        yield _progress("search", round_no=round_no, detail=f"每路召回 {recall} 条")
        fresh: list[Candidate] = []
        for tool_name, args in (
            ("arxiv_search", {"query": plan.arxiv_query, "max_results": recall, "sort_by": "relevance"}),
            ("semantic_scholar_search", {"query": plan.s2_query, "max_results": recall}),
        ):
            yield _action(tool_name, args, round_no=round_no)
            result = await call_tool(tool_name, args)
            if isinstance(result, str):
                # `call_tool` 把工具失败降级成字符串回喂给 LLM；这里如实报成 ok=False，
                # 但**不中断** —— 一路挂了另一路还在（S2 没有 key 时常常如此）。
                yield _observation(tool_name, ok=False, text=result[:400])
                continue

            items = candidates_of(tool_name, result)
            new_items = [c for c in items if c["key"] not in seen]
            for c in new_items:
                seen[c["key"]] = c
            fresh.extend(new_items)
            yield _observation(
                tool_name,
                ok=True,
                n=len(items),
                new=len(new_items),
                text=f"召回 {len(items)} 条，其中 {len(new_items)} 条是新的。"
                + ("示例：" + "；".join(c["title"][:60] for c in new_items[:3]) if new_items else ""),
            )

        # ---------------------------------------------------------- 3. 评分
        yield _progress("score", round_no=round_no, detail=f"对 {len(fresh)} 条新候选打分")
        yield _observation(
            "score",
            ok=True,
            n=len(fresh),
            text=f"用 bge-m3 稠密向量算与主题的余弦相似度（共 {len(seen)} 条候选，本轮新增 {len(fresh)} 条）。",
        )
        await score_candidates(topic, fresh)
        scored.extend(fresh)

        passed = [c for c in scored if (c.get("score") or 0.0) >= threshold]
        yield _observation(
            "score",
            ok=True,
            n=len(passed),
            top=max((c.get("score") or 0.0) for c in scored) if scored else 0.0,
            text=f"达标 {len(passed)}/{len(scored)} 条（阈值 {threshold}）。",
        )

        # ---------------------------------------------------------- 4. 下载
        downloadable = sorted(
            (c for c in passed if c.get("arxiv_id") and not c.get("downloaded")),
            key=lambda c: c.get("score") or 0.0,
            reverse=True,
        )
        quota = max(0, ceiling - sum(1 for c in scored if c.get("downloaded")))
        todo = downloadable[:quota]
        yield _progress("download", round_no=round_no, detail=f"本轮下载 {len(todo)} 篇")

        for cand in todo:
            yield _action("arxiv_fetch", {"arxiv_id": cand["arxiv_id"]}, round_no=round_no)
            ok, paper_id, note, is_new = await _download(cand)
            cand["downloaded"] = ok
            cand["is_new"] = is_new
            cand["note"] = note
            if ok and paper_id is not None:
                cand["paper_id_int"] = paper_id
            yield _observation(
                "arxiv_fetch",
                ok=ok,
                paper_id=paper_id,
                new=is_new,
                title=cand.get("title", "")[:120],
                text=f"{cand.get('title', '')[:70]} —— {note}",
            )
        if not todo:
            yield _observation(
                "arxiv_fetch",
                ok=True,
                n=0,
                text="本轮没有需要新下载的达标论文（要么已入库，要么候选项没有 arXiv 全文）。",
            )

        downloaded = sum(1 for c in scored if c.get("downloaded"))

        # ---------------------------------------------------------- 5. 反思
        yield _progress("reflect", round_no=round_no)
        yield _thought("reflect", "评审本轮结果：够不够覆盖主题、还要不要再搜。", round_no=round_no)
        reflection = await reflect(topic, scored, downloaded=downloaded, min_score=threshold, used_queries=used_queries)
        yield _observation(
            "reflect",
            ok=True,
            coverage=reflection.coverage,
            relevance=reflection.relevance,
            overall=reflection.overall,
            verdict=reflection.verdict,
            text=(
                f"覆盖度 {reflection.coverage:.2f} / 相关度 {reflection.relevance:.2f}"
                f" → {reflection.verdict}。{reflection.critique}"
            ),
        )

        # ---------------------------------------------------------- 6. 停 / 重规划
        # 硬规则优先于模型的 verdict：篇数到顶就收（再搜也是重复），
        # 低于硬下限就一定要再搜一轮（不能因为模型说 accept 就交不满 5 篇的结果）。
        if downloaded >= ceiling:
            logger.info("探索达标收工 topic={!r} 轮次={} 下载={}", topic, round_no, downloaded)
            break
        if round_no >= rounds_cap:
            break
        if downloaded >= floor and reflection.verdict == "accept":
            break

        feedback = "；".join(reflection.queries) or reflection.critique
        suggestions = "、".join(f"`{q}`" for q in reflection.queries[:3]) or "（模型未给出，沿用评审意见）"
        yield _thought(
            "replan",
            f"本轮达标入库 {downloaded} 篇（目标 {ceiling}，硬下限 {floor}），换检索词再来：{suggestions}",
            round_no=round_no,
        )

    # -------------------------------------------------------------- 推荐
    yield _progress("recommend", round_no=round_no, detail="生成推荐列表")
    passed = [c for c in scored if (c.get("score") or 0.0) >= threshold]
    recommendations = build_recommendations(scored, min_score=threshold, limit=max(10, ceiling))
    for item in recommendations:
        yield ("recommend", item)

    downloaded = sum(1 for c in scored if c.get("downloaded"))
    # `new` 是"本次新入库"的篇数（区别于 downloaded：库中已有的记录也算 downloaded）。
    # 订阅通知看的正是它 —— 少写这个键会让 `record_run` 永远把角标记成 0。
    fresh_count = sum(1 for c in scored if c.get("is_new"))
    yield _observation(
        "recommend",
        ok=True,
        n=len(recommendations),
        text=f"推荐 {len(recommendations)} 篇（阈值 {threshold}），其中 {downloaded} 篇已入库（{fresh_count} 篇为本次新增）。",
    )

    yield (
        "done",
        {
            "topic": topic,
            "rounds": round_no,
            "searched": len(seen),
            "scored": len(scored),
            "downloaded": downloaded,
            "new": fresh_count,
            "passed": len(passed),
            "min_score": threshold,
            "quality": reflection.overall if reflection else 0.0,
            "verdict": reflection.verdict if reflection else "",
            "critique": reflection.critique if reflection else "",
            "recommendations": recommendations,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    )


def _clamp(value: int | None, default: int, low: int, high: int) -> int:
    if value is None:
        value = default
    return max(low, min(int(value), high))


__all__ = [
    "STAGES",
    "Candidate",
    "ExploreEvent",
    "ExploreReflection",
    "SearchPlan",
    "build_recommendations",
    "candidates_of",
    "explore",
    "plan_queries",
    "reflect",
    "score_candidates",
]
