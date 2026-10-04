"""
把 docs/技术文档.md 导出为 PDF。

管线：Markdown --(python-markdown)--> HTML（mermaid / MathJax 内联本地 JS）
      --(Chrome headless --print-to-pdf)--> PDF

无需联网：mermaid 与 MathJax 的 dist 已放在 docs/figures/vendor/。

用法：
    python scripts/export_doc_pdf.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SRC = DOCS / "技术文档.md"
BUILD = DOCS / "_build"
VENDOR = DOCS / "figures" / "vendor"
OUT_PDF = DOCS / "技术文档.pdf"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# --------------------------------------------------------------------------- CSS
CSS = r"""
:root{
  --ink:#0f161e; --muted:#5b6672; --line:#e3e6ea; --line2:#eef0f3;
  --accent:#805ce5; --accent-soft:#efe9ff; --bg-soft:#f7f7f8; --danger:#c0564f;
}
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact; print-color-adjust:exact}
body{
  margin:0; color:var(--ink); background:#fff;
  font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Segoe UI",sans-serif;
  font-size:9.5pt; line-height:1.42;
}
@page{ size:A4; margin:12mm 12mm 13mm 12mm; }

h1,h2,h3,h4{ line-height:1.25; break-after:avoid-page; page-break-after:avoid; }
h1{ font-size:16pt; margin:14px 0 7px; padding:4px 0 4px 9px; border-left:5px solid var(--accent);
    border-bottom:1px solid var(--line); letter-spacing:.2px; }
