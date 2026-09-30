"""源头溯源引擎（抗幻觉的落点）。

四道检查，由便宜到昂贵，前一道不过就不必做后一道：

1. **白名单校验**（字符串级，零成本，不可省）
   生成文本里出现的每个 `[n]` 必须落在检索上下文的编号范围内。
   出现越界编号就是**幻觉引用**，直接判定失败 —— 这一条最便宜也最有效，
   绝大多数"编造参考文献"的案例死在这里。

2. **声明-证据切分**
   把答案按句切开，每句带上它引用的 chunk。没有引用任何 chunk 的句子
   记为"无据声明"（uncited）。

3. **蕴含验证（NLI）**
   逐句判断"该句是否被它引用的语料片段支持"。默认用词法蕴含代理分
   （token 覆盖 + 数字一致性）；装上 NLI 模型后自动升级为真正的蕴含判断。
   数字/单位不一致是学术场景最危险的幻觉，所以**对数字单独加一条硬规则**。

4. **Grounding Ratio 闸门**
   ratio = 有据且被支持的句子数 / 总句子数。低于 `GROUNDING_MIN_RATIO`（默认 0.8）
   就不允许当作可信答案返回 —— 由调用方决定是"标注低置信"还是"拒答"。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# [1] / [1,2] / [1-3] / 【1】 都吃掉
_MARKER_RE = re.compile(r"[\[【]\s*(\d+(?:\s*[-–,，]\s*\d+)*)\s*[\]】]")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+(?=[A-Z])")


def extract_markers(text: str) -> list[int]:
    """抽出所有引用编号（展开 [1-3] 这类区间），保持出现顺序。"""
    out: list[int] = []
    for raw in _MARKER_RE.findall(text or ""):
        parts = re.split(r"\s*[,，]\s*", raw)
        for part in parts:
            rng = re.match(r"(\d+)\s*[-–]\s*(\d+)", part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                if lo <= hi and hi - lo <= 50:  # 防御离谱区间
                    out.extend(range(lo, hi + 1))
            elif part.strip().isdigit():
                out.append(int(part))
    return out


def strip_markers(text: str) -> str:
    return _MARKER_RE.sub("", text or "").strip()


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句。切得太碎没关系，溯源只需要粒度够细。"""
    raw = (text or "").strip()
    if not raw:
        return []
    return [s.strip() for s in _SENT_SPLIT_RE.split(raw) if s and s.strip()]


def tokenize(text: str) -> set[str]:
    from app.rag.crag import tokenize as _t

    return _t(text)


def numbers_in(text: str) -> set[str]:
    return set(_NUM_RE.findall(text or ""))


def lexical_entailment(claim: str, evidence: str) -> float:
    """词法蕴含代理分。

    做法：claim 的实词有多少比例出现在 evidence 里。学术场景下，
    "结论句"的实词（方法名、指标名、数据集名）通常在原文出现过，所以这个代理
    与真实蕴含的相关性不低。**明确它只是代理**：它无法识别否定、
    无法处理同义替换，因此阈值取得偏保守（0.6）。
    """
    c_tokens = tokenize(claim)
    if not c_tokens:
        return 0.0
    e_tokens = tokenize(evidence)
    if not e_tokens:
        return 0.0
    return len(c_tokens & e_tokens) / len(c_tokens)


def numbers_consistent(claim: str, evidence: str) -> bool:
    """claim 里出现的数字必须在 evidence 里出现。

    这是硬规则：**"提升了 12%" 与 "提升了 21%" 在词法覆盖上几乎一样**，
    但结论相反。数字是学术文本里最容易被幻觉篡改、也最容易机械校验的部分，
    所以不交给概率模型判断。
    """
    nums = numbers_in(strip_markers(claim))
    if not nums:
        return True
    ev = numbers_in(evidence)
    return nums.issubset(ev)


@dataclass(slots=True)
class Citation:
    marker: int  # 正文里的编号 [n]
    chunk_id: str  # 对应的 chunk
    paper_id: str = ""
    section: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    nli_score: float = 0.0
    supported: bool = False
    quote: str = ""  # 被引用的原文片段（截断）

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "section": self.section,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "nli_score": round(self.nli_score, 4),
            "supported": self.supported,
            "quote": self.quote,
        }


