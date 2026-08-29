"""Two-stage retrieval internals (cosine recall + rerank)."""

from .service import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    SNIPPET_MAX_CHARS,
    KnowledgeSearchService,
    calculate_candidate_k,
)

__all__ = [
    "DEFAULT_SCORE_THRESHOLD",
    "DEFAULT_TOP_K",
    "MAX_QUERY_CHARS",
    "MAX_TOP_K",
    "SNIPPET_MAX_CHARS",
    "KnowledgeSearchService",
    "calculate_candidate_k",
]
