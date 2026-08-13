"""Dependency-neutral identity and error contracts for owner-private Memory."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

DEFAULT_MEMORY_NAMESPACE = "default"


class MemoryDocumentNotFound(LookupError):
    pass


class MemoryDocumentConflict(RuntimeError):
    pass


class MemoryEpisodeCursorInvalid(ValueError):
    pass


class MemoryDreamLeaseConflict(MemoryDocumentConflict):
    """The caller can no longer prove ownership of the Dream Job lease."""


class MemoryDreamStaleConflict(MemoryDocumentConflict):
    """The frozen Dream input no longer matches current Memory authority."""


class MemoryDreamSettlementInvariant(MemoryDocumentConflict):
    """Dream settlement reached a state outside its terminal contract."""


@dataclass(frozen=True, slots=True)
class MemoryDocumentScope:
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str = DEFAULT_MEMORY_NAMESPACE

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory scope requires project and owner UUIDs") from None
        namespace = self.namespace.strip() if isinstance(self.namespace, str) else ""
        if not namespace or len(namespace) > 255:
            raise ValueError("Memory scope requires a bounded namespace")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "namespace", namespace)


def scope_predicates(row_type, scope: MemoryDocumentScope):
    """Return persistence predicates without importing SQLAlchemy here.

    Column comparison is supplied by the caller's ORM type.  Keeping this
    helper duck-typed lets the contract package remain importable without the
    persistence stack while all stores share one exact scope definition.
    """

    if type(scope) is not MemoryDocumentScope:
        raise TypeError("MemoryDocumentScope is required")
    return (
        row_type.project_id == scope.project_id,
        row_type.owner_user_id == scope.owner_user_id,
        row_type.namespace == scope.namespace,
    )


__all__ = [
    "DEFAULT_MEMORY_NAMESPACE",
    "MemoryDocumentConflict",
    "MemoryDocumentNotFound",
    "MemoryDocumentScope",
    "MemoryDreamLeaseConflict",
    "MemoryDreamSettlementInvariant",
    "MemoryDreamStaleConflict",
    "MemoryEpisodeCursorInvalid",
    "scope_predicates",
]
