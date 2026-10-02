"""Guardrails 节点 —— 四层防护 + 决策路由。

热路径（每轮问答都要过）只做正则与集合运算，**不调 LLM**：输入侧的注入 / PII /
长度，检索侧的阈值与恶意内容，生成侧的引用白名单与幻觉率，输出侧的敏感内容 /
学术规范 / 长度，全是 O(n) 字符串扫描。

唯一会调模型的是"学术规范分类器"，且**只在已经决定重写之后**才调一次 ——
它不负责判"要不要拦"，只负责把"该怎么改"说具体，属于软路径上的增强。
（`test_agent_graph_e2e` 里那条"happy path 只调 3 次模型"的断言就是靠这个约束守住的。）

决策（与 `prompts/guardrails.md` 的 action 一一对应）：

| action | 触发 | 去向 |
|---|---|---|
| `block` | 注入就是整条消息 / 越权 / 敏感内容 / 复述系统提示 | END + 错误码 4003 |
| `rewrite` | 有据率 < 0.8、幻觉引用、引用失据 | 回 synthesizer 重写，**一轮为限** |
| `strip` | 注入被剥离、越界编号被删 | END，用清理后的内容收尾 |
| `pass` | 无问题 | END |

重写预算靠 `state["guardrail_action"]` 自己判：本轮已写过一次就不会再写第二次。
不用 `guardrail_flags` 判 —— 那个字段带 `operator.add` reducer，跨轮只增不减，
第二轮起会被历史标记永久判成"已重写"。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.agents.prompts import render
from app.agents.state import AgentState
from app.core.config import settings
from app.core.logging import logger
from app.llm.client import Role
from app.llm.structured import complete_structured
from app.rag.source_tracing import extract_markers

# ---------------------------------------------------------------- 输入侧：注入

# 关键词直匹配：典型句式，命中即剥离该句。
INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"ignore\s+(all\s+)?(previous|above)\s+instructions?",
        r"忽略(以上|上述|之前)的?(所有)?(指令|要求|提示)",
        r"you\s+are\s+now\s+(a|an)\s+",
        r"你现在(是|扮演)",
        r"(打印|输出|重复)(你的)?\s*(system\s*prompt|系统提示|初始指令)",
        r"reveal\s+your\s+(system\s+)?prompt",
    )
]

# 语义型注入：不做 embedding —— 热路径上加载 bge-m3 要 2GB 权重与一次前向，
# 代价远超收益。这里用"指令动词 + 目标名词"的结构启发式兜住换个说法的同一类意图。
# ponytail: 覆盖率靠模板堆；等真机上有 embedding 常驻，把这里换成与注入模板的相似度判定。
SEMANTIC_INJECTION = [
    re.compile(p, re.I)
    for p in (
        r"(忘掉|忽略|跳过|绕过|无视).{0,10}(规则|限制|设定|约束)",
        r"(不要|别)(再)?(遵守|执行|管).{0,8}(规则|指令|限制)",
        r"(以|用)?\s*(开发者|管理员|上帝|root)\s*模式",
        r"(越过|突破|解除|关闭).{0,8}(安全|内容)?.{0,6}(限制|过滤|审查|策略|防护)",
        r"(泄露|透露|导出|展示).{0,8}(系统|内部|隐藏).{0,6}(提示|指令|配置|设定)",
        r"forget\s+(everything|your\s+rules|your\s+instructions)",
        r"(jailbreak|dan\s+mode|developer\s+mode|do\s+anything\s+now)",
    )
]

# 越权：访问他人论文 / 系统级配置。命中即硬拒绝。
PERMISSION_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"(查看|访问|获取|下载|打开|删除).{0,8}(他人|别人|其他用户|其他账号).{0,8}(论文|文件|数据|会话|记录|历史)",
        r"(查看|导出|读取|列出).{0,6}(全部|所有|系统).{0,4}(用户|账号|密钥|密码|配置)",
        r"(管理员|root|超级用户|后台).{0,6}(权限|密钥|密码|配置)",
    )
]

# 输入侧剥离：命中后把该句切掉，而不是拒绝整条消息
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+")

# ---------------------------------------------------------------- PII / 敏感内容

# 只收三类最常见的：邮箱、中国大陆手机号、二代身份证号。
# 银行卡号（16~19 位纯数字）故意不收 —— 论文正文里的长数字（矩阵维度、随机种子）
# 会被它成片误伤，宁可漏也不把正常内容涂掉。
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("邮箱", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
]

# 输出侧敏感内容：违法违规的操作性内容。命中即硬拒绝。
SENSITIVE_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"(制作|合成|提纯|配制).{0,8}(爆炸物|炸药|枪支|毒品|剧毒|神经毒剂)",
        r"(入侵|攻击|拖库|撞库).{0,8}(系统|网站|服务器|数据库).{0,8}(方法|教程|步骤|代码)",
        r"how\s+to\s+(make|build|synthesize)\s+(a\s+)?(bomb|explosive|nerve\s+agent)",
    )
]

# 可注入的 HTML：检索来的片段里带 `<script>` 时，问答回答会把它原样送给前端
HTML_DANGER = re.compile(r"<\s*(script|iframe|object|embed|link|style)\b", re.I)

# ---------------------------------------------------------------- 输出侧：学术规范

# 免责声明的存在性判定（任一命中即视为已声明）。只做成标记，不改写正文 ——
# 在回答后面硬贴一段免责声明会污染用户要的正文，也躲不开"答案是原文"的契约。
DISCLAIMER_HINTS = ("免责声明", "仅供参考", "不构成", "请以原文为准", "请核实", "个人观点")

# 引用格式：只认半角 [n]。全角括号与区间写法先规范化，再交给白名单校验。
_FULLWIDTH_MARKER = re.compile(r"[【［]\s*(\d+)\s*[】］]")
_RANGE_MARKER = re.compile(r"\[\s*(\d+)\s*[-,，]\s*(\d+)\s*\]")

REFUSAL_INJECTION = "这条消息里只有「绕过安全规则」之类的指令，没有可回答的研究问题。请直接提出你的问题。"
REFUSAL_PERMISSION = "我不能访问他人的论文、会话记录或系统配置。请换成你自己的论文或公开文献的问题。"
REFUSAL_SENSITIVE = "这个问题涉及违法违规内容，我不能提供。"
REFUSAL_PROMPT_LEAK = "我无法提供系统提示的内容。请继续问你的研究问题。"

# 触发硬拒绝的标记（优先级高于软性问题）
HARD_FLAGS = {"injection_only", "permission_denied", "sensitive_output", "prompt_leak"}
# 触发重写的标记
SOFT_FLAGS = {"low_grounding", "bad_citation_ref", "hallucination_suspected"}
# 只影响"过没过质量门限"、不触发重写的标记
QUALITY_FLAGS = {"low_grounding", "bad_citation_ref", "hallucination_suspected"}

_REFUSALS = {
    "injection_only": REFUSAL_INJECTION,
    "permission_denied": REFUSAL_PERMISSION,
    "sensitive_output": REFUSAL_SENSITIVE,
    "prompt_leak": REFUSAL_PROMPT_LEAK,
}


# ==================================================================== 输入侧


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 按 1 字 1 个，其余按 4 字符 1 个。

    ponytail: 不引 tiktoken（多一个 tokenizer 依赖 + 每次首载），这个量级只用来做
    "该不该截断"的闸门，误差 20% 不影响结论。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    return cjk + (len(text) - cjk) // 4


def mask_pii(text: str) -> tuple[str, list[str]]:
    """把 PII 替换成占位符。返回 (脱敏文本, 命中的类别)。"""
    hits: list[str] = []
    for label, pattern in PII_PATTERNS:
        if pattern.search(text):
            hits.append(label)
            text = pattern.sub(f"[已隐去{label}]", text)
    return text, hits


def _fit(text: str, max_chars: int, max_tokens: int) -> tuple[str, list[str]]:
    """长度闸门：先按字符截断，再按 token 估算按比例收紧。"""
    flags: list[str] = []
    if len(text) > max_chars:
        flags.append("truncated")
        text = text[:max_chars]
    tokens = estimate_tokens(text)
    if tokens > max_tokens:
        flags.append("over_token_budget")
        # 按估算比例回缩：切出来的长度保证不超过 max_tokens。
        text = text[: max(1, int(len(text) * max_tokens / tokens))]
    return text, flags


def check_input(state: AgentState) -> tuple[list[str], dict[str, Any]]:
    """输入侧：注入、PII、长度。返回 (flags, patch)。"""
    flags: list[str] = []
    patch: dict[str, Any] = {}
    raw = str(state.get("query") or "")

    if not raw.strip():
        return ["empty_query"], {}

    if any(p.search(raw) for p in PERMISSION_PATTERNS):
        flags.append("permission_denied")

    hits = [p for p in (*INJECTION_PATTERNS, *SEMANTIC_INJECTION) if p.search(raw)]
    if hits:
        flags.append("injection_suspected")
        kept = [s for s in _SENT_SPLIT.split(raw) if s and not any(p.search(s) for p in hits)]
        cleaned = " ".join(s.strip() for s in kept).strip()
        if cleaned:
            # 注入只是搭车：剥掉它，用户真正的问题照答
            patch["query"] = cleaned
        else:
            # 整条消息都是指令，剥完什么也不剩 —— 没有可回答的语义，硬拒绝
            flags.append("injection_only")

    masked, pii = mask_pii(patch.get("query", raw))
    if pii:
        flags.append("pii_in_query")
        patch["query"] = masked

    fitted, size_flags = _fit(
        patch.get("query", raw), settings.GUARDRAIL_MAX_INPUT_CHARS, settings.GUARDRAIL_MAX_INPUT_TOKENS
    )
    if size_flags:
        flags.extend(size_flags)
        patch["query"] = fitted

    return flags, patch


# ==================================================================== 检索侧


def looks_malicious(text: str) -> bool:
    """片段里混着注入指令或可执行 HTML —— 即"间接注入"（论文正文里埋攻击）。"""
    return bool(HTML_DANGER.search(text) or any(p.search(text) for p in (*INJECTION_PATTERNS, *SEMANTIC_INJECTION)))


def malicious_chunk_ids(docs: list[dict[str, Any]]) -> list[str]:
    return [str(d.get("chunk_id", "")) for d in docs if looks_malicious(str(d.get("text", "")))]


def check_retrieval(state: AgentState) -> list[str]:
    """检索侧：相关性阈值、恶意内容、单篇独占。

    注意这里是**检出**而不是丢弃：回答已经生成，[n] 编号是按当前片段顺序排的，
    在这里抽掉片段会把编号整体错位、溯源全指偏。真正的丢弃要发生在生成之前
    （retriever 节点），本节点只负责标记出来给前端与留痕。
    """
    flags: list[str] = []
    docs = state.get("reranked") or state.get("retrieved") or []
    if not docs:
        return ["no_context"]

    if all(float(d.get("score", 0.0)) < settings.RETRIEVAL_MIN_SCORE for d in docs):
        flags.append("low_quality_context")

    if malicious_chunk_ids(docs):
        flags.append("malicious_context")

    # 单篇独占：比较类问题只命中一篇，说明检索偏了
    if state.get("intent") == "cross_paper_reasoning":
        papers = {str(d.get("paper_id")) for d in docs}
        if len(papers) == 1 and len(docs) >= 3:
            flags.append("single_source")

    return flags


# ==================================================================== 生成侧


# 输出侧：只有「用户先索要系统提示、回答又真的复述了」才算泄露。
# 单看回答里出现「系统提示」四个字会误伤 prompt engineering 方向的正经回答 ——
# 那正是本系统用户会问的话题。
_LEAK_REQUEST = re.compile(
    r"(打印|输出|重复|展示|告诉我).{0,6}(system\s*prompt|系统提示|初始指令)|reveal\s+your\s+(system\s+)?prompt",
    re.I,
)


def _prompt_leak(query: str, answer: str) -> bool:
    return bool(_LEAK_REQUEST.search(query)) and ("system prompt" in answer.lower() or "系统提示" in answer)


def check_generation(state: AgentState, answer: str) -> tuple[list[str], str, list[dict[str, Any]]]:
    """生成侧：引用白名单、幻觉率。返回 (flags, 修订后的 answer, 过滤后的 citations)。"""
    flags: list[str] = []
    docs = state.get("reranked") or state.get("retrieved") or []
    whitelist = {str(d.get("chunk_id", "")) for d in docs if d.get("chunk_id")}

    # ① 引用白名单：正文里的 [n] 必须落在检索上下文里（n 即片段序号）
    markers = extract_markers(answer)
    if whitelist and any(m > len(docs) for m in markers):
        flags.append("bad_citation_ref")
        answer = re.sub(
            r"[\[【]\s*(\d+)\s*[\]】]",
            lambda m: "" if int(m.group(1)) > len(docs) else m.group(0),
            answer,
        )

    # ② 引用记录里的 chunk_id 也必须真实存在 —— [n] 合法不代表 sources 合法
    citations = list(state.get("citations") or [])
    if whitelist:
        kept = [c for c in citations if str(c.get("chunk_id", "")) in whitelist]
        if len(kept) != len(citations):
            flags.append("bad_citation_ref")
            citations = kept

    # ③ 幻觉检测：NLI（或词法代理）判定"未被支持"的引用占比
    if citations:
        unsupported = sum(1 for c in citations if not c.get("verified"))
        ratio = unsupported / len(citations)
        if ratio > settings.GUARDRAIL_HALLUCINATION_MAX_RATIO:
            flags.append("hallucination_suspected")
            logger.info("幻觉引用占比 {:.0%}（{}/{}）", ratio, unsupported, len(citations))

    if _prompt_leak(str(state.get("query") or ""), answer):
        flags.append("prompt_leak")

    return flags, answer, citations


# ==================================================================== 输出侧


def check_academic_norm(state: AgentState, answer: str) -> list[str]:
    """学术规范：引用格式 + 免责声明。只出标记，不改正文。

    `no_disclaimer` 刻意不触发重写：绝大多数正常回答都不会自带免责声明，
    把它当触发器等于每轮白烧一次生成。
    """
    flags: list[str] = []
    if not state.get("retrieved") and not state.get("plan"):
        return flags  # 寒暄轮不受学术规范约束

    if _FULLWIDTH_MARKER.search(answer) or _RANGE_MARKER.search(answer):
        flags.append("citation_format")
    if not any(h in answer for h in DISCLAIMER_HINTS):
        flags.append("no_disclaimer")
    return flags


def check_output(state: AgentState, answer: str) -> tuple[list[str], str]:
    """输出侧：有据率、敏感内容、PII、HTML、长度。"""
    flags: list[str] = []
    if not answer:
        return ["empty_answer"], answer

    ratio = float(state.get("grounding_ratio") or 0.0)
    if (state.get("retrieved") or state.get("reranked")) and ratio < settings.GROUNDING_MIN_RATIO:
        flags.append("low_grounding")

    if any(p.search(answer) for p in SENSITIVE_PATTERNS):
        flags.append("sensitive_output")
        return flags, answer  # 命中即拒绝，不再做后面的清洗

    masked, pii = mask_pii(answer)
    if pii:
        flags.append("pii_in_output")
        answer = masked

    if HTML_DANGER.search(answer):
        flags.append("html_escaped")
        answer = HTML_DANGER.sub("&lt;", answer)

    if len(answer) > settings.GUARDRAIL_MAX_OUTPUT_CHARS:
        flags.append("output_truncated")
        answer = answer[: settings.GUARDRAIL_MAX_OUTPUT_CHARS] + "\n\n…（输出过长已截断）"

    return flags, answer


# ==================================================================== 决策


def decide(flags: list[str], *, prior_action: str | None = None) -> str:
    """汇总动作。硬拒绝 > 重写（一轮为限）> 剥离 > 放行。纯函数，便于单测。"""
    seen = set(flags)
    if seen & HARD_FLAGS:
        return "block"
    if seen & SOFT_FLAGS:
        # 本轮已经重写过一次 → 收手，按当前稿收尾（预算判定必须在节点内，
        # 放到路由函数里会差一轮，和 reflector 那边踩过的是同一个坑）
        return "pass" if prior_action == "rewrite" else "rewrite"
    if "injection_suspected" in seen:
        return "strip"
    return "pass"


def route_after_guardrails(state: AgentState) -> str:
    """guardrails 的条件边：只有软性问题且预算没用完才回 synthesizer 重写。"""
    return "synthesizer" if state.get("guardrail_action") == "rewrite" else "end"


# ==================================================================== 学术规范分类器


class AcademicCompliance(BaseModel):
    """分类器输出。violations 为空 = 没有违反学术规范。"""

    violations: list[str] = Field(default_factory=list)
    fix_hint: str = ""


def _context_of(state: AgentState) -> str:
    docs = state.get("reranked") or state.get("retrieved") or []
    return "\n\n".join(str(d.get("text", ""))[:600] for d in docs[:5]) or "（无检索上下文）"


async def classify_academic(answer: str, context: str) -> AcademicCompliance | None:
    """LLM 分类器：判断回答是否违反学术规范。失败返回 None（不阻断重写）。"""
    try:
        return await complete_structured(
            AcademicCompliance,
            [
                {"role": "system", "content": render("guardrails")},
                {"role": "user", "content": f"检索上下文：\n{context}\n\n待检查的回答：\n{answer}"},
            ],
            role=Role.REVIEWER,  # 评审用稳定模型
        )
    except Exception as exc:  # noqa: BLE001 - 分类器只是增强，挂了不影响重写
        logger.warning("学术规范分类器不可用：{}", exc)
        return None


def _mechanical_hint(flags: list[str]) -> str:
    hints = []
    if "bad_citation_ref" in flags:
        hints.append("删掉引用编号超出检索上下文范围的那几句，或改写成不依赖该引用的表述")
    if "low_grounding" in flags:
        hints.append("补齐有据率：每一句结论后面都要有检索上下文里的 [n] 支撑，没有支撑的结论删掉")
    if "hallucination_suspected" in flags:
        hints.append("引用被判定为未支持：只保留能在原文中找到依据的表述，数字必须与原文一致")
    if "citation_format" in flags:
        hints.append("引用编号统一用半角 [n]，不要用【n】或 [1,2] 这类写法")
    if "no_disclaimer" in flags:
        hints.append("结尾补一句必要的学术免责声明（结论仅供参考、请以原文为准）")
    return "；".join(hints) or "按检索上下文重写，确保每句结论都有引用支撑"


# ==================================================================== 节点


def _dedupe(flags: list[str]) -> list[str]:
    return list(dict.fromkeys(flags))


async def guardrails_node(state: AgentState) -> dict[str, Any]:
    """图上的防护节点：四层一次过完，产最终 answer 与本轮 action。"""
    in_flags, patch = check_input(state)
    ret_flags = check_retrieval(state)

    working: AgentState = {**state, **patch}  # type: ignore[assignment]
    answer = str(working.get("answer") or "")

    gen_flags, answer, citations = check_generation(working, answer)
    out_flags, answer = check_output(working, answer)
    acad_flags = check_academic_norm(working, answer)

    all_flags = _dedupe([*in_flags, *ret_flags, *gen_flags, *out_flags, *acad_flags])
    action = decide(all_flags, prior_action=state.get("guardrail_action"))
    passed = action != "block" and not (set(all_flags) & QUALITY_FLAGS)

    result: dict[str, Any] = {
        "answer": answer,
        "citations": citations,
        "guardrail_flags": all_flags,
        "guardrail_action": action,
        "guardrail_passed": passed,
        "done": True,
        "trace": [{"node": "guardrails", "action": action, "flags": all_flags, "passed": passed}],
    }
    if "query" in patch:
        result["query"] = patch["query"]

    if action == "block":
        reason = next((f for f in all_flags if f in HARD_FLAGS), "blocked")
        result["answer"] = _REFUSALS.get(reason, REFUSAL_PROMPT_LEAK)
        result["error"] = f"guardrail_blocked:{reason}"
        result["trace"][0]["reason"] = reason
        logger.warning("Guardrails 硬拒绝 reason={} flags={}", reason, all_flags)
        return result

    if action == "rewrite":
        hint = _mechanical_hint(all_flags)
        verdict = await classify_academic(answer, _context_of(working))
        if verdict is not None and verdict.violations:
            result["guardrail_flags"] = _dedupe([*all_flags, "academic_violation"])
            hint = f"{hint}；{verdict.fix_hint}" if verdict.fix_hint else hint
            result["trace"][0]["academic_violations"] = verdict.violations
        # 走 synthesizer 现成的 fix_hint 通道，不另开一条并行 Plumbing
        result["reflection"] = {**(state.get("reflection") or {}), "fix_hint": hint}
        result["trace"][0]["fix_hint"] = hint

    logger.info("Guardrails action={} passed={} flags={}", action, passed, all_flags)
    return result


__all__ = [
    "HARD_FLAGS",
    "INJECTION_PATTERNS",
    "PII_PATTERNS",
    "SEMANTIC_INJECTION",
    "SOFT_FLAGS",
    "AcademicCompliance",
    "check_academic_norm",
    "check_generation",
    "check_input",
    "check_output",
    "check_retrieval",
    "classify_academic",
    "decide",
    "estimate_tokens",
    "guardrails_node",
    "looks_malicious",
    "mask_pii",
    "route_after_guardrails",
]
