"""MCP Server 共用小工具（不单独暴露为工具）。"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

UA = "research-copilot/0.1 (+https://github.com/)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str, *, limit: int = 2000) -> str:
    """去 HTML 标签 / 实体 / 多余空白，截断到 limit。

    **顺序不能反**：先 unescape 再剥标签。arXiv 摘要里的尖括号是转义过的
    （`&lt;b&gt;`），先剥标签它们毫发无伤，unescape 之后才变成真的 `<b>` ——
    结果就是标签被原样喂给了模型。
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


async def get_json(
    url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 30
) -> Any:
    """带 UA 的 GET，返回 JSON。失败抛 httpx 异常，由 MCP 层转成工具错误。"""
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
        resp = await client.get(url, params=params, headers=h)
        resp.raise_for_status()
        return resp.json()


async def get_text(
    url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 30
) -> str:
    h = {"User-Agent": UA, **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
        resp = await client.get(url, params=params, headers=h)
        resp.raise_for_status()
        return resp.text


async def get_bytes(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    """带 UA 的 GET，返回 `(内容, content-type)`。用于下载 PDF 这类二进制。

    `max_bytes` 是**流式**累计上限，不是下载完再检查 —— arXiv 上一个坏链指到
    几百 MB 的 tar 时，后者会先把内存吃光再报错。
    """
    h = {"User-Agent": UA, **(headers or {})}
    chunks: list[bytes] = []
    total = 0
    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client,
        client.stream("GET", url, params=params, headers=h) as resp,
    ):
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        async for piece in resp.aiter_bytes():
            total += len(piece)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"响应超过上限 {max_bytes} 字节（已收 {total}）")
            chunks.append(piece)
    return b"".join(chunks), content_type


__all__ = ["UA", "clean_text", "get_bytes", "get_json", "get_text"]
