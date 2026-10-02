"""公式识别 → LaTeX，并区分行内公式与块级公式。

三条可用的路径，按可靠性排序
---------------------------
1. **MinerU**（`MINERU_METHOD=auto`）：它内置公式识别，`equation` 块直接是 LaTeX
   文本，落在正文行里 —— 本模块对这类行只做规范化，不重认；
2. **PyMuPDF 的数学字体**：`pdf._line_from_fitz` 已经把"过半字符来自 CMMI/CMSY/
   Symbol 这类数学字体"的行标成 `kind="formula"`。这条最稳，因为它是版式事实
   而不是猜测；
3. **符号密度启发式**（`is_formula_line`）：兜住上面两条都漏掉的（换了模板字体、
   纯文本后端）。**只有它会产生假阳性**，所以判据写得很保守。

为什么不上 Nougat / Mathpix：Nougat 要 torch + 数 GB 权重（与 MinerU 重复，
还会把"重解析器"的部署问题再引一遍）；Mathpix 是付费 API，要 key 加一次网络
往返，而这套语料的公式 MinerU 已经能给出 LaTeX。

`ponytail:` 天花板 = **图片里**的公式（扫描件没有文本层，上面三条路径全摸不到）。
真被这类语料咬到时再上 Nougat / Mathpix，接在 `ocr_parser` 后面。
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.parsers.pdf import Block

#: 出现就算"这行含数学"。刻意不含 `=` `+` `-` `~` —— 它们在普通正文里太常见。
MATH_SYMBOLS = frozenset(
    "∫∮∑∏√±×÷≈≠≤≥≡∼∂∇∞∈∉∋⊂⊆⊃⊇∪∩∧∨¬∀∃⊥∥∠∴∵∝⊕⊗⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
)
#: 已有 LaTeX 定界符 → 上游（MinerU）已经认好了，别再动
_LATEX_DELIM_RE = re.compile(r"\$[^$]+\$|\\\[.+?\\\]|\\\(.+?\\\)")
_OPS = frozenset("+-*/^_<>")
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f]")
_WS_RE = re.compile(r"\s+")

_SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻"
_SUB_CHARS = "₀₁₂₃₄₅₆₇₈₉₊₋"
_SUP_MAP = str.maketrans(_SUP_CHARS, "0123456789+-")
_SUB_MAP = str.maketrans(_SUB_CHARS, "0123456789+-")

#: Unicode → LaTeX。只收常见项：贪多会把正文里的普通字符也改掉。
_UNICODE_LATEX: dict[str, str] = {
    "∫": r"\int",
    "∮": r"\oint",
    "∑": r"\sum",
    "∏": r"\prod",
    "√": r"\sqrt",
    "±": r"\pm",
    "×": r"\times",
    "÷": r"\div",
    "≈": r"\approx",
    "≠": r"\neq",
    "≤": r"\leq",
    "≥": r"\geq",
    "≡": r"\equiv",
    "∼": r"\sim",
    "∂": r"\partial",
    "∇": r"\nabla",
    "∞": r"\infty",
    "∈": r"\in",
    "∉": r"\notin",
    "⊂": r"\subset",
    "⊆": r"\subseteq",
    "∪": r"\cup",
    "∩": r"\cap",
    "∧": r"\wedge",
    "∨": r"\vee",
    "¬": r"\neg",
    "∀": r"\forall",
    "∃": r"\exists",
    "⊥": r"\perp",
    "∠": r"\angle",
    "∴": r"\therefore",
    "∵": r"\because",
    "∝": r"\propto",
    "⊕": r"\oplus",
    "⊗": r"\otimes",
    "←": r"\leftarrow",
    "→": r"\to",
    "↔": r"\leftrightarrow",
    "↑": r"\uparrow",
    "↓": r"\downarrow",
    "⇔": r"\Leftrightarrow",
    "⇒": r"\Rightarrow",
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "ζ": r"\zeta",
    "η": r"\eta",
    "θ": r"\theta",
    "ι": r"\iota",
    "κ": r"\kappa",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ξ": r"\xi",
    "ο": "o",
    "π": r"\pi",
    "ρ": r"\rho",
    "σ": r"\sigma",
    "τ": r"\tau",
    "υ": r"\upsilon",
    "φ": r"\phi",
    "χ": r"\chi",
    "ψ": r"\psi",
    "ω": r"\omega",
    "Γ": r"\Gamma",
    "Δ": r"\Delta",
    "Θ": r"\Theta",
    "Λ": r"\Lambda",
    "Ξ": r"\Xi",
    "Π": r"\Pi",
    "Σ": r"\Sigma",
    "Φ": r"\Phi",
    "Ψ": r"\Psi",
    "Ω": r"\Omega",
}


def is_formula_line(text: str, *, max_chars: int = 240) -> bool:
    """保守地判"这一行是块级公式"。

    误判的代价不对称：把正文判成公式 = 一行正文变成 `chunk_type=formula`，
    检索按类型过滤时会被漏掉；把公式判成正文 = 没转 LaTeX，照样能检索。
    所以判据宁可漏，不可过。含汉字的行一律不算公式 —— 中文论文里"α 与 β 的关系"
    这种正文太常见了。
    """
    line = text.strip()
    if not line or len(line) > max_chars:
        return False
    if _LATEX_DELIM_RE.search(line):
        return True
    if _CJK_RE.search(line):
        return False
    math = sum(1 for ch in line if ch in MATH_SYMBOLS)
    letters = sum(1 for ch in line if ch.isalpha())
    ops = sum(1 for ch in line if ch in _OPS)
    if math >= 2:
        return True
    if "=" in line and ops >= 1:
        return True
    # 单符号 + 等号 + 极短（"E = mc²"）：靠"字母少"兜住，别把整句话捞进来
    return bool(math >= 1 and "=" in line and letters <= 6)


def _take_run(text: str, start: int, charset: str, table: dict[int, str]) -> tuple[str, int]:
    end = start
    while end < len(text) and text[end] in charset:
        end += 1
    return text[start:end].translate(table), end


def to_latex(text: str) -> str:
    """Unicode 数学 → LaTeX。连写的上标/下标合成一个 `^{}` / `_{}`。"""
    out: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch in _SUP_CHARS:
            run, index = _take_run(text, index, _SUP_CHARS, _SUP_MAP)
            out.append(f"^{{{run}}}")
            continue
        if ch in _SUB_CHARS:
            run, index = _take_run(text, index, _SUB_CHARS, _SUB_MAP)
            out.append(f"_{{{run}}}")
            continue
        mapped = _UNICODE_LATEX.get(ch)
        if mapped is not None:
            out.append(mapped)
            # 命令后紧跟字母会被吃成一个未知命令（\alphabeta），必须留白
            if index + 1 < len(text) and text[index + 1].isalpha():
                out.append(" ")
        else:
            out.append(ch)
        index += 1
    return _WS_RE.sub(" ", "".join(out)).strip()


def inline_math(text: str, *, max_run: int = 24) -> str:
    """把行内的纯数学片段包成 `$...$`，供前端 MathJax 渲染。

    只认"整个空格分隔片段就是公式"的情况（无汉字、够短、含数学符号）。
    含汉字的片段一律放行 —— 中文没有词间空格，"其中α是系数"整段是一个片段，
    包起来只会把正文变成 LaTeX 垃圾。代价是这类行内公式漏转，但至少不产错。
    """
    pieces = re.split(r"(\s+)", text)
    converted: list[str] = []
    for piece in pieces:
        if not piece or piece.isspace():
            converted.append(piece)
            continue
        if piece.startswith("$") and piece.endswith("$"):
            converted.append(piece)
            continue
        has_math = any(ch in MATH_SYMBOLS for ch in piece)
        if has_math and not _CJK_RE.search(piece) and len(piece) <= max_run:
            converted.append(f"${to_latex(piece)}$")
        else:
            converted.append(piece)
    return "".join(converted)


def split_formulas(blocks: list[Block], *, inline_latex: bool = True) -> tuple[list[Block], list[Block]]:
    """把块级公式摘出来，并（可选地）给正文里的行内公式加 LaTeX 定界符。

    返回 `(留在正文里的块, 公式块)`。摘出来的公式**不再进正文流** —— 留在里面
    会被检索命中两次（一次正文 chunk、一次 formula chunk）。
    """
    kept: list[Block] = []
    formulas: list[Block] = []
    for block in blocks:
        if block.kind == "formula":
            formulas.append(replace(block, text=_display(block.text), inline=False))
            continue
        if block.kind == "text" and is_formula_line(block.text):
            formulas.append(replace(block, kind="formula", text=_display(block.text), inline=False))
            continue
        if block.kind == "text" and inline_latex and any(ch in MATH_SYMBOLS for ch in block.text):
            kept.append(replace(block, text=inline_math(block.text)))
            continue
        kept.append(block)
    return kept, formulas


def _display(text: str) -> str:
    """块级公式统一用 `$$...$$` 包一层 —— 前端按定界符决定行内还是独占一行渲染。"""
    stripped = text.strip()
    if stripped.startswith("$"):
        return stripped
    return f"$${to_latex(stripped)}$$"


__all__ = [
    "MATH_SYMBOLS",
    "inline_math",
    "is_formula_line",
    "split_formulas",
    "to_latex",
]
