"""Search-internal value types shared by the retrieval stages.

``KnowledgeSearchService`` orchestrates transactions and Provider calls;
these frozen dataclasses carry the validated request, the per-base effective
defaults, the strategy snapshot, one recall candidate, and the recall outcome
between its stages. Nothing here touches the database or a Provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..contracts import (
    KnowledgeEmbeddingMaterial,
    KnowledgeLocalScoreKind,
    KnowledgeMatchedChild,
    KnowledgeMatchedVia,
    KnowledgeMetadataFilter,
    KnowledgeRecallRoute,
    KnowledgeRerankMaterial,
    KnowledgeRetrievalMode,
)

__all__ = [
    "BaseDefaults",
    "Candidate",
    "RankedCandidate",
    "RecallOutcome",
    "SearchGroup",
    "SearchSnapshot",
    "ValidatedSearch",
    "effective_defaults",
]


@dataclass(frozen=True, slots=True)
class ValidatedSearch:
    """Range-checked request values; ``None`` means "use the base defaults"."""

    query: str
    top_k: int | None
    score_threshold: float | None
    metadata_filters: tuple[KnowledgeMetadataFilter, ...]
    retrieval_mode: KnowledgeRetrievalMode | None
    relative_score_cutoff: float | None = None


@dataclass(frozen=True, slots=True)
class BaseDefaults:
    top_k: int
    score_threshold: float
    retrieval_mode: KnowledgeRetrievalMode
    summary_index_enabled: bool
    relative_cutoff: float | None = None


def effective_defaults(defaults: BaseDefaults, overrides: ValidatedSearch) -> BaseDefaults:
    return BaseDefaults(
        top_k=overrides.top_k if overrides.top_k is not None else defaults.top_k,
        score_threshold=overrides.score_threshold if overrides.score_threshold is not None else defaults.score_threshold,
        retrieval_mode=overrides.retrieval_mode if overrides.retrieval_mode is not None else defaults.retrieval_mode,
        summary_index_enabled=defaults.summary_index_enabled,
        relative_cutoff=overrides.relative_score_cutoff if overrides.relative_score_cutoff is not None else defaults.relative_cutoff,
    )


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    model_bindings: dict[UUID, tuple[UUID, UUID | None]]
    effective_defaults: dict[UUID, BaseDefaults]
    overrides: ValidatedSearch


@dataclass(frozen=True, slots=True)
class SearchGroup:
    """Bases sharing one ``(embedding model, reranker model)`` pair.

    ``rerank`` is ``None`` for the NULL-reranker group: its candidates keep
    their cosine similarity as the final score.
    """

    embedding: KnowledgeEmbeddingMaterial
    rerank: KnowledgeRerankMaterial | None
    base_ids: list[UUID]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One recalled segment with its display fields and recall-stage score.

    ``vector_score`` is the maximum Segment/Child/Summary source cosine;
    ``matched_via`` identifies its source. ``content`` is the complete parent
    text frozen by the recall snapshot; ``index_text`` is the corresponding
    persisted model input used only by the reranker. Hits carry ``content`` as
    the passage while citations only quote its head. ``matched_children``
    are the really-recalled child chunks, carried by the recall transaction
    itself (empty for general-mode segments).
    """

    segment_id: UUID
    position: int
    content: str
    index_text: str
    source_position: dict[str, Any]
    document_id: UUID
    document_name: str
    document_version: int
    knowledge_base_id: UUID
    knowledge_base_name: str
    vector_score: float
    matched_children: tuple[KnowledgeMatchedChild, ...] = ()
    matched_via: KnowledgeMatchedVia = "segment"
    # Which recall routes actually surfaced this parent. Lexical evidence
    # exempts a rerank-free candidate from the cosine threshold: an exact
    # identifier match is the reason hybrid exists, and its cosine says
    # nothing about that match.
    recall_routes: frozenset[KnowledgeRecallRoute] = frozenset({"semantic"})
    # 1-based place inside its base's recall order (RRF for hybrid bases,
    # cosine otherwise); the reranker input cut keeps the best places.
    recall_rank: int = 0


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate with its native score and score-domain provenance."""

    final_score: float
    local_score_kind: KnowledgeLocalScoreKind
    score_domain: str
    candidate: Candidate


@dataclass(frozen=True, slots=True)
class RecallOutcome:
    """Everything one recall transaction produced for the whole search.

    ``candidates_by_group`` keys by ``id(group)``; ``lexical_scores`` carries
    the fusion evidence (``ts_rank_cd`` of every recalled parent against the
    same lexical query) so the final ordering needs no further transaction.
    """

    candidates_by_group: dict[int, list[Candidate]]
    lexical_scores: dict[UUID, float]
    semantic_count: int
    lexical_count: int
    summary_count: int
