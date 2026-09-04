"""Query-vector cache contracts: immutable values, bounded reuse, no text normalization."""

from __future__ import annotations

from uuid import uuid4

import pytest
from actweave_knowledge.retrieval.query_cache import KnowledgeQueryEmbeddingCache


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_query_vectors_are_copied_into_immutable_values() -> None:
    cache = KnowledgeQueryEmbeddingCache(enabled=True, max_entries=16, ttl_seconds=300)
    model_id = uuid4()
    vector = [1.0, 0.0, 0.0]

    assert cache.get(model_id, "安装方法") is None
    cache.put(model_id, "安装方法", vector)
    vector[0] = -1.0

    assert cache.get(model_id, "安装方法") == (1.0, 0.0, 0.0)


def test_expiry_is_measured_from_write_and_a_hit_does_not_extend_it() -> None:
    clock = _Clock()
    cache = KnowledgeQueryEmbeddingCache(enabled=True, max_entries=16, ttl_seconds=5, clock=clock)
    model_id = uuid4()
    cache.put(model_id, "query", [1.0])

    clock.now = 4.999
    assert cache.get(model_id, "query") == (1.0,)
    clock.now = 5.0
    assert cache.get(model_id, "query") is None

    cache.put(model_id, "query", [2.0])
    clock.now = 9.999
    assert cache.get(model_id, "query") == (2.0,)
    clock.now = 10.0
    assert cache.get(model_id, "query") is None


def test_capacity_evicts_the_least_recently_read_or_written_query() -> None:
    cache = KnowledgeQueryEmbeddingCache(enabled=True, max_entries=16, ttl_seconds=300)
    model_id = uuid4()
    queries = [f"{index}:" + "中" * 2000 for index in range(17)]
    for index, query in enumerate(queries[:16]):
        cache.put(model_id, query, [float(index)])

    # Touching the oldest entry saves it from the next capacity eviction.
    assert cache.get(model_id, queries[0]) == (0.0,)
    cache.put(model_id, queries[16], [16.0])
    assert cache.get(model_id, queries[1]) is None
    assert cache.get(model_id, queries[0]) == (0.0,)

    # Replacing an existing entry makes it newest without consuming a slot.
    cache.put(model_id, queries[2], [22.0])
    cache.put(model_id, "another", [23.0])
    assert cache.get(model_id, queries[3]) is None
    assert cache.get(model_id, queries[2]) == (22.0,)


def test_disabled_cache_never_reuses_a_query() -> None:
    cache = KnowledgeQueryEmbeddingCache(enabled=False, max_entries=16, ttl_seconds=300)
    model_id = uuid4()

    for _ in range(2):
        cache.put(model_id, "query", [1.0])
        assert cache.get(model_id, "query") is None


def test_model_identity_keeps_different_vector_spaces_separate() -> None:
    cache = KnowledgeQueryEmbeddingCache(enabled=True, max_entries=16, ttl_seconds=300)
    first_model, second_model = uuid4(), uuid4()
    cache.put(first_model, "同一个问题", [1.0, 0.0])

    assert cache.get(second_model, "同一个问题") is None
    cache.put(second_model, "同一个问题", [0.0, 1.0, 0.0])
    assert cache.get(first_model, "同一个问题") == (1.0, 0.0)
    assert cache.get(second_model, "同一个问题") == (0.0, 1.0, 0.0)


@pytest.mark.parametrize("other_query", [" Query", "query"])
def test_raw_query_bytes_are_not_normalized(other_query: str) -> None:
    cache = KnowledgeQueryEmbeddingCache(enabled=True, max_entries=16, ttl_seconds=300)
    model_id = uuid4()
    cache.put(model_id, "Query", [1.0])

    assert cache.get(model_id, other_query) is None
    assert cache.get(model_id, "Query") == (1.0,)
