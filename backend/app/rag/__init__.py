"""RAG 层：混合检索 + RRF 融合 + CRAG 三级降级 + 溯源引擎。"""

from app.rag.crag import CorrectiveRAG, CRAGResult, GradeResult, Verdict, grade, lexical_coverage
from app.rag.fusion import (
    DENSE_SOURCE,
    SPARSE_SOURCE,
    dedupe_by_id,
    min_max_normalize,
    reciprocal_rank_fusion,
    rrf_fuse,
)
from app.rag.query_rewrite import decompose_query, hyde_query, rewrite_query
from app.rag.reranker import CrossEncoderReranker, get_reranker
from app.rag.retriever import HybridRetriever, RetrievedChunk, build_paper_filter, to_context_block
from app.rag.source_tracing import (
    Citation,
    SourceTrace,
    SourceTracer,
    TraceReport,
    content_terms,
    get_tracer,
    term_grounding_ratio,
)

__all__ = [
    "HybridRetriever",
    "RetrievedChunk",
    "to_context_block",
    "build_paper_filter",
    "rrf_fuse",
    "reciprocal_rank_fusion",
    "dedupe_by_id",
    "min_max_normalize",
    "DENSE_SOURCE",
    "SPARSE_SOURCE",
    "CrossEncoderReranker",
    "get_reranker",
    "CorrectiveRAG",
    "CRAGResult",
    "GradeResult",
    "Verdict",
    "grade",
    "lexical_coverage",
    "rewrite_query",
    "decompose_query",
    "hyde_query",
    "SourceTracer",
    "SourceTrace",
    "TraceReport",
    "Citation",
    "get_tracer",
    "content_terms",
    "term_grounding_ratio",
]
