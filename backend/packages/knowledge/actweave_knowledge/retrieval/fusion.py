"""Pure ranking math of the retrieval pipeline (design §8.2 / §8.3).

Candidate budgets, the reranker input cap, the per-base reciprocal-rank merge
of the semantic and lexical routes, shared-place (``RANK``) semantics, the
three-branch final fusion, and the per-base relative cutoff. Every function is
deterministic over its inputs so the ordering contract can be unit-tested
without PostgreSQL or a Provider.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from ..contracts import (
    KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET,
    KNOWLEDGE_RERANK_CANDIDATE_BUDGET,
    KnowledgeRecallRoute,
)
from .candidates import BaseDefaults, Candidate, RankedCandidate

__all__ = [
    "apply_relative_cutoffs",
    "calculate_candidate_k",
    "calculate_per_base_budget",
    "candidate_sort_key",
    "identity_key",
    "merge_recall_routes",
    "rank_fused",
    "rank_fusion_score",
    "rerank_input",
    "rerank_input_cap",
    "route_rrf_value",
    "shared_place_ranks",
    "stable_sort_key",
]

_CANDIDATE_K_FLOOR = 20
_CANDIDATE_K_CEILING = 100


def calculate_candidate_k(top_k: int) -> int:
    """Recall scale ``B = min(100, max(20, top_k * 5))`` (design §8.2)."""

    return min(_CANDIDATE_K_CEILING, max(_CANDIDATE_K_FLOOR, top_k * 5))


def calculate_per_base_budget(top_k: int, target_base_count: int) -> int:
    """Per-base per-route budget ``C = min(B, floor(G/N))`` (design §8.2).

    ``0`` (more target bases than the global parent budget) means the search
    must be rejected with an explicit narrowing hint, never silently truncated.
    """

    return min(
        calculate_candidate_k(top_k),
        KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET // target_base_count,
    )


def rerank_input_cap(top_k: int, base_count: int) -> int:
    """Per-base reranker input ``max(top_k, ceil(min(100, 10*top_k) / bases))``."""

    budget = min(KNOWLEDGE_RERANK_CANDIDATE_BUDGET, 10 * top_k)
    return max(top_k, -(-budget // max(base_count, 1)))


def rerank_input(candidates: list[Candidate], *, top_k: int) -> list[Candidate]:
    """Keep each base's best recall places within the group's rerank budget."""

    base_count = len({candidate.knowledge_base_id for candidate in candidates})
    cap = rerank_input_cap(top_k, base_count)
    return [candidate for candidate in candidates if candidate.recall_rank <= cap]


def rank_fusion_score(domain_rank: int, lexical_rank: int | None = None) -> float:
    """Design §8.3: ``61/2 * (1/(60+domain_rank) + 1/(60+lexical_rank))``.

    A candidate without a positive lexical score has no lexical rank and its
    second term is 0, capping the fused score at 0.5; both ranks at 1 give
    exactly 1.0.
    """

    lexical_term = 1.0 / (60.0 + lexical_rank) if lexical_rank is not None else 0.0
    return 61.0 / 2.0 * (1.0 / (60.0 + domain_rank) + lexical_term)


def route_rrf_value(rank: int) -> float:
    """One route's contribution to the per-base recall merge (design §8.2)."""

    return 1.0 / (60.0 + rank)


