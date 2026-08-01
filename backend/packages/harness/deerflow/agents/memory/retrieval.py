"""Deterministic lexical retrieval over one authorized project Memory snapshot.

PostgreSQL remains the source of truth. This module owns no files, cache, index,
scope, or identity; callers must first obtain one exact authorized snapshot.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_QUERY_MAX_CHARS = 1_000
_CATEGORY_MAX_CHARS = 32
_MAX_TOP_K = 20
_CONFIDENCE_WEIGHT = 0.2
_WORD_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_cjk(character: str) -> bool:
    return "\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff" or "\u3040" <= character <= "\u30ff" or "\uac00" <= character <= "\ud7a3"


def _tokenize(value: str) -> list[str]:
    """Return Unicode word tokens plus CJK character/bigram recall tokens."""

    tokens: list[str] = []
    for token in _WORD_RE.findall(_normalize_text(value)):
        if not token:
            continue
        tokens.append(token)
        if "-" in token:
            tokens.extend(part for part in token.split("-") if part)

        cjk_run: list[str] = []
        for character in token:
            if _is_cjk(character):
                cjk_run.append(character)
                continue
            if cjk_run:
                tokens.extend(cjk_run)
                tokens.extend("".join(cjk_run[index : index + 2]) for index in range(len(cjk_run) - 1))
                cjk_run.clear()
        if cjk_run:
            tokens.extend(cjk_run)
            tokens.extend("".join(cjk_run[index : index + 2]) for index in range(len(cjk_run) - 1))
    return tokens


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.5
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(result):
        return 0.5
    return max(0.0, min(1.0, result))


def _created_at(value: object) -> tuple[str | None, datetime | None]:
    if not isinstance(value, str) or not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return value, parsed.astimezone(UTC)


def _validate_inputs(
    query: str,
    *,
    category: str | None,
    top_k: int,
) -> tuple[str, str | None]:
    if not isinstance(query, str):
        raise ValueError("memory search query must be a string")
    query = query.strip()
    if not query or len(query) > _QUERY_MAX_CHARS:
        raise ValueError("memory search query is invalid")
    if type(top_k) is not int or not 1 <= top_k <= _MAX_TOP_K:
        raise ValueError("memory search top_k is invalid")
    if category is not None:
        if not isinstance(category, str):
            raise ValueError("memory search category is invalid")
        category = category.strip()
        if not category or len(category) > _CATEGORY_MAX_CHARS:
            raise ValueError("memory search category is invalid")
    return query, category


def _candidate(fact: object, *, category: str | None) -> dict[str, Any] | None:
    if not isinstance(fact, Mapping):
        return None
    raw_id = fact.get("id")
    raw_content = fact.get("content")
    if raw_id is None or not isinstance(raw_content, str):
        return None
    fact_id = str(raw_id).strip()
    content = raw_content.strip()
    if not fact_id or not content:
        return None
    raw_category = fact.get("category", "context")
    fact_category = raw_category.strip() if isinstance(raw_category, str) else "context"
    fact_category = fact_category or "context"
    if category is not None and fact_category != category:
        return None
    created_at, parsed_created_at = _created_at(fact.get("createdAt"))
    normalized = _normalize_text(content)
    tokens = _tokenize(content)
    if not tokens:
        return None
    return {
        "id": fact_id,
        "content": content,
        "category": fact_category,
        "confidence": _confidence(fact.get("confidence")),
        "createdAt": created_at,
        "_created_at": parsed_created_at,
        "_normalized": normalized,
        "_tokens": tokens,
        "_counts": Counter(tokens),
    }


def rank_project_memory_facts(
    facts: Sequence[object],
    query: str,
    *,
    category: str | None = None,
    top_k: int = 5,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rank facts from a single already-authorized snapshot.

    Category filtering happens before document-frequency and top-k calculation.
    Confidence and age can reorder textual matches but can never make an
    unrelated fact appear in the result set.
    """

    query, category = _validate_inputs(query, category=category, top_k=top_k)
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes, bytearray)):
        raise ValueError("memory facts must be a sequence")
    candidates = [candidate for fact in facts if (candidate := _candidate(fact, category=category)) is not None]
    if not candidates:
        return []

    query_normalized = _normalize_text(query)
    query_terms = tuple(dict.fromkeys(_tokenize(query)))
    if not query_terms:
        return []

    document_count = len(candidates)
    average_length = sum(len(candidate["_tokens"]) for candidate in candidates) / document_count
    document_frequency = {term: sum(1 for candidate in candidates if term in candidate["_counts"]) for term in query_terms}
    effective_now = now or datetime.now(UTC)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=UTC)
    effective_now = effective_now.astimezone(UTC)

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        counts: Counter[str] = candidate["_counts"]
        matched_terms = [term for term in query_terms if counts.get(term, 0)]
        exact_match = query_normalized in candidate["_normalized"]
        if not matched_terms and not exact_match:
            continue

        document_length = len(candidate["_tokens"])
        relevance = 0.0
        for term in matched_terms:
            frequency = counts[term]
            inverse_frequency = math.log(1.0 + (document_count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + 1.2 * (1.0 - 0.75 + 0.75 * document_length / max(average_length, 1.0))
            relevance += inverse_frequency * (frequency * 2.2) / denominator
        if exact_match:
            relevance += 2.0

        time_decay = 1.0
        parsed_created_at = candidate["_created_at"]
        if isinstance(parsed_created_at, datetime):
            age_days = max(0, (effective_now - parsed_created_at).days)
            if age_days >= 30:
                time_decay = math.exp(-0.01 * (age_days - 30))
        score = relevance * time_decay + candidate["confidence"] * _CONFIDENCE_WEIGHT
        ranked.append(
            {
                "id": candidate["id"],
                "content": candidate["content"],
                "category": candidate["category"],
                "confidence": candidate["confidence"],
                "createdAt": candidate["createdAt"],
                "score": round(score, 6),
                "matchType": "exact" if exact_match else "lexical",
            }
        )

    ranked.sort(
        key=lambda result: (
            -result["score"],
            str(result.get("createdAt") or ""),
            result["id"],
        )
    )
    return ranked[:top_k]


__all__ = ["rank_project_memory_facts"]
