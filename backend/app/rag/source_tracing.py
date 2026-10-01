"""源头溯源引擎（抗幻觉的落点）。

三方法融合，由便宜到昂贵，前一道不过就不必做后一道：

1. **Self-Citation Prompting**（字符串级，零成本，不可省）
   生成 Prompt 要求模型用 `[n]` 标注来源，`n` 就是检索上下文里的编号。
   这里做的是**白名单校验**：正文里出现的每个 `[n]` 必须落在上下文编号范围内。
   出现越界编号就是**幻觉引用**，直接判定失败 —— 这一条最便宜也最有效，
   绝大多数"编造参考文献"的案例死在这里。

2. **NLI-based Attribution**
   把答案按句切开，逐句判断"该句是否被它引用的语料片段蕴含"。装了
   `cross-encoder/nli-deberta-v3-base` 就用模型判 entailment / contradiction / neutral；
   没装则退化为**词法蕴含代理**（token 覆盖 + 数字一致性）。
   数字/单位不一致是学术场景最危险的幻觉，所以**对数字单独加一条硬规则**，
   不交给概率模型判断。

3. **Grounding Ratio**
   实词级的支持率：`被支持实词数 / 答案实词总数`（实词 = 去掉停用词与标点的 token）。
   低于 `GROUNDING_MIN_RATIO`（默认 0.8）就不允许当作可信答案返回 ——
   由调用方决定是"标注低置信"还是"拒答"。
   为什么按**实词**而不是按句：按句时一句 40 字的废话和一句 5 字的短句等权，
   而短句往往是"综上所述"这类衔接句，会让有据率虚高。

输出契约 `SourceTrace`
----------------------
`answer_span`（答案文本片段）/ `chunk_id` / `paper_id` / `page`（PDF 高亮定位）/
`bbox`（前端 PDF.js 精确框选）/ `confidence`（0~1）/ `attribution_method`
（`self_citation` | `nli` | `hybrid`，见 `SourceTracer._method`）。
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

# 停用词：只收高频功能词，不求语言学完备。实词率的目的是"别让虚词把分母灌满"，
# 收多了会误伤真实内容词（如"中/上"在"在表中"是虚词，在"对照组中"也算），
# 所以宁可保守 —— 少收几个词，比率依然单调可靠。
_EN_STOPWORD_TEXT = """
a an the of to in on for and or is are was were be been being with that this these those it its as
by at from we our us their they them can could may might will would shall should not no but if then
than which who whom whose when where how what also more most such using used use uses has have had
do does did all any both each other some there here so up out over into about between during after
before above below only same own too very
"""
# 中文按**字**切（tokenizer 的粒度就是字），所以这里列的是单字虚词
_ZH_STOPWORD_TEXT = (
    "的了是在和与及或而也就都还被把对从到为以之其这那有无不没很更最会能可要该让使由于中上"
    "下里外时后前些个者等且则若即并但因所此我你他它们各自"
)

STOPWORDS = frozenset(_EN_STOPWORD_TEXT.split()) | frozenset(_ZH_STOPWORD_TEXT)


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


def sentence_spans(text: str) -> list[tuple[int, int, str]]:
    """按句切分，同时给出每句在原文里的字符区间。

    一次给出两种粒度是因为两个消费方要的粒度不同：前端要高亮的是**区间**
    （`qa_history.sources[].answer_span`），溯源面板要展示的是**文本**。
    分两次切句迟早会漂成两套不一致的边界。
    """
    raw_text = text or ""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for piece in _SENT_SPLIT_RE.split(raw_text):
        if not piece:
            continue
        start = raw_text.find(piece, pos)
        if start < 0:  # split 与 find 不同步时的兜底，宁可区间差一点也不要丢句
            start = pos
        end = start + len(piece)
        pos = end
        if piece.strip():
            out.append((start, end, piece.strip()))
    return out


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句。切得太碎没关系，溯源只需要粒度够细。"""
    return [s for _, _, s in sentence_spans(text)]


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


