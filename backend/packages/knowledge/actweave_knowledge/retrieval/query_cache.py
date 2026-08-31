"""Bounded process-local reuse of query embeddings, never retrieval authority."""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from uuid import UUID


class KnowledgeQueryEmbeddingCache:
    """Synchronous operations share one event loop; Provider work stays outside."""

    def __init__(
        self,
        *,
        enabled: bool,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[tuple[UUID, bytes], tuple[float, tuple[float, ...]]] = OrderedDict()

    def get(self, model_id: UUID, query: str) -> tuple[float, ...] | None:
        if not self._enabled:
            return None
        key = (model_id, hashlib.sha256(query.encode("utf-8")).digest())
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, vector = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return vector

    def put(self, model_id: UUID, query: str, vector: Sequence[float]) -> None:
        if not self._enabled:
            return
        key = (model_id, hashlib.sha256(query.encode("utf-8")).digest())
        self._entries[key] = (self._clock() + self._ttl_seconds, tuple(vector))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
