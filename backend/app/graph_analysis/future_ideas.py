"""未来方向 —— 从综述推 3-5 个还没被解决的问题，并**机械过滤**掉假的。

模型提"未来方向"最常见的失败模式不是想不出，而是想出了一堆**已经做完了**的：
"用对比学习改进表示" / "引入注意力机制" —— 这些在已成型的领域里早就被解决，
只是模型不知道。所以本模块的核心不是提示词，是 `filter_ideas` 这道闸门：

| 过滤规则 | 命中依据 | 出处 |
|---|---|---|
| 已被核心论文解决 | 与核心论文的贡献句高度重合 | 综述的 `core_papers[].contribution` |
| 已被后续工作 follow-up | 与调用方给出的"已解决旧 idea"列表高度重合 | 请求参数 `resolved_ideas` |
| 引用了图外的论文 | `based_on` 里有不存在的 paper_id | 引文图节点集合 |

相似度用**词元重合度（重叠系数 / containment）**，不引语义模型：这里判的是
"这个方向是不是已经被那项工作做掉了"，即"短的那段文本是否被长的包含"。
用 Jaccard 会失真 —— 一条核心论文的贡献句往往比一个方向描述长得多，
两者即便讲的是同一件事，Jaccard 也会被分母压到 0.5 以下而漏判。
重叠系数对"包含"这件事是对的量纲，而且它是确定的：同一份综述跑两次，
过滤结果必须一模一样，否则用户没法解释"为什么昨天那个方向还在，今天没了"。
`filter_ideas` 是纯函数，全部规则可以在单测里逐条钉死。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import logger
from app.llm.client import LLMClient, Message, Role, get_llm
from app.llm.structured import complete_structured

# 重叠系数阈值：0.6 落在"同一件事的不同说法"（通常 0.75+）与
# "同一个子领域的两个不同问题"（通常 0.3 以下）之间。
SIMILARITY_THRESHOLD = 0.6

_LATIN_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


class FutureIdea(BaseModel):
    """一个候选未来方向。"""

    title: str = Field(description="方向名称（8-20 字，具体到一个可做的事，不要「更深入的研究」）")
    rationale: str = Field(description="为什么它现在还没被解决（2-3 句，要指出卡在哪）")
    grounded_in: list[str] = Field(default_factory=list, description="支撑它的开放问题原文（取自综述的 open_problems）")
    based_on: list[str] = Field(default_factory=list, description="相关论文 id，必须取自给定清单")
    from_open_problems: bool = Field(default=False, description="是否直接源自综述里反复提到的开放问题")


class IdeaList(BaseModel):
    ideas: list[FutureIdea] = Field(default_factory=list)


def _tokens(text: str) -> set[str]:
    """切成可比较的词元：拉丁词按空格切，中文按**二元组**切。

    中文没有空格，按字切会让"表示学习"和"学习表示"完全重合（其实是一回事，
    没问题），但也会让"注意力机制"和"注意力分散"高度重合（不是一回事）。
    二元组是这两者之间的折中，够用。
    """
    text = (text or "").lower()
    out: set[str] = set(_LATIN_RE.findall(text))
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i : i + 2] for i in range(len(run) - 1))
    return out


def similarity(a: str, b: str) -> float:
    """**重叠系数**：`|A ∩ B| / min(|A|, |B|)` —— "短的那段是否被长的包含"。

    任一侧没有词元 → 0（不确定就不拦，宁可放过也不要误杀）。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def already_done(idea: str, known: list[str], *, threshold: float = SIMILARITY_THRESHOLD) -> str | None:
    """这个方向是不是已经在 `known` 里被做掉了？命中则返回命中的那条。"""
    for item in known:
        if item and similarity(idea, item) >= threshold:
            return item
    return None