def content_terms(text: str) -> set[str]:
    """答案里的**实词**集合：去掉引用编号、停用词与标点。

    数字不成词（`_WORD_RE` 不匹配纯数字），所以它不进实词率的分母 ——
    数字的校验由 `numbers_consistent` 那条硬规则单独负责，重复计入只会稀释信号。
    """
    return {t for t in tokenize(strip_markers(text)) if t not in STOPWORDS}


def term_grounding_ratio(claims: Sequence[tuple[str, bool]]) -> float:
    """`grounding_ratio = 被支持实词数 / 答案实词总数`。

    claims 是 `[(句子正文, 该句是否被证据支持)]`。按实词而不是按句计权：
    长句与短衔接句等权时，一句"综上所述。"会顶掉一整句真实声明。

    分母为 0（空答案、或整段都是停用词）时返回 **0.0**，不返回 1.0 ——
    什么都没说的时候不该报告"完全有据"。
    """
    total = 0
    supported = 0
    for text, ok in claims:
        n = len(content_terms(text))
        total += n
        if ok:
            supported += n
    if not total:
        return 0.0
    return supported / total


@dataclass(slots=True)
class SourceTrace:
    """一条可展示、可点击定位的溯源记录（对外契约）。

    字段与规格一一对应：`answer_span` 是答案文本片段；`page` + `bbox` 供前端
    PDF.js 定位到原文位置；`confidence` 是 0~1 的可信度；`attribution_method`
    说明这条结论是**怎么**被认定的（见下面的取值）。
    """

    answer_span: str = ""
    chunk_id: int | str = ""
    paper_id: int | str = ""
    page: int | None = None
    bbox: Any | None = None
    confidence: float = 0.0
    attribution_method: str = "self_citation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_span": self.answer_span,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "page": self.page,
            "bbox": self.bbox,
            "confidence": round(self.confidence, 4),
            "attribution_method": self.attribution_method,
        }


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
    answer_span: str = ""  # 答案里引用它的那句话（去掉编号）
    char_span: tuple[int, int] | None = None  # 该句在答案中的字符区间，供前端高亮
    bbox: Any | None = None  # 命中块在 PDF 页里的坐标
    attribution_method: str = "self_citation"

    @property
    def confidence(self) -> float:
        """对外暴露 0~1 的可信度 —— 就是蕴含判定分，不再另存一份免得两边漂移。"""
        return self.nli_score

    def to_source_trace(self) -> SourceTrace:
        return SourceTrace(
            answer_span=self.answer_span,
            chunk_id=self.chunk_id,
            paper_id=self.paper_id,
            page=self.page_start,
            bbox=self.bbox,
            confidence=self.nli_score,
            attribution_method=self.attribution_method,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "section": self.section,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "nli_score": round(self.nli_score, 4),
            "confidence": round(self.confidence, 4),
            "supported": self.supported,
            "quote": self.quote,
            "answer_span": self.answer_span,
            "char_span": list(self.char_span) if self.char_span else None,
            "bbox": self.bbox,
            "attribution_method": self.attribution_method,
        }


