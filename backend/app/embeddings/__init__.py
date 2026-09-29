"""Embedding 层：bge-m3 Dense + Sparse。"""

from app.embeddings.bge_m3 import (
    DENSE_DIM,
    BGEM3Embedder,
    EmbeddingResult,
    get_embedder,
    normalize_dense,
    top_weights,
)

__all__ = [
    "DENSE_DIM",
    "BGEM3Embedder",
    "EmbeddingResult",
    "get_embedder",
    "normalize_dense",
    "top_weights",
]
