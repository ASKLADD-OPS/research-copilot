"""Prompt 集中加载器 —— 所有 LLM Prompt 以 Markdown 存于此目录。

约定：
- 文件名即 prompt 名（`intent.md` → `load("intent")`）。
- 首次读取后缓存；开发期改文件不会自动生效，重启即可。
- 模板占位用 `{{var}}`，由 `render()` 做简单替换（不引入 jinja2，够用就行）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@lru_cache(maxsize=64)
def load(name: str) -> str:
    """读取 prompt 正文。name 不带 .md。"""
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt 不存在：{path}")
    return path.read_text(encoding="utf-8")


def render(name: str, **vars: object) -> str:
    """渲染 prompt，替换 {{var}}。未提供的占位保留原样，便于发现漏传。"""
    text = load(name)
    return _PLACEHOLDER.sub(lambda m: str(vars.get(m.group(1), m.group(0))), text)


def available() -> list[str]:
    return sorted(p.stem for p in PROMPT_DIR.glob("*.md"))


__all__ = ["available", "load", "render"]