@dataclass(slots=True)
class TraceReport:
    citations: list[Citation] = field(default_factory=list)
    grounding_ratio: float = 0.0
    sentences_total: int = 0
    sentences_supported: int = 0
    unsupported_claims: list[str] = field(default_factory=list)
    uncited_claims: list[str] = field(default_factory=list)
    phantom_markers: list[int] = field(default_factory=list)  # 越界编号 = 幻觉引用
    number_mismatches: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.grounding_ratio >= settings.GROUNDING_MIN_RATIO and not self.phantom_markers

    def to_dict(self) -> dict[str, Any]:
        return {
            "grounding_ratio": round(self.grounding_ratio, 4),
            "sentences_total": self.sentences_total,
            "sentences_supported": self.sentences_supported,
            "passed": self.passed,
            "phantom_markers": self.phantom_markers,
            "unsupported_claims": self.unsupported_claims[:10],
            "uncited_claims": self.uncited_claims[:10],
            "number_mismatches": self.number_mismatches[:10],
            "citations": [c.to_dict() for c in self.citations],
        }


class SourceTracer:
    """把生成结果对齐回检索上下文。"""

    def __init__(
        self,
        *,
        nli_threshold: float = 0.6,
        enable_nli: bool = True,
        nli_model: str = "cross-encoder/nli-deberta-v3-base",
    ) -> None:
        self.nli_threshold = nli_threshold
        self._enable_nli = enable_nli
        self._nli_model = nli_model
        self._nli: Any = None
        # enable_nli=False 直接标记"已检查完毕、没有模型"，让下游一律走词法代理
        self._nli_checked = not enable_nli
        self._lock = threading.Lock()

    @property
    def nli(self) -> Any | None:
        """可选的真 NLI 模型（cross-encoder）。没装/加载失败则返回 None，用词法代理。

        `enable_nli=False` 是给**离线场景**（单测、CI、无外网部署）用的硬开关，
        与"装了但加载失败"不同：后者会先真去拉一次权重，网络不通时表现为长时间
        挂起而不是抛错，`except` 兜不住。
        """
        if self._nli_checked:
            return self._nli
        with self._lock:
            if not self._nli_checked:
                self._nli_checked = True
                try:
                    # 下载超时必须有：huggingface_hub 默认无下载超时，网络被墙/代理
                    # 半通时这里会**永久挂住**。设了上限，拉不到才会变成异常 → 降级。
                    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "8")
                    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "8")

                    from sentence_transformers import CrossEncoder

                    self._nli = CrossEncoder(self._nli_model, max_length=512)
                    logger.info("已加载 NLI 模型，蕴含验证升级为模型判断")
                except Exception as exc:  # noqa: BLE001
                    logger.info("NLI 模型不可用，使用词法蕴含代理：{}", exc)
                    self._nli = None
        return self._nli

    def entail(self, claim: str, evidence: str) -> float:
        """返回 claim 被 evidence 支持的程度 [0,1]。"""
        if self.nli is not None:
            try:
                # 标签顺序按模型 config：contradiction, entailment, neutral
                scores = self.nli.predict([(evidence, claim)], apply_softmax=True)[0]
                return float(scores[1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("NLI 推理失败，退回词法代理：{}", exc)
        return lexical_entailment(claim, evidence)

    def trace(self, answer: str, chunks: Sequence[Any]) -> TraceReport:
        """核心入口。chunks 的顺序决定编号：第 i 个对应正文里的 [i]。"""
        report = TraceReport()
        if not chunks:
            report.uncited_claims = split_sentences(answer)
            report.sentences_total = len(report.uncited_claims)
            return report

        by_marker = dict(enumerate(chunks, start=1))

        # ---- 1. 白名单校验 ----
        seen_markers = extract_markers(answer)
        report.phantom_markers = sorted({m for m in seen_markers if m not in by_marker})
        if report.phantom_markers:
            logger.warning("检测到幻觉引用编号（不在检索上下文内）：{}", report.phantom_markers)

        # ---- 2/3. 逐句验证 ----
        sentences = split_sentences(answer)
        report.sentences_total = max(1, len(sentences))
        cited_markers: dict[int, Citation] = {}

        for sentence in sentences:
            markers = [m for m in extract_markers(sentence) if m in by_marker]
            if not markers:
                if len(strip_markers(sentence)) > 8:  # 忽略"综上所述"这类短衔接句
                    report.uncited_claims.append(strip_markers(sentence))
                continue

            sentence_supported = False
            for m in markers:
                chunk = by_marker[m]
                content = getattr(chunk, "content", "") or ""
                score = self.entail(strip_markers(sentence), content)
                ok = score >= self.nli_threshold and numbers_consistent(sentence, content)
                if not numbers_consistent(sentence, content):
                    report.number_mismatches.append(strip_markers(sentence)[:160])
                sentence_supported = sentence_supported or ok

                if m not in cited_markers:
                    # chunks 表用单值 page，旧结构用 page_start/page_end —— 两种都认，
                    # 免得溯源链路被"字段改名"这种纯表示层的事故打断。
                    page = getattr(chunk, "page", None) or getattr(chunk, "page_start", None)
                    page_end = getattr(chunk, "page_end", None) or page
                    cited_markers[m] = Citation(
                        marker=m,
                        chunk_id=getattr(chunk, "id", ""),
                        paper_id=getattr(chunk, "paper_id", ""),
                        section=getattr(chunk, "section", None),
                        page_start=page,
                        page_end=page_end,
                        nli_score=score,
                        supported=ok,
                        quote=content[:280],
                    )
                else:
                    # 同一编号被多次引用，取最高分
                    prev = cited_markers[m]
                    if score > prev.nli_score:
                        prev.nli_score = score
                        prev.supported = prev.supported or ok

            if sentence_supported:
                report.sentences_supported += 1
            else:
                report.unsupported_claims.append(strip_markers(sentence)[:200])

        report.citations = [cited_markers[m] for m in sorted(cited_markers)]
        report.grounding_ratio = report.sentences_supported / report.sentences_total
        return report

    def enforce(self, report: TraceReport) -> None:
        """闸门：不达标就抛异常，由调用方决定降级策略。"""
        from app.core.errors import GroundingError

        if report.phantom_markers:
            raise GroundingError(f"检测到幻觉引用编号 {report.phantom_markers}，这些编号不在检索上下文中")
        if report.grounding_ratio < settings.GROUNDING_MIN_RATIO:
            raise GroundingError(
                f"溯源率 {report.grounding_ratio:.0%} 低于阈值 {settings.GROUNDING_MIN_RATIO:.0%}，"
                f"存在 {len(report.unsupported_claims)} 条无据声明"
            )


@lru_cache
def get_tracer() -> SourceTracer:
    return SourceTracer()


def _demo() -> None:
    """自检：溯源引擎的四道检查各自能拦住什么。"""
    from dataclasses import dataclass

    @dataclass
    class Chunk:
        id: str
        content: str
        paper_id: str = "p1"
        section: str = "Method"
        page_start: int = 3
        page_end: int = 3

    chunks = [
        Chunk("c1", "Our method improves accuracy by 12% on GLUE benchmark compared to BERT."),
        Chunk("c2", "We use a sparse attention mechanism to reduce quadratic complexity."),
    ]
    tracer = SourceTracer()
    tracer._nli_checked = True  # 强制走词法代理，离线可跑

    # 1) 正常引用：命中白名单且被支持 → ratio = 1
    ok = tracer.trace("The method improves accuracy by 12% on GLUE [1].", chunks)
    assert ok.passed, ok.to_dict()
    assert ok.grounding_ratio == 1.0
    assert len(ok.phantom_markers) == 0

    # 2) 幻觉引用：编号越界，必须被拦下（这条是最关键的安全网）
    bad = tracer.trace("Attention is all you need [7].", chunks)
    assert bad.phantom_markers == [7], bad.phantom_markers
    assert not bad.passed
    try:
        tracer.enforce(bad)
    except Exception as e:
        assert "幻觉引用" in str(e), e
    else:  # pragma: no cover
        raise AssertionError("越界编号必须被 enforce 拦下")

    # 3) 数字篡改：12% → 21%，词法覆盖几乎一样但数字对不上 → 判无据
    tampered = tracer.trace("The method improves accuracy by 21% on GLUE [1].", chunks)
    assert tampered.number_mismatches, "数字不一致必须被记录"
    assert tampered.grounding_ratio < 1.0, tampered.grounding_ratio

    # 4) 无据声明：没有引用编号的句子
    uncited = tracer.trace("We use a sparse attention mechanism [2]. This is the best paper ever.", chunks)
    assert uncited.uncited_claims, uncited.uncited_claims
    assert uncited.grounding_ratio < 1.0

    # 5) 工具函数
    assert extract_markers("a [1,2] b [3-5] c") == [1, 2, 3, 4, 5]
    assert strip_markers("答案 [1] 完") == "答案  完".replace("  ", " ")
    assert numbers_in("12% and 3.5") == {"12%", "3.5"}
    assert not numbers_consistent("提升 21%", "提升了 12%")
    assert numbers_consistent("提升 12%", "提升了 12%")
    assert split_sentences("第一句。第二句！") == ["第一句。", "第二句！"]

    print("source_tracing self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _demo()
