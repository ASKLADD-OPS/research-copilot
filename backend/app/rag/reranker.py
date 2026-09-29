"""CrossEncoder 重排。

召回阶段（双塔 / 倒排）追求"别漏"，重排阶段追求"排准"：
CrossEncoder 把 (query, passage) 拼在一起过一遍模型，能捕捉交叉特征，
但无法预计算索引，所以只对前 K 个候选做。

重排不是必选项：模型加载失败（显存不足 / 未下载）时**静默跳过**并保留 RRF 顺序 ——
检索链路降级但要继续可用，这比整个问答挂掉强。
"""

from __future__ import annotations

import asyncio
import threading
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import logger


class CrossEncoderReranker:
    def __init__(self, model_name: str | None = None, *, max_length: int = 1024) -> None:
        self.model_name = model_name or settings.RERANKER_MODEL
        self.max_length = max_length
        self._model: Any = None
        self._lock = threading.Lock()
        self._unavailable = False

    @property
    def model(self) -> Any | None:
        if self._unavailable:
            return None
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import CrossEncoder

                        self._model = CrossEncoder(self.model_name, max_length=self.max_length)
                        logger.info("已加载重排模型 {}", self.model_name)
                    except Exception as exc:  # noqa: BLE001
                        # 只降级一次，不要每个请求都重试加载
                        self._unavailable = True
                        logger.warning("重排模型不可用，跳过重排（保留 RRF 顺序）：{}", exc)
                        return None
        return self._model

    def rerank(self, query: str, chunks: list[Any], *, top_k: int | None = None) -> list[Any]:
        model = self.model
        if model is None or not chunks:
            return chunks
        pairs = [(query, c.content) for c in chunks]
        with self._lock:
            scores = model.predict(pairs, batch_size=16, show_progress_bar=False)
        for chunk, score in zip(chunks, scores, strict=False):
            chunk.rerank_score = float(score)
        ordered = sorted(chunks, key=lambda c: c.rerank_score or 0.0, reverse=True)
        return ordered[:top_k] if top_k else ordered

    async def arerank(self, query: str, chunks: list[Any], *, top_k: int | None = None) -> list[Any]:
        return await asyncio.to_thread(self.rerank, query, chunks, top_k=top_k)

    def health(self) -> tuple[bool, str]:
        if self._unavailable:
            return False, "重排模型不可用（已降级跳过）"
        if not settings.RERANKER_ENABLED:
            return True, "重排已关闭（RERANKER_ENABLED=false）"
        return True, f"模型 {self.model_name}"


@lru_cache
def get_reranker() -> CrossEncoderReranker:
    return CrossEncoderReranker()