def stable_sort_key(item: RankedCandidate) -> tuple[float, float, UUID, UUID, int, UUID]:
    candidate = item.candidate
    return (
        -item.final_score,
        -candidate.vector_score,
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def rank_fused(ranked: list[RankedCandidate], lexical_ranks: dict[UUID, int]) -> list[tuple[RankedCandidate, float]]:
    """Fusion branch: RANK inside each domain, plus the global lexical rank.

    Equal native scores share a place (``1, 1, 3`` — never row_number), so
    equal evidence keeps an equal fused score; resource identity only orders
    fused ties, it never manufactures a difference. The vector score is
    deliberately absent from the final key: comparing raw scores across
    domains is exactly what fusion avoids. ``lexical_ranks`` carries the
    shared-place rank of every shortlisted parent with a positive lexical
    score (empty without lexical evidence, making the second term 0).
    """

    by_domain: dict[str, list[RankedCandidate]] = {}
    for item in ranked:
        by_domain.setdefault(item.score_domain, []).append(item)
    fused: list[tuple[RankedCandidate, float]] = []
    for items in by_domain.values():
        items.sort(key=stable_sort_key)
        rank = 0
        previous_score: float | None = None
        for index, item in enumerate(items, start=1):
            if previous_score is None or item.final_score != previous_score:
                rank = index
                previous_score = item.final_score
            fused.append((item, rank_fusion_score(rank, lexical_ranks.get(item.candidate.segment_id))))

    def _fused_key(entry: tuple[RankedCandidate, float]) -> tuple[float, UUID, UUID, int, UUID]:
        candidate = entry[0].candidate
        return (
            -entry[1],
            candidate.knowledge_base_id,
            candidate.document_id,
            candidate.position,
            candidate.segment_id,
        )

    fused.sort(key=_fused_key)
    return fused


def candidate_sort_key(candidate: Candidate) -> tuple[float, UUID, UUID, int, UUID]:
    return (
        -candidate.vector_score,
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def identity_key(candidate: Candidate) -> tuple[UUID, UUID, int, UUID]:
    return (
        candidate.knowledge_base_id,
        candidate.document_id,
        candidate.position,
        candidate.segment_id,
    )


def merge_recall_routes(
    semantic_pool: list[Candidate],
    lexical_pool: list[tuple[Candidate, float]],
    cap: int,
) -> list[Candidate]:
    """One hybrid base's recall merge: ``Σ 1/(60+rank)``, then keep ``C``.

    Each route ranks with shared places (``RANK``: equal scores share, never
    row_number) — semantic by cosine, lexical by ``ts_rank_cd`` — and a parent
    missing from a route simply contributes 0 for it. Identity only breaks
    ties in the final merged order.
    """

    rrf: dict[UUID, float] = {}
    candidates: dict[UUID, Candidate] = {}

    def _accumulate(entries: list[tuple[float, Candidate]], route: KnowledgeRecallRoute) -> None:
        rank = 0
        previous: float | None = None
        for index, (score, candidate) in enumerate(entries, start=1):
            if previous is None or score != previous:
                rank = index
                previous = score
            rrf[candidate.segment_id] = rrf.get(candidate.segment_id, 0.0) + route_rrf_value(rank)
            known = candidates.get(candidate.segment_id)
            if known is None:
                candidates[candidate.segment_id] = replace(candidate, recall_routes=candidate.recall_routes | {route})
            else:
                # The semantic candidate keeps its attribution; both routes
                # stay on record so thresholds can honor the lexical evidence.
                candidates[candidate.segment_id] = replace(known, recall_routes=known.recall_routes | candidate.recall_routes | {route})

    _accumulate(sorted(((candidate.vector_score, candidate) for candidate in semantic_pool), key=lambda entry: (-entry[0], *identity_key(entry[1]))), "semantic")
    _accumulate(sorted(((score, candidate) for candidate, score in lexical_pool), key=lambda entry: (-entry[0], *identity_key(entry[1]))), "lexical")

    ordered = sorted(candidates.values(), key=lambda candidate: (-rrf[candidate.segment_id], *identity_key(candidate)))
    return ordered[:cap]


def shared_place_ranks(scores: dict[UUID, float]) -> dict[UUID, int]:
    """``RANK`` semantics over positive scores: equal scores share a place."""

    ordered = sorted(scores.items(), key=lambda entry: (-entry[1], entry[0]))
    ranks: dict[UUID, int] = {}
    rank = 0
    previous: float | None = None
    for index, (segment_id, score) in enumerate(ordered, start=1):
        if previous is None or score != previous:
            rank = index
            previous = score
        ranks[segment_id] = rank
    return ranks


def apply_relative_cutoffs(
    group_ranked: list[RankedCandidate],
    defaults: dict[UUID, BaseDefaults],
) -> tuple[list[RankedCandidate], int]:
    """Per-base relative cut on native scores, after the absolute threshold.

    A base with a cutoff ``c`` keeps candidates scoring at least ``c`` times
    its best native score in this group; the cut never applies when that best
    score is not positive, and lexical evidence exempts a rerank-free
    candidate exactly as it does for the absolute threshold. Returns the kept
    items in their original order plus the number dropped.
    """

    best: dict[UUID, float] = {}
    for item in group_ranked:
        base_id = item.candidate.knowledge_base_id
        if defaults[base_id].relative_cutoff is not None:
            best[base_id] = max(best.get(base_id, item.final_score), item.final_score)
    kept: list[RankedCandidate] = []
    dropped = 0
    for item in group_ranked:
        base_id = item.candidate.knowledge_base_id
        cutoff = defaults[base_id].relative_cutoff
        top = best.get(base_id)
        if cutoff is not None and top is not None and top > 0 and item.final_score < cutoff * top:
            if not (item.local_score_kind == "cosine" and "lexical" in item.candidate.recall_routes):
                dropped += 1
                continue
        kept.append(item)
    return kept, dropped