h1:first-of-type{ margin-top:0; }
h2{ font-size:12pt; margin:11px 0 5px; padding-bottom:2px; border-bottom:1px solid var(--line2); }
h3{ font-size:10.4pt; margin:8px 0 4px; color:#20293a; }
h4{ font-size:9.6pt; margin:7px 0 3px; color:#20293a; }
p{ margin:3.5px 0; }
strong{ font-weight:700; }
code{ font-family:Consolas,"Cascadia Mono","Microsoft YaHei",monospace; font-size:.9em;
      background:var(--bg-soft); padding:.5px 2.5px; border-radius:3px; color:#334; }
pre{ background:var(--bg-soft); border:1px solid var(--line); border-radius:5px;
     padding:5px 7px; overflow:visible; break-inside:avoid; page-break-inside:avoid; margin:5px 0; }
pre code{ background:none; padding:0; font-size:7.9pt; line-height:1.36; white-space:pre-wrap;
          word-break:break-word; }

blockquote{ margin:5px 0; padding:3px 9px; border-left:3px solid var(--accent);
            background:#faf8ff; color:#39424f; }
blockquote p{ margin:2px 0; }

/* ---- 表格 ---- */
table{ width:100%; border-collapse:collapse; margin:5px 0; font-size:8.1pt;
       break-inside:avoid; page-break-inside:avoid; }
thead th{ background:var(--accent-soft); color:#2c2350; font-weight:700; text-align:left; }
th,td{ border:1px solid var(--line); padding:2px 4px; vertical-align:top; line-height:1.32; }
tbody tr:nth-child(even){ background:#fbfbfc; }
td code, th code{ font-size:7.6pt; background:#f0f1f4; }

/* ---- 列表 ---- */
ul,ol{ margin:3.5px 0; padding-left:1.4em; }
li{ margin:1.5px 0; }
li>ul,li>ol{ margin:1.5px 0; }

/* ---- 链接 ---- */
a{ color:var(--accent); text-decoration:none; border-bottom:1px solid #d9cdfb; }

/* ---- 图片 ---- */
img{ max-width:88%; height:auto; display:block; margin:6px auto; break-inside:avoid;
     border:1px solid var(--line); border-radius:5px; }

/* ---- mermaid ---- */
pre.mermaid{ background:#fff; border:1px solid var(--line); border-radius:6px;
             padding:5px; text-align:center; }
pre.mermaid svg{ max-width:100%; height:auto; max-height:135mm; }

/* ---- 数学 ---- */
mjx-container{ display:block; text-align:center; margin:5px 0; font-size:100%; }
mjx-container[display="true"]{ break-inside:avoid; }

/* ---- 分隔线：不给整页空白 ---- */
hr{ border:none; border-top:1px solid var(--line2); margin:8px 0; }

/* 避免表格/图跨页后标题孤立 */
h2 + table, h3 + table{ break-before:avoid-page; }
"""

MERMAID_INIT = r"""
<script>
(function(){
  var done=false;
  function boot(){
    if(done) return; done=true;
    try{
      mermaid.initialize({
        startOnLoad:true,
        theme:'neutral',
        securityLevel:'loose',
        fontFamily:'"Microsoft YaHei","PingFang SC",sans-serif',
        flowchart:{ htmlLabels:true, curve:'basis', useMaxWidth:true, nodeSpacing:28, rankSpacing:34 },
        er:{ useMaxWidth:true },
        themeVariables:{
          primaryColor:'#efe9ff', primaryBorderColor:'#805ce5', primaryTextColor:'#0f161e',
          lineColor:'#8a93a0', secondaryColor:'#f5f3fb', tertiaryColor:'#fbfbfc',
          fontSize:'14px'
        }
      });
    }catch(e){ document.title='MERMAID_INIT_ERROR: '+e.message; }
  }
  if(window.mermaid){ boot(); }
  else{
    var t=setInterval(function(){ if(window.mermaid){ clearInterval(t); boot(); } },50);
  }
})();
</script>
<script>
window.MathJax = {
  tex:{ inlineMath:[], displayMath:[['$$','$$']], processEscapes:true },
  svg:{ fontCache:'global' },
  options:{ skipHtmlTags:['script','noscript','style','textarea','pre','code','annotation','annotation-xml'] },
  startup:{ typeset:true }
};
</script>
"""


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    got = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if got:
        return got
    sys.exit("找不到 Chrome，请把可执行文件路径加进 CHROME_CANDIDATES")


def preprocess(md: str) -> tuple[str, list[str]]:
    """抽走 mermaid 原始块 + 把零散行内公式换成纯文本，返回 (正文, mermaid 源码列表)。"""
    mermaids: list[str] = []

    def _stash(m: re.Match) -> str:
        mermaids.append(m.group(1))
        return f"\n@@MERMAID_{len(mermaids) - 1}@@\n"

    md = re.sub(r"```mermaid\n(.*?)```", _stash, md, flags=re.S)

    # 行内公式：只保留 3 处，直接换 unicode，省掉 inlineMath 对货币 $2.5 / $10 的误伤
    inline = {
        r"$O(n)$": "<em>O</em>(<em>n</em>)",
        r"$\sum 1/(k+\text{rank})$": "Σ 1/(<em>k</em>+rank)",
    }
    for k, v in inline.items():
        md = md.replace(k, v)
    if "$" in md:
        left = re.findall(r"\$[^$\n]{1,60}\$", md)
        left = [x for x in left if not x.startswith("$$") and x != "$…$"]
        if left:
            print("[warn] 仍有疑似行内公式未处理:", left)
    return md, mermaids


def build_html() -> tuple[Path, int]:
    import markdown

    md = SRC.read_text(encoding="utf-8")
    md, mermaids = preprocess(md)

    html_body = markdown.markdown(
        md,
        extensions=["extra", "toc", "sane_lists", "admonition"],
        output_format="html5",
    )

    # 还原 mermaid 块（原样喂给浏览器，实体由浏览器自行归一）
    for i, src in enumerate(mermaids):
        html_body = html_body.replace(f"<p>@@MERMAID_{i}@@</p>", f'<pre class="mermaid">{src.rstrip()}</pre>')
        html_body = html_body.replace(f"@@MERMAID_{i}@@", f'<pre class="mermaid">{src.rstrip()}</pre>')

    # 图片路径：HTML 在 docs/_build/ 下 ⇒ figures/x.png → ../figures/x.png
    html_body = html_body.replace('src="figures/', 'src="../figures/')

    mermaid_js = (VENDOR / "mermaid.min.js").read_text(encoding="utf-8")
    mathjax_js = (VENDOR / "mathjax-tex-svg.js").read_text(encoding="utf-8")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Research Copilot 技术文档</title>
<style>{CSS}</style>
</head><body>
{html_body}
<script>{mermaid_js}</script>
<script>{mathjax_js}</script>
{MERMAID_INIT}
</body></html>
"""
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / "技术文档.html"
    out.write_text(html, encoding="utf-8")
    return out, len(mermaids)


def to_pdf(html: Path, pdf: Path, budget_ms: int = 90000) -> None:
    chrome = find_chrome()
    with tempfile.TemporaryDirectory(prefix="rc_pdf_") as prof:
        # 用 file:/// URI（正斜杠 + 盘符）
        uri = "file:///" + str(html.resolve()).replace("\\", "/")
        cmd = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--disable-extensions",
            f"--user-data-dir={prof}",
            "--virtual-time-budget=%d" % budget_ms,
            "--run-all-compositor-stages-before-draw",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={pdf}",
            uri,
        ]
        print("[chrome]", " ".join(cmd[:2]), "...", uri[-40:])
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if not pdf.exists():
            sys.exit(f"PDF 未生成。\nstdout={r.stdout[-800:]!r}\nstderr={r.stderr[-800:]!r}")


def page_count(pdf: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return -1


if __name__ == "__main__":
    html, n_mermaid = build_html()
    print(f"[ok] HTML: {html}  (mermaid 图 {n_mermaid} 张)")
    to_pdf(html, OUT_PDF)
    n = page_count(OUT_PDF)
    size = OUT_PDF.stat().st_size / 1024
    print(f"[ok] PDF : {OUT_PDF}  {n} 页  {size:.0f} KB")
