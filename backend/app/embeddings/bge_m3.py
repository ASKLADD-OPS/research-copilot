"""bge-m3 双向量 Embedding（Dense dim=1024 + Sparse）。

三条加载路径，按可用性依次降级
------------------------------
1. **FlagEmbedding `BGEM3FlagModel`**（首选）：一次前向同时吐出 dense 与
   *learned sparse*。bge-m3 的稀疏向量是模型学出来的（checkpoint 里有个
   `sparse_linear` 头，属 SPLADE 家族），`sentence_transformers` 只暴露 dense
   池化输出，拿不到这个头 —— 所以主路径不能用 ST。
2. **ModelScope `pipeline(task=Tasks.text_embedding, model='BAAI/bge-m3')`**：
   规格指定的加载方式，权重同样来自魔塔社区。这条只给 dense；
   稀疏路降级为词法权重（会记 warning，别把它当成 bge-m3 稀疏）。
3. **词法兜底**：词频 + IDF 权重。让链路在没装重依赖的环境（CI / 离线）也能跑通。

三条路径都在日志里自报家门（`backend` 字段），因为"降级了但没人发现"是
这类模型封装最典型的静默事故。

加载走 ModelScope 的 `snapshot_download` 拿本地快照，再把**本地路径**交给模型
类 —— 这样容器重建不会重复下载约 2GB 权重。
"""

from __future__ import annotations

import asyncio
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

# 拉权重时排除的路径。bge-m3 的仓库里带了约 2.2GB 的 ONNX 导出（onnx/），
# 而 FlagEmbedding 与 ModelScope pipeline 走的都是 PyTorch —— 这份 ONNX
# 永远不会被加载。不排除它，每次冷启动就白多下 2GB。
MODELSCOPE_IGNORE_PATTERNS: tuple[str, ...] = (
    "onnx/*",
    "imgs/*",
    "*.onnx",
    "*.onnx_data",
    "*.h5",
    "*.tflite",
    "*.msgpack",
)


# ---------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class EmbeddingResult:
    """一次批量编码的结果。dense 与 sparse 一一对应（同下标 = 同一段文本）。"""

    dense: list[list[float]] = field(default_factory=list)
    sparse: list[dict[int, float]] = field(default_factory=list)
    backend: str = "unknown"

    def __len__(self) -> int:
        return len(self.dense)

    def __iter__(self):  # 兼容 `for d, s in result` 的直觉写法
        yield from zip(self.dense, self.sparse, strict=False)


@dataclass(slots=True)
class HybridVector:
    """单条文本的混合向量。`encode_hybrid()` 返回它的列表。"""

    dense: list[float] = field(default_factory=list)
    sparse: dict[int, float] = field(default_factory=dict)