def filter_ideas(
    ideas: list[FutureIdea],
    *,
    solved: list[str] | None = None,
    resolved: list[str] | None = None,
    open_problems: list[str] | None = None,
    valid_ids: set[str] | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[list[FutureIdea], list[dict[str, str]]]:
    """过滤 + 标注。返回 `(保留的方向, 丢弃的原因列表)`。

    `solved` / `resolved` 是"已经做完"的两份清单（核心论文贡献 / 旧的 idea），
    `open_problems` 只用来**标注**（`from_open_problems`）与排序，不用来过滤 ——
    来自开放问题的方向是我们要的，不是要拦的。
    """
    solved = [s for s in (solved or []) if s]
    resolved = [r for r in (resolved or []) if r]
    open_problems = [o for o in (open_problems or []) if o]
    valid_ids = valid_ids or set()

    kept: list[FutureIdea] = []
    dropped: list[dict[str, str]] = []

    for idea in ideas:
        probe = f"{idea.title}。{idea.rationale}"
        if hit := already_done(probe, solved, threshold=threshold):
            dropped.append({"idea": idea.title, "reason": f"已被核心论文解决：{hit[:80]}"})
            continue
        if hit := already_done(probe, resolved, threshold=threshold):
            dropped.append({"idea": idea.title, "reason": f"与已有方向重复：{hit[:80]}"})
            continue

        ids = list(idea.based_on)
        if valid_ids:
            ids = [i for i in ids if str(i) in valid_ids]
            if not ids:
                dropped.append({"idea": idea.title, "reason": "没有可溯源的论文支撑（引文图里不存在）"})
                continue

        grounded = bool(open_problems and already_done(probe, open_problems, threshold=0.25) is not None)
        kept.append(idea.model_copy(update={"based_on": ids, "from_open_problems": grounded}))

    # 由开放问题直接长出来的排前面：它们才是用户真正想看的"缺口"
    kept.sort(key=lambda i: not i.from_open_problems)
    return kept, dropped


def _prompt(survey_digest: str, resolved: list[str], count: int) -> str:
    known = "\n".join(f"- {r}" for r in resolved) if resolved else "（无）"
    return (
        f"下面是某个研究方向的综述要点。请提出 **{count}** 个**尚未解决**的未来研究方向。\n\n"
        "硬约束：\n"
        "1. 每个方向都必须能指到具体的论文 id（based_on），id 只能取自清单。\n"
        "2. **不要提已经被解决的问题**。下面「已知已解决」清单里的内容一律不要出现，"
        "换个说法也不行：\n" + known + "\n"
        "3. 优先从综述里的 open_problems 长出来 —— 那是原论文自己承认的缺口，"
        "比你凭空想的方向扎实得多。\n"
        "4. 方向要具体到「做什么、为什么现在还做不了」，不要「更深入的研究」这类空话。\n"
        "5. 不要重复：给的方向之间也要有明显区别。\n\n"
        f"【综述要点】\n{survey_digest}"
    )


def digest_of(survey: Any) -> str:
    """把 `Survey`（或其 dict 形态）折成提示词用的紧凑文本。"""
    data = survey.model_dump() if hasattr(survey, "model_dump") else dict(survey or {})
    parts = [
        f"标题：{data.get('title', '')}",
        f"总览：{data.get('overview', '')}",
        "时间线：" + "；".join(f"{e.get('year')} {e.get('milestone')}" for e in data.get("timeline") or []),
        "社区方法："
        + "；".join(
            f"#{c.get('community')} {c.get('label')}——{c.get('method')}" for c in data.get("communities") or []
        ),
        "核心论文贡献："
        + "；".join(f"{c.get('paper_id')} {c.get('contribution')}" for c in data.get("core_papers") or []),
        "开放问题：" + "；".join(data.get("open_problems") or []),
        "允许引用的论文 id："
        + ", ".join(
            sorted(
                {str(i) for e in data.get("timeline") or [] for i in e.get("paper_ids") or []}
                | {str(c.get("paper_id")) for c in data.get("core_papers") or []}
            )
        ),
    ]
    return "\n".join(p for p in parts if p.split("：", 1)[-1].strip())


async def suggest_future_directions(
    survey: Any,
    *,
    resolved_ideas: list[str] | None = None,
    valid_ids: set[str] | None = None,
    count: int = 5,
    llm: LLMClient | None = None,
    temperature: float = 0.5,
) -> dict[str, Any]:
    """综述 → 3-5 个未来方向（已过滤）。

    返回 `{"ideas": [...], "dropped": [{"idea","reason"}], "count": n, "note": str}`。
    少于 3 条时**不自动补足**：见过太多"凑数型方向"，一条靠谱的比三条水的好。
    真需要更多就让用户放宽 `resolved_ideas`。
    """
    data = survey.model_dump() if hasattr(survey, "model_dump") else dict(survey or {})
    solved = [c.get("contribution", "") for c in data.get("core_papers") or []]
    open_problems = [str(o) for o in data.get("open_problems") or []]
    count = max(3, min(int(count), 5))

    messages: list[Message] = [
        {"role": "user", "content": _prompt(digest_of(data), [r for r in (resolved_ideas or []) if r], count)}
    ]
    result = await complete_structured(
        IdeaList, messages, role=Role.PLANNER, llm=llm or get_llm(), temperature=temperature
    )

    kept, dropped = filter_ideas(
        result.ideas,
        solved=solved,
        resolved=resolved_ideas or [],
        open_problems=open_problems,
        valid_ids=valid_ids,
    )
    note = ""
    if len(kept) < 3:
        note = f"过滤后只剩 {len(kept)} 个方向（原 {len(result.ideas)} 个），放宽已解决清单可以获得更多候选。"
        logger.warning(note)
    return {"ideas": [i.model_dump() for i in kept], "dropped": dropped, "count": len(kept), "note": note}


__all__ = [
    "SIMILARITY_THRESHOLD",
    "FutureIdea",
    "IdeaList",
    "already_done",
    "digest_of",
    "filter_ideas",
    "similarity",
    "suggest_future_directions",
]