@dataclass(slots=True)
class TraceReport:
    citations: list[Citation] = field(default_factory=list)
    grounding_ratio: float = 0.0
    sentences_total: int = 0
    sentences_supported: int = 0
    terms_total: int = 0  # 答案实词总数（grounding_ratio 的分母）
    terms_supported: int = 0  # 落在被支持句子里的实词数
    unsupported_claims: list[str] = field(default_factory=list)
    uncited_claims: list[str] = field(default_factory=list)
    phantom_markers: list[int] = field(default_factory=list)  # 越界编号 = 幻觉引用
    number_mismatches: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.grounding_ratio >= settings.GROUNDING_MIN_RATIO and not self.phantom_markers

    def source_traces(self) -> list[SourceTrace]:
        """对外契约视图。API 直接返回它，前端不必知道 Citation 的内部形状。"""
        return [c.to_source_trace() for c in self.citations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "grounding_ratio": round(self.grounding_ratio, 4),
            "sentences_total": self.sentences_total,
            "sentences_supported": self.sentences_supported,
            "terms_total": self.terms_total,
            "terms_supported": self.terms_supported,
            "passed": self.passed,
            "phantom_markers": self.phantom_markers,
            "unsupported_claims": self.unsupported_claims[:10],
            "uncited_claims": self.uncited_claims[:10],
            "number_mismatches": self.number_mismatches[:10],
            "citations": [c.to_dict() for c in self.citations],
            "sources": [s.to_dict() for s in self.source_traces()],
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
        # 本轮是否真的用了 NLI 模型 —— 决定 attribution_method 报 nli 还是 hybrid
        self._nli_used = False
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
        """返回 claim 被 evidence 支持的程度 [0,1]。

        `contradiction` 与 `neutral` 都不算支持 —— 规格要求"矛盾即幻觉、
        中性即无法验证"，两者都够不上被支持的证据。
        """
        if self.nli is not None:
            try:
                # 标签顺序按模型 config：contradiction, entailment, neutral
                scores = self.nli.predict([(evidence, claim)], apply_softmax=True)[0]
                self._nli_used = True
                return float(scores[1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("NLI 推理失败，退回词法代理：{}", exc)
        return lexical_entailment(claim, evidence)

    def _method(self, supported: bool) -> str:
        """这条引用是**怎么**被认定的 —— 三种取值对应三种验证强度。

        - `nli`：命中白名单 + 真 NLI 模型判定 entailment（最强）
        - `hybrid`：命中白名单 + 词法蕴含 + 数字一致性（模型不可用时的可用档位）
        - `self_citation`：只过了引用编号白名单，语义上**没有**被支持 ——
          前端据此把这条引用标灰，别让它看起来和上面两种一样可信
        """
        if not supported:
            return "self_citation"
        return "nli" if self._nli_used else "hybrid"

    def trace(self, answer: str, chunks: Sequence[Any]) -> TraceReport:
        """核心入口。chunks 的顺序决定编号：第 i 个对应正文里的 [i]。"""
        report = TraceReport()
        if not chunks:
            # 没有上下文 → 答案里的一切都是无据声明。实词率分母照算，
            # 这样"无上下文硬答"这条路径上的 ratio 是 0 而不是 1。
            report.uncited_claims = split_sentences(answer)
            report.sentences_total = max(1, len(report.uncited_claims))
            report.terms_total = len(content_terms(answer))
            return report

        by_marker = dict(enumerate(chunks, start=1))

        # ---- 1. 白名单校验（Self-Citation 的机械部分）----
        seen_markers = extract_markers(answer)
        report.phantom_markers = sorted({m for m in seen_markers if m not in by_marker})
        if report.phantom_markers:
            logger.warning("检测到幻觉引用编号（不在检索上下文内）：{}", report.phantom_markers)

        # ---- 2. 逐句 NLI / 词法蕴含验证 ----
        spans = sentence_spans(answer)
        report.sentences_total = max(1, len(spans))
        cited_markers: dict[int, Citation] = {}
        claims: list[tuple[str, bool]] = []

        for start, end, sentence in spans:
            plain = strip_markers(sentence)
            markers = [m for m in extract_markers(sentence) if m in by_marker]
            if not markers:
                if len(plain) > 8:  # 忽略"综上所述"这类短衔接句
                    report.uncited_claims.append(plain)
                claims.append((plain, False))
                continue

            sentence_supported = False
            marker_supported: dict[int, bool] = {}
            for m in markers:
                chunk = by_marker[m]
                content = getattr(chunk, "content", "") or ""
                score = self.entail(plain, content)
                numbers_ok = numbers_consistent(sentence, content)
                ok = score >= self.nli_threshold and numbers_ok
                if not numbers_ok:
                    report.number_mismatches.append(plain[:160])
                sentence_supported = sentence_supported or ok
                marker_supported[m] = marker_supported.get(m, False) or ok

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
                        answer_span=plain,
                        char_span=(start, end),
                        bbox=getattr(chunk, "bbox", None),
                    )
                else:
                    # 同一编号被多次引用，取最高分
                    prev = cited_markers[m]
                    if score > prev.nli_score:
                        prev.nli_score = score
                    prev.supported = prev.supported or ok

            # 认定方法要等这句验证完才能定：既取决于 entail 是否真的走了模型，
            # 也取决于这一句最终有没有被支持
            for m in markers:
                cited_markers[m].attribution_method = self._method(marker_supported[m])

            claims.append((plain, sentence_supported))
            if sentence_supported:
                report.sentences_supported += 1
            else:
                report.unsupported_claims.append(plain[:200])

        report.citations = [cited_markers[m] for m in sorted(cited_markers)]
        # ---- 3. Grounding Ratio（实词级）----
        report.terms_total = sum(len(content_terms(text)) for text, _ in claims)
        report.terms_supported = sum(len(content_terms(text)) for text, ok in claims if ok)
        report.grounding_ratio = term_grounding_ratio(claims)
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
    """自检：溯源引擎的三方法各自能拦住什么。"""
    from dataclasses import dataclass

    @dataclass
    class Chunk:
        id: str
        content: str
        paper_id: str = "p1"
        section: str = "Method"
        page: int = 3
        bbox: tuple[float, float, float, float] | None = (72.0, 120.0, 480.0, 143.0)

    chunks = [
        Chunk("c1", "Our method improves accuracy by 12% on GLUE benchmark compared to BERT."),
        Chunk("c2", "We use a sparse attention mechanism to reduce quadratic complexity."),
    ]
    tracer = SourceTracer(enable_nli=False)  # 强制走词法代理，离线可跑

    # 1) 正常引用：命中白名单且被支持 → ratio = 1，且溯源契约字段齐全
    ok = tracer.trace("The method improves accuracy by 12% on GLUE [1].", chunks)
    assert ok.passed, ok.to_dict()
    assert ok.grounding_ratio == 1.0
    assert len(ok.phantom_markers) == 0
    trace = ok.source_traces()[0]
    assert trace.chunk_id == "c1" and trace.paper_id == "p1" and trace.page == 3
    assert trace.bbox == (72.0, 120.0, 480.0, 143.0)
    assert trace.confidence > 0 and trace.attribution_method == "hybrid", trace
    assert trace.answer_span == "The method improves accuracy by 12% on GLUE ."

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

    # 5) 注入幻觉会让实词级有据率下降（不是因为句子变多，是因为实词落到了无据句里）
    clean = tracer.trace("The method improves accuracy by 12% on GLUE [1].", chunks)
    polluted = tracer.trace(
        "The method improves accuracy by 12% on GLUE [1]. "
        "Moreover the authors invented a completely fictional dataset called SuperGLUE-XL.",
        chunks,
    )
    assert polluted.grounding_ratio < clean.grounding_ratio
    assert polluted.terms_supported == clean.terms_total  # 有据的那部分实词没变
    assert polluted.terms_total > clean.terms_total

    # 6) 工具函数
    assert extract_markers("a [1,2] b [3-5] c") == [1, 2, 3, 4, 5]
    assert strip_markers("答案[1]完") == "答案完"
    assert numbers_in("12% and 3.5") == {"12%", "3.5"}
    assert not numbers_consistent("提升 21%", "提升了 12%")
    assert numbers_consistent("提升 12%", "提升了 12%")
    assert split_sentences("第一句。第二句！") == ["第一句。", "第二句！"]
    # 切句要同时给出字符区间，且区间能切回原句
    text = "第一句。第二句！"
    assert [text[a:b] for a, b, _ in sentence_spans(text)] == ["第一句。", "第二句！"]
    # 实词：停用词、标点、纯数字都不算（数字的校验由 numbers_consistent 单独负责）
    assert content_terms("这个方法 in the paper") == {"方", "法", "paper"}
    assert content_terms("12% 的[1]标点。") == {"标", "点"}
    assert term_grounding_ratio([]) == 0.0
    assert term_grounding_ratio([("的了的了", False)]) == 0.0
    assert term_grounding_ratio([("方法有效", True)]) == 1.0

    print("source_tracing self-check OK")


if __name__ == "__main__":  # pragma: no cover
    _demo()
