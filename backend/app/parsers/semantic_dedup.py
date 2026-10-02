"""语义级去重：版本谱系（arXiv v1 vs v7）与跨库重复（同一篇论文的不同来源）。

两种"像重复"要分清楚，混起来会把版本历史弄丢
--------------------------------------------
| 场景 | 判据 | 动作 |
|---|---|---|
| 同一篇 arXiv 论文的不同版本 | **`arxiv_id` 相同、`version` 不同** | 记进 `paper_versions` 谱系，**两版都留**（"这篇改了什么"要能追溯） |
| 同一篇论文从不同来源进来（arXiv 镜像 / 出版社站 / 第三方库） | 没有可比的 arXiv 编号，但摘要向量余弦 **≥ 0.95** | 合并：`duplicate_of` 指向已有那一篇，不重复写向量 |

**顺序不能反**：版本判据必须在相似度之前。v1 与 v7 的摘要向量相似度也在 0.97
以上，先跑相似度闸门的话 v7 会被当成"跨库重复"合并掉，`paper_versions` 永远
长不出第二行 —— 版本谱系整个失效，而且不会有任何报错。

语义指纹（规格里的 `md5(title_emb + abstract_emb + authors)`）在这里只当
**精确同一份**的判据与日志证据，不当决策依据：向量本身就能算余弦，
再哈希一遍不增加分辨力。指纹里嵌 `authors` 才有意义 —— 同标题不同作者
是两个东西，而向量看不出这一点。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings

#: 版本号形如 v7 / V7 / ver.7；没有就当 v1（arXiv 首版）
_VERSION_RE = re.compile(r"(?:v|ver\.?|version\s*)(\d{1,3})\s*$", re.I)
#: arXiv 新式编号 2401.12345（可带 vN）；旧式 cs/0701001、math.GT/0309136
_NEW_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.I)
_OLD_ID_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", re.I)

DISTINCT = "distinct"
DUPLICATE = "duplicate"
NEW_VERSION = "new_version"


@dataclass(slots=True)
class DedupVerdict:
    """一次去重判定的结论。`canonical_paper_id` 是"该归到哪一篇"。"""

    kind: str = DISTINCT
    canonical_paper_id: int | None = None
    similarity: float = 0.0
    reason: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.kind == DUPLICATE

    @property
    def is_new_version(self) -> bool:
        return self.kind == NEW_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "canonical_paper_id": self.canonical_paper_id,
            "similarity": round(float(self.similarity), 4),
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 标识归一化
def normalize_arxiv_id(raw: str | None) -> tuple[str | None, str | None]:
    """从任意写法里抽出 `(arxiv_id, version)`。

    认这些写法：`https://arxiv.org/abs/2401.12345v7`、`arxiv.org/pdf/2401.12345v7.pdf`、
    `arXiv:2401.12345v7`、裸编号、旧式 `cs/0701001v2`。版本缺省为 None
    （**不是 "v1"** —— "没写版本"与"写了 v1"在比对时含义不同，交给调用方决定）。
    """
    if not raw:
        return None, None
    text = str(raw).strip()
    # 去掉 URL 尾巴与扩展名，先剥 .pdf 再找编号，否则 `12345v7.pdf` 会匹配不上
    text = re.sub(r"\.pdf$", "", text, flags=re.I).rstrip("/")
    for pattern in (_NEW_ID_RE, _OLD_ID_RE):
        match = pattern.search(text)
        if match:
            return match.group(1), (match.group(2) or "").lower() or None
    return None, None


def canonical_version(version: str | None) -> str:
    """归一化版本号，缺省按 v1。只用于**比较**，不用于落库。"""
    if not version:
        return "v1"
    text = str(version).strip()
    match = _VERSION_RE.search(text)
    if match:
        return f"v{int(match.group(1))}"
    if text.isdigit():  # 库里存过裸数字，别把 "7" 与 "v7" 判成两版
        return f"v{int(text)}"
    return text.lower()


# ---------------------------------------------------------------- 语义指纹
def author_names(authors: Any) -> str:
    """把 authors 字段摊平成一行名字。兼容 `[{name}]` / `["Ada"]` / 单字符串。"""
    if not authors:
        return ""
    if isinstance(authors, str):
        return authors.strip()
    names: list[str] = []
    for item in authors if isinstance(authors, Iterable) else [authors]:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("full_name") or "").strip())
        else:
            names.append(str(item).strip())
    return ", ".join(n for n in names if n)


def _quantize(vector: Sequence[float], digits: int = 3) -> str:
    """向量量化成字符串。**必须量化**：浮点末位在不同批次/设备上会抖，
    不量化的话同一篇论文会算出两个指纹，指纹就失去了"同一份"的判据意义。"""
    return ",".join(f"{float(x):.{digits}f}" for x in vector)


def semantic_fingerprint(
    title: str | None,
    abstract: str | None,
    authors: Any = None,
    *,
    dense_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> str:
    """语义指纹：`md5(title_emb + abstract_emb + authors)`。

    有 `dense_fn`（嵌入模型）时按规格走向量；没有时退化为规范化文本指纹 ——
    后者措辞一变就变，只能判"完全同一份"。两条路都**带作者**：
    同标题不同作者是两个东西，向量分辨不出来。
    """
    names = author_names(authors)
    if dense_fn is not None:
        try:
            vectors = dense_fn([title or "", abstract or ""])
        except Exception as exc:  # noqa: BLE001 - 指纹算不出来不该中断入库
            logger.warning("指纹嵌入失败，退化为文本指纹: {}", exc)
        else:
            if len(vectors) == 2:
                return hashlib.md5(f"{_quantize(vectors[0])}|{_quantize(vectors[1])}|{names}".encode()).hexdigest()
    payload = re.sub(r"\s+", " ", f"{title or ''}|{abstract or ''}|{names}".strip().lower())
    return hashlib.md5(payload.encode()).hexdigest()


# ---------------------------------------------------------------- 判定
def classify(
    candidate: dict[str, Any],
    hits: list[dict[str, Any]],
    *,
    threshold: float | None = None,
) -> DedupVerdict:
    """按"同 arXiv 编号优先，其次摘要向量相似度"给出结论。

    `candidate` = `{paper_id, arxiv_id, version}`，`hits` = 检索到的候选
    `[{paper_id, arxiv_id, version, score}]`（score 为摘要向量余弦）。
    纯函数 —— 不碰数据库、不碰 Milvus，因此可以拿合成数据把全部分支钉死。
    """
    limit = settings.SEMANTIC_DEDUP_THRESHOLD if threshold is None else threshold
    self_id = _as_int(candidate.get("paper_id"))
    cand_arxiv, _ = normalize_arxiv_id(candidate.get("arxiv_id"))
    cand_version = canonical_version(candidate.get("version"))

    same_arxiv: list[tuple[float, int, dict[str, Any]]] = []
    similar: list[tuple[float, int, dict[str, Any]]] = []
    top = 0.0
    for hit in hits or []:
        paper_id = _as_int(hit.get("paper_id"))
        if paper_id is None or (self_id is not None and paper_id == self_id):
            continue
        score = float(hit.get("score") or 0.0)
        top = max(top, score)
        hit_arxiv, _ = normalize_arxiv_id(hit.get("arxiv_id"))
        if cand_arxiv and hit_arxiv and hit_arxiv == cand_arxiv:
            same_arxiv.append((score, paper_id, hit))
        elif score >= limit:
            similar.append((score, paper_id, hit))

    if same_arxiv:
        score, paper_id, hit = max(same_arxiv, key=lambda item: item[0])
        hit_version = canonical_version(hit.get("version"))
        if hit_version == cand_version:
            return DedupVerdict(DUPLICATE, paper_id, score, f"同 arXiv 编号 {cand_arxiv} 同版本 {cand_version}")
        return DedupVerdict(
            NEW_VERSION, paper_id, score, f"同 arXiv 编号 {cand_arxiv}，库中已有 {hit_version}，本次 {cand_version}"
        )
    if similar:
        score, paper_id, _hit = max(similar, key=lambda item: item[0])
        return DedupVerdict(DUPLICATE, paper_id, score, f"摘要向量相似度 {score:.4f} ≥ {limit}")

    reason = "未命中版本谱系，相似度也未达阈值" if hits else "库中无候选"
    return DedupVerdict(DISTINCT, None, top, reason)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 与库交互
async def _search_hits(dense: list[float], *, limit: int) -> list[dict[str, Any]]:
    """取摘要向量最近的 N 篇，并回表补上 arxiv_id / version（判定版本要用）。"""
    from sqlalchemy import select

    from app.db.milvus import asearch_summaries
    from app.db.session import session_scope
    from app.models import Paper

    found = await asearch_summaries(dense, limit=limit)
    hits = [{"paper_id": int(h.paper_id), "score": float(h.score)} for h in found if h.paper_id]
    if not hits:
        return []
    ids = [h["paper_id"] for h in hits]
    async with session_scope() as session:
        rows = (await session.execute(select(Paper.id, Paper.arxiv_id, Paper.version).where(Paper.id.in_(ids)))).all()
    meta = {int(pid): (arxiv, version) for pid, arxiv, version in rows}
    for hit in hits:
        arxiv, version = meta.get(hit["paper_id"], (None, None))
        hit["arxiv_id"] = arxiv
        hit["version"] = version
    return hits


def resolve_duplicate(
    paper_id: int | None,
    dense: list[float] | None,
    *,
    arxiv_id: str | None = None,
    version: str | None = None,
    hits: list[dict[str, Any]] | None = None,
    threshold: float | None = None,
) -> DedupVerdict:
    """同步入口（流水线在 `asyncio.to_thread` 里跑，没有运行中的事件循环）。

    `hits` 可注入 —— 单测不需要 Milvus，也不需要真库。
    没有摘要向量时返回 "distinct"：不做判断本身比猜一个更安全。
    """
    if hits is None:
        if not dense:
            return DedupVerdict(DISTINCT, None, 0.0, "没有摘要向量，跳过去重判定")
        try:
            hits = asyncio.run(_search_hits(dense, limit=settings.SEMANTIC_DEDUP_TOP_N))
        except Exception as exc:  # noqa: BLE001 - Milvus 不可用不该让整篇论文失败
            logger.warning("语义去重检索失败，跳过: {}", exc)
            return DedupVerdict(DISTINCT, None, 0.0, f"检索失败: {type(exc).__name__}")
    verdict = classify({"paper_id": paper_id, "arxiv_id": arxiv_id, "version": version}, hits, threshold=threshold)
    logger.info(
        "去重判定 paper_id={} kind={} canonical={} sim={:.4f} ({})",
        paper_id,
        verdict.kind,
        verdict.canonical_paper_id,
        verdict.similarity,
        verdict.reason,
    )
    return verdict


__all__ = [
    "DISTINCT",
    "DUPLICATE",
    "NEW_VERSION",
    "DedupVerdict",
    "author_names",
    "canonical_version",
    "classify",
    "normalize_arxiv_id",
    "resolve_duplicate",
    "semantic_fingerprint",
]
