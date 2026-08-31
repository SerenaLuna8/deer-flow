"""Two-stage retrieval internals (cosine recall + rerank + lexical)."""

from .lexical import (
    LEXICAL_INDEX_INPUT_MAX_BYTES,
    LEXICAL_TOKEN_ENCODED_MAX_BYTES,
    encode_lexical_token,
    lexical_index_input,
    lexical_query_input,
    lexical_v1_tokens,
)
from .service import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    SNIPPET_MAX_CHARS,
    KnowledgeSearchService,
    calculate_candidate_k,
    calculate_per_base_budget,
)

__all__ = [
    "DEFAULT_SCORE_THRESHOLD",
    "DEFAULT_TOP_K",
    "LEXICAL_INDEX_INPUT_MAX_BYTES",
    "LEXICAL_TOKEN_ENCODED_MAX_BYTES",
    "MAX_QUERY_CHARS",
    "MAX_TOP_K",
    "SNIPPET_MAX_CHARS",
    "KnowledgeSearchService",
    "calculate_candidate_k",
    "calculate_per_base_budget",
    "encode_lexical_token",
    "lexical_index_input",
    "lexical_query_input",
    "lexical_v1_tokens",
]
