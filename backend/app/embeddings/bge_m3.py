"""bge-m3 双向量 Embedding（Dense dim=1024 + Sparse）。

为什么不能只用 sentence-transformers
------------------------------------
bge-m3 的**稀疏向量是模型学出来的**（checkpoint 里有一个 `sparse_linear` 头，
属于 learned sparse / SPLADE 家族），`sentence_transformers` 只暴露 dense 池化输出，
拿不到稀疏头。所以主路径走 BAAI 官方封装的 `FlagEmbedding.BGEM3FlagModel`，
它一次前向同时返回 dense、sparse、colbert 三种表示。

加载走 ModelScope（国内可直连）：`snapshot_download` 把权重拉到本地缓存目录，
再把**本地路径**交给 BGEM3FlagModel —— 这样容器重建不会重复下载约 2GB 权重。

降级路径
--------
没装 FlagEmbedding 时退化为 `LexicalSparseEmbedder`：用 tokenizer 词频做 BM25 风格
词法权重当稀疏向量。**这不是 bge-m3 稀疏**，只是让链路在没有重依赖时也能跑通
（离线测试、CI）。会在日志里明确警告，避免被误当成正式效果。
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logging import logger

DENSE_DIM = 1024


@dataclass(slots=True)
class EmbeddingResult:
    dense: list[list[float]] = field(default_factory=list)
    sparse: list[dict[int, float]] = field(default_factory=list)
    backend: str = "unknown"

    def __len__(self) -> int:
        return len(self.dense)


def normalize_dense(vec: list[float]) -> list[float]:
    """L2 归一化。用 COSINE 度量时等价，但归一化后两种度量都不会因模长漂移而失真。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]


def top_weights(sparse: dict[int, float], top_n: int = 256) -> dict[int, float]:
    """截断稀疏向量，只保留权重最高的 top_n 个 token。

    控制检索开销：Milvus 的倒排扫描成本随非零项线性增长，而尾部 token 对得分贡献极小。
    """
    if len(sparse) <= top_n:
        return dict(sparse)
    ordered = sorted(sparse.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return dict(ordered)


class LexicalSparseEmbedder:
    """降级用：词频权重稀疏向量（非 bge-m3 稀疏）。"""

    def __init__(self, model_name: str) -> None:
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._df: Counter[int] = Counter()
        self._docs = 0

    def _weights(self, text: str) -> dict[int, float]:
        ids = self.tokenizer.encode(text, add_special_tokens=False)
        tf = Counter(ids)
        self._docs += 1
        self._df.update(tf.keys())
        out: dict[int, float] = {}
        for token_id, count in tf.items():
            idf = math.log(1 + (self._docs + 1) / (self._df[token_id] + 1))
            out[int(token_id)] = float(math.log1p(count) * idf)
        return top_weights(out)

    def encode(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(dense=[], sparse=[self._weights(t) for t in texts], backend="lexical-fallback")


class BGEM3Embedder:
    """主路径：bge-m3 dense + learned sparse。线程安全（推理加锁，避免并发抢占显存）。"""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        max_length: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.device = device or settings.EMBEDDING_DEVICE
        self.max_length = max_length or settings.EMBEDDING_MAX_LENGTH
        self.batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._lock = threading.Lock()
        self._model: Any = None
        self._fallback: LexicalSparseEmbedder | None = None

    # ------------------------------------------------------------ 加载
    def _resolve_local_path(self) -> str:
        """优先 ModelScope 拉本地快照；失败再交给 HuggingFace 自己解析。"""
        if settings.EMBEDDING_BACKEND != "modelscope":
            return self.model_name
        try:
            from modelscope import snapshot_download

            t0 = time.time()
            path = snapshot_download(
                self.model_name,
                cache_dir=settings.MODELSCOPE_CACHE or None,
            )
            logger.info("bge-m3 权重就绪（{:.1f}s）: {}", time.time() - t0, path)
            return str(path)
        except Exception as exc:  # noqa: BLE001 —— 网络问题不该让服务起不来
            logger.warning("ModelScope 拉取失败，回退 HuggingFace 解析：{}", exc)
            return self.model_name

    @property
    def model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
        return self._model

    def _load(self) -> Any:
        path = self._resolve_local_path()
        try:
            from FlagEmbedding import BGEM3FlagModel

            m = BGEM3FlagModel(path, use_fp16=(self.device != "cpu"))
            logger.info("已加载 bge-m3（FlagEmbedding, device={}）", self.device)
            return m
        except ImportError:
            logger.warning(
                "未安装 FlagEmbedding，稀疏向量降级为词法权重（非 bge-m3 稀疏）。"
                "安装后即自动启用 learned sparse：pip install FlagEmbedding"
            )
            self._fallback = LexicalSparseEmbedder(path)
            return self._fallback

    @property
    def is_fallback(self) -> bool:
        return isinstance(self.model, LexicalSparseEmbedder)

    # ------------------------------------------------------------ 编码
    def encode(self, texts: list[str], *, top_n: int = 256) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult()
        model = self.model

        if isinstance(model, LexicalSparseEmbedder):
            return model.encode(texts)

        with self._lock:
            out = model.encode(
                texts,
                batch_size=self.batch_size,
                max_length=self.max_length,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        dense = [normalize_dense(list(v)) for v in out["dense_vecs"]]
        sparse = [top_weights({int(k): float(w) for k, w in d.items()}, top_n) for d in out["lexical_weights"]]
        return EmbeddingResult(dense=dense, sparse=sparse, backend="bge-m3")

    def encode_query(self, query: str, *, top_n: int = 256) -> EmbeddingResult:
        return self.encode([query], top_n=top_n)

    def health(self) -> tuple[bool, str]:
        try:
            res = self.encode_query("health check")
            return (
                True,
                f"{res.backend}, dense_dim={len(res.dense[0]) if res.dense else 0}, sparse_terms={len(res.sparse[0]) if res.sparse else 0}",
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@lru_cache
def get_embedder() -> BGEM3Embedder:
    return BGEM3Embedder()
