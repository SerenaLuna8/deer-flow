"""Authoring-time Agent tool-group and model catalog validation.

The PostgreSQL model catalog is dynamic governance state, while the internal
tool-group catalog is restart-frozen server configuration.  This module keeps
both authorities behind explicit ports so Agent authoring never reaches into
ambient ``AppConfig`` state and tests can inject exact catalogs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.errors import AssetStorageUnavailable, AssetValidationFailed
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)


class ToolGroupCatalog(Protocol):
    """Exact, secret-free catalog of server-supported internal tool groups."""

    def contains(self, tool_group: str, /) -> bool: ...


class ActiveModelCatalog(Protocol):
    """Subset of ``SystemModelRepository`` needed by Agent governance."""

    async def resolve_active_model(
        self,
        model_ref: str | None,
        *,
        load_envelope: bool,
    ) -> object | None: ...


class AgentCatalogValidationPort(Protocol):
    """Validate one Agent execution catalog inside the caller transaction."""

    async def validate(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        model_ref: str,
        tool_groups: Sequence[str],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RejectingAgentCatalogValidator:
    """Default authority for non-HTTP construction: deny every mutation.

    Production-facing composition roots must replace this with
    :class:`AgentCatalogValidator`. Keeping the default as an object instead
    of ``None`` makes the fail-closed behavior explicit and testable.
    """

    async def validate(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        model_ref: str,
        tool_groups: Sequence[str],
    ) -> None:
        del session, model_ref, tool_groups
        raise AssetValidationFailed(request_id if isinstance(request_id, str) else "unknown")


@dataclass(frozen=True, slots=True, init=False)
class StaticToolGroupCatalog:
    """Restart-frozen exact tool-group names supplied by Gateway wiring."""

    names: frozenset[str]

    def __init__(self, names: Iterable[str]) -> None:
        try:
            values = tuple(names)
        except TypeError:
            raise ValueError("tool-group catalog must be iterable") from None
        if any(not isinstance(value, str) or not value or value.strip() != value for value in values):
            raise ValueError("tool-group catalog contains an invalid name")
        object.__setattr__(self, "names", frozenset(values))

    def contains(self, tool_group: str, /) -> bool:
        return tool_group in self.names


ModelCatalogFactory = Callable[[AsyncSession], ActiveModelCatalog]


@dataclass(frozen=True, slots=True)
class AgentCatalogValidator:
    """PostgreSQL-backed model validation plus exact tool-group validation."""

    tool_group_catalog: ToolGroupCatalog
    model_catalog_factory: ModelCatalogFactory = SystemModelRepository

    def __post_init__(self) -> None:
        if not callable(getattr(self.tool_group_catalog, "contains", None)):
            raise ValueError("tool-group catalog is required")
        if not callable(self.model_catalog_factory):
            raise ValueError("model-catalog factory is required")

    async def validate(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        model_ref: str,
        tool_groups: Sequence[str],
    ) -> None:
        safe_request_id = request_id if isinstance(request_id, str) else "unknown"
        if not isinstance(model_ref, str) or not model_ref or model_ref.strip() != model_ref:
            raise AssetValidationFailed(safe_request_id)
        try:
            groups = tuple(tool_groups)
        except TypeError:
            raise AssetValidationFailed(safe_request_id) from None
        if any(not isinstance(group, str) or not group or group.strip() != group or not self.tool_group_catalog.contains(group) for group in groups):
            raise AssetValidationFailed(safe_request_id)

        model_catalog = self.model_catalog_factory(session)
        try:
            material = await model_catalog.resolve_active_model(
                model_ref,
                load_envelope=False,
            )
        except SystemModelRepositoryInvariant:
            raise AssetStorageUnavailable(safe_request_id) from None
        if material is None:
            raise AssetValidationFailed(safe_request_id)


async def require_agent_catalog_validation(
    validator: AgentCatalogValidationPort | None,
    session: AsyncSession,
    *,
    request_id: str,
    model_ref: str,
    tool_groups: Sequence[str],
) -> None:
    """Fail closed when a mutating service was not wired with catalog authority."""

    if validator is None:
        raise AssetValidationFailed(request_id if isinstance(request_id, str) else "unknown")
    await validator.validate(
        session,
        request_id=request_id,
        model_ref=model_ref,
        tool_groups=tool_groups,
    )


__all__ = [
    "ActiveModelCatalog",
    "AgentCatalogValidationPort",
    "AgentCatalogValidator",
    "ModelCatalogFactory",
    "RejectingAgentCatalogValidator",
    "StaticToolGroupCatalog",
    "ToolGroupCatalog",
    "require_agent_catalog_validation",
]
