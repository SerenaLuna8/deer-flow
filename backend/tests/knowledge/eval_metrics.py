"""Pure retrieval-quality metrics for the M10 T14 evaluation.

Formulas are taken from the M10 design §11.2 and are independent of the
search implementation: expected values in tests are worked examples, not
recomputations of production ranking.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

GRADE_TARGET = 2
NDCG_K = 10


def gain(grade: int) -> float:
    """``2^grade - 1``; grade 2 → 3, grade 1 → 1, grade 0 → 0."""

    if grade < 0:
        raise ValueError("grade must be non-negative")
    return float((1 << grade) - 1)


def dcg_at_k(grades_in_rank_order: Sequence[int], k: int = NDCG_K) -> float:
    """DCG with discount ``log2(rank + 1)``; rank is 1-based."""

    total = 0.0
    for index, grade in enumerate(grades_in_rank_order[:k], start=1):
        total += gain(grade) / math.log2(index + 1)
    return total


def idcg_at_k(grades: Sequence[int], k: int = NDCG_K) -> float:
    ideal = sorted((grade for grade in grades if grade > 0), reverse=True)
    return dcg_at_k(ideal, k)


def ndcg_at_k(
    judgments: Mapping[str, int],
    ranked_ids: Sequence[str],
    k: int = NDCG_K,
) -> float | None:
    """nDCG@k, or ``None`` when IDCG=0 (no-answer items).

    Unjudged ranked ids contribute grade 0. Missing target ids still
    participate in IDCG so a miss is punished.
    """

    ideal_grades = [grade for grade in judgments.values() if grade > 0]
    ideal = idcg_at_k(ideal_grades, k)
    if ideal == 0.0:
        return None
    ranked_grades = [judgments.get(item_id, 0) for item_id in ranked_ids[:k]]
    return dcg_at_k(ranked_grades, k) / ideal


def recall_hit(target_ids: Sequence[str], retrieved_ids: Sequence[str]) -> bool:
    """True when any grade-2 target appears in the retrieved identity list."""

    retrieved = set(retrieved_ids)
    return any(item_id in retrieved for item_id in target_ids)


def reciprocal_rank_at_k(
    target_ids: Sequence[str],
    retrieved_ids: Sequence[str],
    k: int = 5,
) -> float:
    """Reciprocal rank of the first target in the first ``k`` results."""

    targets = set(target_ids)
    for rank, item_id in enumerate(retrieved_ids[:k], start=1):
        if item_id in targets:
            return 1.0 / rank
    return 0.0


def mean_or_none(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def false_recall_rate(returned_counts: Sequence[int]) -> float:
    """Share of no-answer queries that returned at least one hit."""

    if not returned_counts:
        return 0.0
    return sum(1 for count in returned_counts if count > 0) / len(returned_counts)