# ---------------------------------------------------------------- 纯函数工具
def normalize_dense(vec: list[float]) -> list[float]:
    """L2 归一化。用 COSINE 度量时等价，但归一化后换任何度量都不会因模长漂移失真。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]


def top_weights(sparse: dict[int, float], top_n: int = 256) -> dict[int, float]:
    """截断稀疏向量，只保留权重最高的 top_n 个 token。

    控制检索开销：Milvus 倒排扫描的成本随非零项个数线性增长，而尾部 token
    对最终得分贡献极小。256 是经验值（bge-m3 单句稀疏项通常在 100-400 之间）。
    """
    if len(sparse) <= top_n:
        return dict(sparse)
    return dict(sorted(sparse.items(), key=lambda kv: kv[1], reverse=True)[:top_n])


def detect_device(preference: str | None = None) -> str:
    """GPU/CPU 自动切换。`auto` 时探一次 CUDA，探不到就 CPU。"""
    pref = (preference or settings.EMBEDDING_DEVICE or "auto").lower()
    if pref != "auto":
        return pref
    try:
        import torch

        if torch.cuda.is_available():
            logger.info("检测到 CUDA，Embedding 走 GPU")
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - 没装 torch 就只能是 CPU
        pass
    return "cpu"


# ---------------------------------------------------------------- 降级实现
class LexicalSparseEmbedder:
    """降级用：词频 × IDF 权重（**非** bge-m3 稀疏），且不出 dense 向量。"""

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
        return top_weights(out, settings.EMBEDDING_SPARSE_TOP_N)

    def encode(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(dense=[], sparse=[self._weights(t) for t in texts], backend="lexical-fallback")


# ---------------------------------------------------------------- 主实现
class BGEM3Embedder:
    """bge-m3 封装。**单例 + 懒加载 + 线程安全**。

    推理加锁是必需的：BGEM3FlagModel 的 forward 在 GPU 上不是并发安全的，
    两个请求同时进来会争显存；CPU 上也只是白白多线程切换。
    """

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        max_length: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.max_length = max_length or settings.EMBEDDING_MAX_LENGTH
        self.batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE
        self._device_pref = device or settings.EMBEDDING_DEVICE
        self._device: str | None = None
        self._lock = threading.Lock()
        self._model: Any = None

    # ------------------------------------------------------------ 加载
    @property
    def device(self) -> str:
        """首次访问时才探设备 —— 这样纯构造对象不会触发 torch import。"""
        if self._device is None:
            self._device = detect_device(self._device_pref)
        return self._device

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
                ignore_patterns=list(MODELSCOPE_IGNORE_PATTERNS),
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

        # 路径 1：FlagEmbedding —— dense + learned sparse 一起拿
        try:
            from FlagEmbedding import BGEM3FlagModel

            model = BGEM3FlagModel(path, use_fp16=(self.device != "cpu"))
            logger.info("已加载 bge-m3（FlagEmbedding, device={}）", self.device)
            return model
        except ImportError:
            logger.warning("未安装 FlagEmbedding，尝试 ModelScope pipeline（只有 dense）")

        # 路径 2：ModelScope pipeline（规格指定的加载方式，dense only）
        try:
            from modelscope.pipelines import pipeline
            from modelscope.utils.constant import Tasks

            ms = pipeline(task=Tasks.text_embedding, model=self.model_name, device=self.device)
            logger.warning(
                "已加载 bge-m3（ModelScope pipeline, device={}）—— 稀疏路退化为词法权重；"
                "要启用 learned sparse 请安装 FlagEmbedding",
                self.device,
            )
            return _ModelScopeDense(ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ModelScope pipeline 不可用（{}），稀疏降级为词法权重", exc)

        # 路径 3：纯词法
        return LexicalSparseEmbedder(path)

    @property
    def backend(self) -> str:
        if self._model is None:
            return "not-loaded"
        if isinstance(self._model, LexicalSparseEmbedder):
            return "lexical-fallback"
        if isinstance(self._model, _ModelScopeDense):
            return "modelscope-pipeline"
        return "bge-m3"

    # ------------------------------------------------------------ 编码
    def encode(self, texts: list[str], *, top_n: int | None = None) -> EmbeddingResult:
        """一次性拿到 dense + sparse（内部只前向一次）。"""
        if not texts:
            return EmbeddingResult()
        top_n = top_n or settings.EMBEDDING_SPARSE_TOP_N
        model = self.model

        if isinstance(model, LexicalSparseEmbedder):
            return model.encode(texts)

        if isinstance(model, _ModelScopeDense):
            dense = [normalize_dense(list(v)) for v in model.encode(texts)]
            sparse = LexicalSparseEmbedder(self._resolve_local_path()).encode(texts).sparse
            return EmbeddingResult(dense=dense, sparse=sparse, backend="modelscope-pipeline")

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

    # ---- 规格要求的三个入口 ----
    def encode_dense(self, texts: list[str]) -> list[list[float]]:
        """仅稠密向量。dim=1024。"""
        return self.encode(texts).dense

    def encode_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """仅稀疏向量（learned sparse；降级时为词法权重）。"""
        return self.encode(texts).sparse

    def encode_hybrid(self, texts: list[str]) -> list[HybridVector]:
        """稠密 + 稀疏一次拿全。混合检索（RRF 融合）用的就是它。"""
        res = self.encode(texts)
        return [HybridVector(dense=d, sparse=s) for d, s in zip(res.dense, res.sparse, strict=False)]

    def encode_query(self, query: str, *, top_n: int | None = None) -> EmbeddingResult:
        """查询侧编码。bge-m3 的查询与文档共用同一语义空间，无需加指令前缀。"""
        return self.encode([query], top_n=top_n)

    def health(self) -> tuple[bool, str]:
        try:
            res = self.encode_query("health check")
            dim = len(res.dense[0]) if res.dense else 0
            return True, f"{res.backend}, dense_dim={dim}, sparse_terms={len(res.sparse[0]) if res.sparse else 0}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


class _ModelScopeDense:
    """把 ModelScope pipeline 的输出对齐成 FlagEmbedding 的形状（只填 dense）。"""

    def __init__(self, pipe: Any) -> None:
        self._pipe = pipe

    def encode(self, texts: list[str]) -> list[list[float]]:
        out = self._pipe(texts)
        if isinstance(out, dict):
            for key in ("text_embedding", "embeddings", "output"):
                if key in out:
                    return [list(map(float, v)) for v in out[key]]
        return [list(map(float, v)) for v in out]


@lru_cache
def get_embedder() -> BGEM3Embedder:
    """进程内单例。模型加载几十秒，绝不允许多份副本各自占显存/内存。"""
    return BGEM3Embedder()


# ---------------------------------------------------------------- async 包装
# 规格要求 run_in_executor：模型推理是同步阻塞的，直接 await 会卡死事件循环。
# 用 run_in_executor（而不是 asyncio.to_thread）是为了让上层能换 executor ——
# 显存紧张时可以把嵌入压到单线程池，避免并发前向把 GPU 打满。


async def aencode_dense(texts: list[str]) -> list[list[float]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_embedder().encode_dense, texts)


async def aencode_sparse(texts: list[str]) -> list[dict[int, float]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_embedder().encode_sparse, texts)


async def aencode_hybrid(texts: list[str]) -> list[HybridVector]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_embedder().encode_hybrid, texts)


async def aencode_query(query: str) -> EmbeddingResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_embedder().encode_query, query)


async def ahealth() -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_embedder().health)


__all__ = [
    "DENSE_DIM",
    "MODELSCOPE_IGNORE_PATTERNS",
    "BGEM3Embedder",
    "EmbeddingResult",
    "HybridVector",
    "LexicalSparseEmbedder",
    "aencode_dense",
    "aencode_hybrid",
    "aencode_query",
    "aencode_sparse",
    "ahealth",
    "detect_device",
    "get_embedder",
    "normalize_dense",
    "top_weights",
]
