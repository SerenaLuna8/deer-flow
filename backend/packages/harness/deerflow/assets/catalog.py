"""Read-only shared-asset catalog boundary owned by deerflow-harness."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class AssetCatalogUnavailable(RuntimeError):
    """The PostgreSQL system catalog cannot safely serve a runtime lookup."""


class AssetCatalogScope(StrEnum):
    SYSTEM = "system"
    PROJECT = "project"


@dataclass(frozen=True)
class AssetCatalogSkillFile:
    path: str
    content: bytes = field(repr=False)
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class AssetCatalogAgentSnapshot:
    slug: str
    scope: AssetCatalogScope
    version_id: uuid.UUID | str
    generation: int
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    asset_id: uuid.UUID | None = None
    checksum: str = ""
    skill_slugs: tuple[str, ...] = ()
    mcp_slugs: tuple[str, ...] = ()
    payload_schema_version: int = 1
    agents_instructions: str = ""
    identity: str = ""
    user_context: str = ""
    model_settings: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetCatalogSkillSnapshot:
    slug: str
    scope: AssetCatalogScope
    version_id: uuid.UUID | str
    generation: int
    description: str
    files: tuple[AssetCatalogSkillFile, ...]
    asset_id: uuid.UUID | None = None
    checksum: str = ""
    secret_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetCatalogMcpSnapshot:
    slug: str
    scope: AssetCatalogScope
    version_id: uuid.UUID | str
    generation: int
    definition: Mapping[str, object]
    credential_grant_ids: tuple[uuid.UUID, ...]
    asset_id: uuid.UUID | None = None
    checksum: str = ""


CatalogSnapshot = AssetCatalogAgentSnapshot | AssetCatalogSkillSnapshot | AssetCatalogMcpSnapshot


def require_system_asset(snapshot: CatalogSnapshot) -> CatalogSnapshot:
    """Reject project-scoped data at the legacy system-runtime boundary."""

    if snapshot.scope is not AssetCatalogScope.SYSTEM:
        raise AssetCatalogUnavailable("project assets are unavailable through the legacy system catalog")
    return snapshot


@runtime_checkable
class AssetCatalogProvider(Protocol):
    def run_sync(self, operation: str, *args: object) -> object: ...

    async def get_system_agent(self, slug: str) -> AssetCatalogAgentSnapshot: ...

    async def list_system_agents(self) -> tuple[AssetCatalogAgentSnapshot, ...]: ...

    async def list_system_skills(self) -> tuple[AssetCatalogSkillSnapshot, ...]: ...

    async def list_system_mcp(self) -> tuple[AssetCatalogMcpSnapshot, ...]: ...

    async def materialize_mcp_secrets(
        self,
        context: object,
        snapshot: AssetCatalogMcpSnapshot,
    ) -> Mapping[str, Mapping[str, object]]: ...


_provider: AssetCatalogProvider | None = None


def set_asset_catalog_provider(provider: AssetCatalogProvider | None) -> None:
    global _provider
    _provider = provider


def get_asset_catalog_provider() -> AssetCatalogProvider | None:
    return _provider


def require_asset_catalog_provider() -> AssetCatalogProvider:
    provider = get_asset_catalog_provider()
    if provider is None:
        raise AssetCatalogUnavailable("PostgreSQL asset catalog provider is unavailable")
    return provider


def trusted_asset_context(value: object | None) -> object | None:
    """Accept only an opaque, internally supplied context object.

    JSON/client-shaped values are never authorization-grade and must not cross
    the app-owned materialization boundary.
    """

    if value is None or isinstance(
        value,
        (Mapping, list, tuple, set, frozenset, str, bytes, bytearray, int, float, bool),
    ):
        return None
    return value


def run_asset_catalog_lookup(provider: AssetCatalogProvider, operation: str, *args: object) -> object:
    """Run a sync loader lookup through the provider's owning event loop."""

    return provider.run_sync(operation, *args)


__all__ = [
    "AssetCatalogAgentSnapshot",
    "AssetCatalogMcpSnapshot",
    "AssetCatalogProvider",
    "AssetCatalogScope",
    "AssetCatalogSkillFile",
    "AssetCatalogSkillSnapshot",
    "AssetCatalogUnavailable",
    "get_asset_catalog_provider",
    "require_asset_catalog_provider",
    "require_system_asset",
    "run_asset_catalog_lookup",
    "set_asset_catalog_provider",
    "trusted_asset_context",
]
