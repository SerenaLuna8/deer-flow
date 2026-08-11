"""Small process-local cache for immutable compiled Workflow graphs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True, slots=True)
class CompilerCacheKey:
    """The exact compatibility identity frozen by the Workflow design."""

    graph_schema_version: int
    compiler_contract_version: int
    semantic_checksum: str

    def __post_init__(self) -> None:
        if type(self.graph_schema_version) is not int or self.graph_schema_version <= 0:
            raise ValueError("graph_schema_version must be positive")
        if type(self.compiler_contract_version) is not int or self.compiler_contract_version <= 0:
            raise ValueError("compiler_contract_version must be positive")
        if len(self.semantic_checksum) != 64 or any(character not in "0123456789abcdef" for character in self.semantic_checksum):
            raise ValueError("semantic_checksum must be a lowercase SHA-256 digest")


class WorkflowCompilerCache[CompiledT]:
    """Thread-safe LRU for immutable, authority-free lowering templates."""

    def __init__(self, *, max_entries: int = 128) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[CompilerCacheKey, CompiledT] = OrderedDict()
        self._lock = RLock()
        self.hits = 0
        self.misses = 0

    def get_or_compile(
        self,
        key: CompilerCacheKey,
        factory: Callable[[], CompiledT],
    ) -> CompiledT:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                self.hits += 1
                return cached
            self.misses += 1
            compiled = factory()
            self._entries[key] = compiled
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return compiled

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
