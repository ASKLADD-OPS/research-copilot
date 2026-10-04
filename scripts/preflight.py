#!/usr/bin/env python3
"""提交前自检 —— 把「提交前检查表」变成可机械执行的东西。

为什么不是一张静态的勾选表：手工勾选会在最不该出错的时候出错（记错了、跑过但没保存）。
这个脚本每一项都**去现场取证**，通过了就打印证据，没通过就 FAIL 并给出修法。

只用标准库：任何一台机器上 `python scripts/preflight.py` 就能跑，不需要装依赖。

用法
----
    python scripts/preflight.py                     # 只查本地材料
    python scripts/preflight.py --url https://demo.example.com   # 顺带查在线服务
    python scripts/preflight.py --skip-git          # 不在 git 仓库里时跳过 git 项

退出码 0 = 全部通过；1 = 有 FAIL（WARN 不阻塞）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 真实密钥的形态。宁可漏报也别误报，所以只认高置信度的模式。
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/DeepSeek 风格 key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("私钥正文", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
]
# 占位值白名单：示例文件里的这些不算泄露
SECRET_ALLOWLIST = re.compile(
    r"(sk-test|sk-your|sk-xxx|change-me|changeme|your[_-]?key|placeholder|example|dummy|fake|not-a-real)",
    re.IGNORECASE,
)

RESULT: list[tuple[str, str, str]] = []  # (level, name, detail)


def record(level: str, name: str, detail: str = "") -> None:
    RESULT.append((level, name, detail))


def ok(name: str, detail: str = "") -> None:
    record("OK", name, detail)


def warn(name: str, detail: str = "") -> None:
    record("WARN", name, detail)


def fail(name: str, detail: str = "") -> None:
    record("FAIL", name, detail)


def sh(*args: str, cwd: Path = ROOT) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ==================================================================== git
def check_git() -> None:
    code, out = sh("git", "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        warn("git 仓库", "当前目录不是 git 工作区，跳过 git 相关检查")
        return

    code, out = sh("git", "rev-list", "--count", "HEAD")
    if code != 0:
        # 这就是那个能让 `git clean -fd` 清空整个工程的危险状态
        fail("git 提交历史", "仓库没有任何 commit —— 所有源码都是 untracked，一次 git clean -fd 就会清光")
        return
    commits = out.strip()
    if int(commits) < 2:
        warn("git 提交历史", f"只有 {commits} 个 commit，看起来不像真实演进历史")
    else:
        ok("git 提交历史", f"{commits} 个 commit")

    code, out = sh("git", "log", "-1", "--format=%ad", "--date=short")
    if code == 0:
        ok("最近一次提交", out.strip())

    # .env 绝不能被追踪
    code, _ = sh("git", "ls-files", "--error-unmatch", ".env")
    if code == 0:
        fail(".env 未被提交", ".env 已被 git 追踪 —— 立刻 `git rm --cached .env` 并确认 .gitignore 里有 .env")
    else:
        ok(".env 未被提交", "未被追踪（.gitignore 生效）")

    # 未提交的改动
    code, out = sh("git", "status", "--porcelain")
    if code == 0:
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if lines:
            warn("工作区是否干净", f"{len(lines)} 个文件未提交 —— 提交前记得 commit")
        else:
            ok("工作区是否干净", "无未提交改动")


# ==================================================================== 敏感信息
def tracked_files() -> list[Path]:
    code, out = sh("git", "ls-files")
    if code != 0:
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [ROOT / ln for ln in out.splitlines() if ln.strip()]


def check_secrets() -> None:
    suspects: list[str] = []
    binary_ext = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".woff", ".woff2", ".ico", ".mp4", ".webm"}
    scanned = 0
    for path in tracked_files():
        if not path.exists() or path.suffix.lower() in binary_ext:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                if SECRET_ALLOWLIST.search(m.group(0)):
                    continue
                rel = path.relative_to(ROOT).as_posix()
                suspects.append(f"{rel} → {label}: {m.group(0)[:12]}…")
    if suspects:
        fail("无敏感信息硬编码", f"{len(suspects)} 处疑似密钥：\n      " + "\n      ".join(suspects[:10]))
    else:
        ok("无敏感信息硬编码", f"已扫描 {scanned} 个文本文件，未发现高置信度密钥")


def check_env_example() -> None:
    example = ROOT / ".env.example"
    if not example.exists():
        fail(".env.example 存在", "缺失 —— 评审拿不到配置模板")
        return
    text = example.read_text(encoding="utf-8", errors="ignore")
    # 模板里不该出现看起来像真 key 的长串
    for label, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            if not SECRET_ALLOWLIST.search(m.group(0)):
                fail(".env.example 只有占位值", f"疑似真实 {label}: {m.group(0)[:12]}…")
                return
    ok(".env.example 只有占位值", f"{len(text.splitlines())} 行")


# ==================================================================== 技术文档
def pdf_page_count(path: Path) -> int:
    """不用第三方库数页数：/Type /Page 对象出现次数（排除 /Pages）。"""
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page(?![s])", data))


def check_docs() -> None:
    pdf = ROOT / "docs" / "技术文档.pdf"
    md = ROOT / "docs" / "技术文档.md"
    if not pdf.exists():
        fail("技术文档 PDF 存在", f"缺少 {pdf.relative_to(ROOT)} —— 跑 `python scripts/export_doc_pdf.py`")
        return
    pages = pdf_page_count(pdf)
    size_mb = pdf.stat().st_size / 1048576
    if pages > 30:
        fail("技术文档页数 ≤30", f"实际 {pages} 页 —— 超了，需要压缩内容")
    else:
        ok("技术文档页数 ≤30", f"{pages} 页 / {size_mb:.2f} MB")
    if not md.exists():
        warn("技术文档源文件", "只有 PDF 没有 Markdown 源，评审无法核对")
    else:
        ok("技术文档源文件", f"{len(md.read_text(encoding='utf-8').splitlines())} 行 Markdown")


# ==================================================================== 测试与部署资产
def check_assets() -> None:
    must = {
        "pytest 覆盖率门槛": ("backend/pyproject.toml", "fail_under"),
        "关键路径集成测试": ("backend/tests/test_key_paths.py", None),
        "Agent 图 e2e": ("backend/tests/test_agent_graph_e2e.py", None),
        "MCP 工具链测试": ("backend/tests/test_mcp_tools.py", None),
        "图谱分析测试": ("backend/tests/test_graph_analysis.py", None),
        "前端 E2E 用例": ("frontend/e2e/key-flows.spec.ts", None),
        "Playwright 配置": ("frontend/playwright.config.ts", None),
        "docker-compose": ("docker-compose.yml", None),
        "生产覆盖层": ("docker-compose.prod.yml", None),
        "GPU 覆盖层": ("docker-compose.gpu.yml", None),
        "Nginx 反向代理": ("deploy/nginx/research-copilot.conf", None),
        "HTTPS 说明": ("deploy/HTTPS.md", None),
    }
    missing = []
    for label, (rel, needle) in must.items():
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        if needle and needle not in p.read_text(encoding="utf-8", errors="ignore"):
            missing.append(f"{rel}（缺 {needle}）")
    if missing:
        fail("交付资产齐全", "缺失：" + "，".join(missing))
    else:
        ok("交付资产齐全", f"{len(must)} 项")


# ==================================================================== 在线服务
def check_online(url: str) -> None:
    base = url.rstrip("/")
    checks = [
        ("入口页", f"{base}/", 200),
        ("Nginx 探针", f"{base}/healthz", 200),
        ("后端健康", f"{base}/health", 200),
    ]
    for label, target, expect in checks:
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "preflight/1.0"})
            # 本机地址走代理会被拦（本项目环境有 HTTP_PROXY），显式绕开
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # noqa: BLE001
            fail(f"在线 {label}", f"{target} 不可达：{type(e).__name__}: {e}")
            continue
        if code == expect:
            ok(f"在线 {label}", f"{target} → {code}")
        else:
            fail(f"在线 {label}", f"{target} → {code}（期望 {expect}）")


# ==================================================================== 汇总
def main() -> int:
    ap = argparse.ArgumentParser(description="提交前自检")
    ap.add_argument("--url", help="在线演示地址，填了才查在线服务")
    ap.add_argument("--skip-git", action="store_true", help="跳过 git 相关检查")
    ap.add_argument("--json", help="把结果写成 JSON 文件")
    args = ap.parse_args()

    print(f"提交前自检 · 根目录 {ROOT}\n" + "=" * 68)

    check_assets()
    check_docs()
    check_secrets()
    check_env_example()
    if not args.skip_git:
        check_git()
    if args.url:
        check_online(args.url)

    order = {"FAIL": 0, "WARN": 1, "OK": 2}
    for level, name, detail in sorted(RESULT, key=lambda r: order[r[0]]):
        mark = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
        print(f"{mark} {name}" + (f"\n       {detail}" if detail else ""))

    fails = [r for r in RESULT if r[0] == "FAIL"]
    warns = [r for r in RESULT if r[0] == "WARN"]
    print("=" * 68)
    print(f"通过 {len([r for r in RESULT if r[0] == 'OK'])} · 警告 {len(warns)} · 失败 {len(fails)}")

    if args.json:
        Path(args.json).write_text(
            json.dumps([{"level": lv, "name": n, "detail": d} for lv, n, d in RESULT], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")

    if fails:
        print("\n有 FAIL 项 —— 提交前必须修掉：")
        for _lv, name, _d in fails:
            print(f"  · {name}")
        return 1
    print("\n全部检查通过，可以提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
