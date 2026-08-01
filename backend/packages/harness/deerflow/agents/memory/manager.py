"""Project Memory capability contract.

Identity and authorization are deliberately absent from this module. The app
layer issues a run-bound read authority; the harness can only ask that opaque
authority for one exact PostgreSQL snapshot and rank facts from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from deerflow.agents.memory.retrieval import rank_project_memory_facts


@dataclass(frozen=True, slots=True)
class ProjectMemoryCapabilities:
    """Capabilities exposed by the model runtime adapter, not the management API."""

    supports_search: bool
    supports_fact_mutation: bool
    requires_passive_writes: bool


PROJECT_MEMORY_CAPABILITIES = ProjectMemoryCapabilities(
    supports_search=True,
    supports_fact_mutation=False,
    requires_passive_writes=True,
)


class ProjectMemoryReadAuthority(Protocol):
    """Opaque app-owned authority bound to one exact private Run."""

    async def load_snapshot(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class ProjectMemorySearchResponse:
    snapshot_version: int | None
    results: tuple[dict[str, Any], ...]


def _iso_z(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def _fact_payload(fact: object) -> dict[str, Any] | None:
    if isinstance(fact, Mapping):
        return dict(fact)
    try:
        return {
            "id": str(getattr(fact, "id")),
            "content": getattr(fact, "content"),
            "category": getattr(fact, "category"),
            "confidence": getattr(fact, "confidence"),
            "createdAt": _iso_z(getattr(fact, "created_at", None)),
        }
    except (AttributeError, TypeError, ValueError):
        return None


class ProjectMemoryManager:
    """Search adapter over an app-authorized PostgreSQL Memory snapshot."""

    capabilities = PROJECT_MEMORY_CAPABILITIES

    async def asearch(
        self,
        *,
        authority: ProjectMemoryReadAuthority,
        query: str,
        category: str | None,
        top_k: int,
        now: datetime | None = None,
    ) -> ProjectMemorySearchResponse:
        load_snapshot = getattr(authority, "load_snapshot", None)
        if isinstance(authority, Mapping) or not callable(load_snapshot):
            raise RuntimeError("project memory authority is unavailable")
        snapshot = await load_snapshot()
        if snapshot is None:
            return ProjectMemorySearchResponse(
                snapshot_version=None,
                results=(),
            )

        version = getattr(snapshot, "version", None)
        facts = getattr(snapshot, "facts", None)
        if type(version) is not int or version < 1:
            raise RuntimeError("project memory snapshot is invalid")
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes, bytearray)):
            raise RuntimeError("project memory snapshot is invalid")
        payloads = tuple(payload for fact in facts if (payload := _fact_payload(fact)) is not None)
        results = rank_project_memory_facts(
            payloads,
            query,
            category=category,
            top_k=top_k,
            now=now,
        )
        return ProjectMemorySearchResponse(
            snapshot_version=version,
            results=tuple(results),
        )


_project_memory_manager = ProjectMemoryManager()


def get_project_memory_manager() -> ProjectMemoryManager:
    return _project_memory_manager


__all__ = [
    "PROJECT_MEMORY_CAPABILITIES",
    "ProjectMemoryCapabilities",
    "ProjectMemoryManager",
    "ProjectMemoryReadAuthority",
    "ProjectMemorySearchResponse",
    "get_project_memory_manager",
]
