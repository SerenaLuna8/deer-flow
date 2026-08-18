"""PostgreSQL-backed adapter for the harness system-asset catalog protocol."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.agent_payload_checksum import agent_payload_checksum_matches
from app.shared_assets.credential_closure import (
    McpCredentialClosureInvalid,
    McpCredentialClosureTarget,
    lock_mcp_credential_closures,
)
from app.shared_assets.errors import AssetValidationFailed, SharedAssetError
from app.shared_assets.internal_assets import (
    BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
)
from app.shared_assets.keyring import CredentialKeyringInvalid
from app.shared_assets.mcp_repository import McpVersionRecord
from app.shared_assets.mcp_service import McpService
from app.shared_assets.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedMcpSnapshot,
)
from app.shared_assets.resolver import materialize_mcp_secrets as materialize_resolved_mcp_secrets
from app.shared_assets.skill_repository import SkillVersionRecord
from app.shared_assets.skill_service import SkillService
from deerflow.assets.catalog import (
    AssetCatalogAgentSnapshot,
    AssetCatalogMcpSnapshot,
    AssetCatalogScope,
    AssetCatalogSkillFile,
    AssetCatalogSkillSnapshot,
    AssetCatalogUnavailable,
    require_system_asset,
)
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    AssetCatalogStateRow,
    McpServerRow,
    McpServerVersionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)

SnapshotTuple = tuple[AssetCatalogAgentSnapshot, ...] | tuple[AssetCatalogSkillSnapshot, ...] | tuple[AssetCatalogMcpSnapshot, ...]


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass
class _TestCatalog:
    generation: int = 0
    agents: tuple[AssetCatalogAgentSnapshot, ...] = ()
    skills: tuple[AssetCatalogSkillSnapshot, ...] = ()
    mcp: tuple[AssetCatalogMcpSnapshot, ...] = ()


class PostgresAssetCatalogProvider:
    """Generation-aware, fail-closed system snapshot provider.

    Every public lookup reads ``asset_catalog_state`` before consulting the
    process cache. A generation change clears all three asset-kind caches in
    one operation, so publish, suspend and credential/grant revocation cannot
    leave a cross-kind stale snapshot behind.
    """

    def __init__(self, session_factory: Callable[[], AsyncSession] | None = None) -> None:
        if session_factory is None:
            from deerflow.persistence.engine import get_session_factory

            session_factory = get_session_factory()
        self._session_factory = session_factory
        try:
            self._owner_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("PostgresAssetCatalogProvider must be created on its owning event loop") from exc
        self._last_lookup_loop_id: int | None = None
        self._generation: int | None = None
        self._agents: tuple[AssetCatalogAgentSnapshot, ...] | None = None
        self._skills: tuple[AssetCatalogSkillSnapshot, ...] | None = None
        self._mcp: tuple[AssetCatalogMcpSnapshot, ...] | None = None
        self._cache_lock = threading.Lock()
        self._test_catalog: _TestCatalog | None = None
        self._test_load_counts = {"agent": 0, "skill": 0, "mcp": 0}

    @classmethod
    def for_test(
        cls,
        *,
        generation: int = 0,
        agents: tuple[AssetCatalogAgentSnapshot, ...] = (),
        skills: tuple[AssetCatalogSkillSnapshot, ...] = (),
        mcp: tuple[AssetCatalogMcpSnapshot, ...] = (),
    ) -> PostgresAssetCatalogProvider:
        provider = cls.__new__(cls)
        provider._session_factory = None
        provider._owner_loop = asyncio.get_running_loop()
        provider._last_lookup_loop_id = None
        provider._generation = None
        provider._agents = None
        provider._skills = None
        provider._mcp = None
        provider._cache_lock = threading.Lock()
        provider._test_catalog = _TestCatalog(generation, agents, skills, mcp)
        provider._test_load_counts = {"agent": 0, "skill": 0, "mcp": 0}
        return provider

    def replace_test_catalog(
        self,
        *,
        generation: int,
        agents: tuple[AssetCatalogAgentSnapshot, ...],
        skills: tuple[AssetCatalogSkillSnapshot, ...],
        mcp: tuple[AssetCatalogMcpSnapshot, ...],
        mutation: str,
    ) -> None:
        if self._test_catalog is None:
            raise RuntimeError("test-only catalog operation")
        if mutation not in {"publish", "suspend", "grant_revoke"}:
            raise ValueError("unknown test mutation")
        self._test_catalog = _TestCatalog(generation, agents, skills, mcp)

    def cache_load_counts_for_test(self) -> dict[str, int]:
        if self._test_catalog is None:
            raise RuntimeError("test-only catalog operation")
        return dict(self._test_load_counts)

    def last_lookup_loop_id_for_test(self) -> int | None:
        if self._test_catalog is None:
            raise RuntimeError("test-only catalog operation")
        return self._last_lookup_loop_id

    def run_sync(self, operation: str, *args: object) -> object:
        methods = {
            "get_system_agent": self.get_system_agent,
            "list_system_agents": self.list_system_agents,
            "list_system_skills": self.list_system_skills,
            "list_system_mcp": self.list_system_mcp,
            "materialize_mcp_secrets": self.materialize_mcp_secrets,
        }
        method = methods.get(operation)
        if method is None:
            raise AssetCatalogUnavailable("unsupported asset catalog lookup")
        if not self._owner_loop.is_running():
            raise AssetCatalogUnavailable("asset catalog owning loop is unavailable")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._owner_loop:
            raise AssetCatalogUnavailable("synchronous asset catalog lookup cannot block its owning loop")
        future = asyncio.run_coroutine_threadsafe(method(*args), self._owner_loop)
        return future.result()

    async def get_system_agent(self, slug: str) -> AssetCatalogAgentSnapshot:
        if not isinstance(slug, str) or not slug:
            raise AssetCatalogUnavailable("system agent is unavailable")
        for snapshot in await self.list_system_agents():
            if snapshot.slug.casefold() == slug.casefold():
                return snapshot
        raise AssetCatalogUnavailable("system agent is unavailable")

    async def list_system_agents(self) -> tuple[AssetCatalogAgentSnapshot, ...]:
        return await self._lookup("agent")

    async def list_system_skills(self) -> tuple[AssetCatalogSkillSnapshot, ...]:
        return await self._lookup("skill")

    async def list_system_mcp(self) -> tuple[AssetCatalogMcpSnapshot, ...]:
        return await self._lookup("mcp")

    async def materialize_mcp_secrets(
        self,
        context: object,
        snapshot: AssetCatalogMcpSnapshot,
    ) -> Mapping[str, Mapping[str, object]]:
        if not isinstance(context, ProjectContext):
            raise AssetCatalogUnavailable("trusted project context is required for MCP credentials")
        require_system_asset(snapshot)
        try:
            if not isinstance(snapshot.asset_id, uuid.UUID):
                raise ValueError
            version_id = uuid.UUID(str(snapshot.version_id))
            resolved = ResolvedMcpSnapshot(
                kind=AssetKind.MCP,
                scope=AssetScope.SYSTEM,
                asset_id=snapshot.asset_id,
                version_id=version_id,
                checksum=snapshot.checksum,
                catalog_generation=snapshot.generation,
                dependency_version_ids=(),
                definition=snapshot.definition,
                credential_grant_ids=snapshot.credential_grant_ids,
            )
        except (TypeError, ValueError):
            raise AssetCatalogUnavailable("system MCP credential snapshot is invalid") from None
        if self._session_factory is None:
            raise AssetCatalogUnavailable("asset catalog database is unavailable")
        try:
            materialized = await materialize_resolved_mcp_secrets(
                context,
                resolved,
                session_factory=self._session_factory,
            )
            return materialized.by_slot
        except (SharedAssetError, CredentialKeyringInvalid):
            raise AssetCatalogUnavailable("system MCP credentials are unavailable") from None

    def _session(self) -> AsyncSession:
        if self._session_factory is None:
            raise AssetCatalogUnavailable("asset catalog database is unavailable")
        return self._session_factory()

    async def _lookup(self, kind: str):
        try:
            self._last_lookup_loop_id = id(asyncio.get_running_loop())
            if self._test_catalog is not None:
                generation = self._test_catalog.generation
                self._prepare_cache(generation)
                return self._lookup_test(kind, generation)

            async with self._session() as session:
                async with session.begin():
                    generation = await self._read_state(session)
                    self._prepare_cache(generation)
                    cached = self._cached(kind)
                    if cached is not None:
                        return cached
                    loaded = await self._load(session, kind, generation)
                    self._validate_loaded(loaded, generation)
                    final_generation = await self._read_state(session)
                    if final_generation != generation:
                        raise AssetCatalogUnavailable("system asset catalog changed during lookup")
                    self._store(kind, loaded, expected_generation=generation)
                    return loaded
        except AssetCatalogUnavailable:
            raise
        except McpCredentialClosureInvalid:
            raise AssetCatalogUnavailable("system MCP catalog is unavailable") from None
        except (DBAPIError, SATimeoutError):
            raise AssetCatalogUnavailable("asset catalog database is unavailable") from None

    async def _read_state(self, session: AsyncSession) -> int:
        row = (await session.execute(select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1))).scalar_one_or_none()
        if row is None:
            return 0
        return int(row)

    def _prepare_cache(self, generation: int) -> None:
        with self._cache_lock:
            if self._generation != generation:
                self._agents = None
                self._skills = None
                self._mcp = None
                self._generation = generation

    def _cached(self, kind: str) -> SnapshotTuple | None:
        with self._cache_lock:
            return {"agent": self._agents, "skill": self._skills, "mcp": self._mcp}[kind]

    def _store(
        self,
        kind: str,
        snapshots: SnapshotTuple,
        *,
        expected_generation: int,
    ) -> None:
        with self._cache_lock:
            if self._generation != expected_generation:
                raise AssetCatalogUnavailable("system asset catalog changed during lookup")
            if kind == "agent":
                self._agents = snapshots
            elif kind == "skill":
                self._skills = snapshots
            else:
                self._mcp = snapshots

    def _lookup_test(self, kind: str, generation: int):
        cached = self._cached(kind)
        if cached is not None:
            return cached
        if self._test_catalog is None:
            raise AssertionError("missing test catalog")
        loaded = {
            "agent": self._test_catalog.agents,
            "skill": self._test_catalog.skills,
            "mcp": self._test_catalog.mcp,
        }[kind]
        self._test_load_counts[kind] += 1
        self._validate_loaded(loaded, generation)
        self._store(kind, loaded, expected_generation=generation)
        return loaded

    @staticmethod
    def _validate_loaded(snapshots: SnapshotTuple, generation: int) -> None:
        for snapshot in snapshots:
            require_system_asset(snapshot)
            if type(snapshot.generation) is not int or snapshot.generation != generation:
                raise AssetCatalogUnavailable("system asset catalog generation is invalid")

    async def _load(self, session: AsyncSession, kind: str, generation: int):
        if kind == "agent":
            return await self._load_agents(session, generation)
        if kind == "skill":
            return await self._load_skills(session, generation)
        return await self._load_mcp(session, generation)

    @staticmethod
    async def _load_agents(session: AsyncSession, generation: int) -> tuple[AssetCatalogAgentSnapshot, ...]:
        rows = (
            await session.execute(
                select(AgentRow, AgentVersionRow)
                .join(AgentVersionRow, AgentVersionRow.id == AgentRow.current_published_version_id)
                .where(
                    AgentRow.scope == "system",
                    AgentRow.project_id.is_(None),
                    AgentRow.status == "active",
                    or_(
                        AgentRow.source_key.is_(None),
                        AgentRow.source_key != BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY,
                    ),
                    AgentVersionRow.workflow_status == "published",
                )
                .order_by(AgentRow.slug)
            )
        ).all()
        snapshots: list[AssetCatalogAgentSnapshot] = []
        for asset, version in rows:
            if asset.source_key == BUILTIN_SKILL_BUILDER_AGENT_SOURCE_KEY:
                continue
            if asset.status != "active":
                raise AssetCatalogUnavailable("system agent catalog is invalid")
            raw_skill_ids = tuple((await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == version.id).order_by(AgentVersionSkillRefRow.sort_order))).scalars().all())
            skill_rows = (
                await session.execute(
                    select(AgentVersionSkillRefRow.skill_version_id, SkillRow.slug)
                    .join(SkillVersionRow, SkillVersionRow.id == AgentVersionSkillRefRow.skill_version_id)
                    .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
                    .where(
                        AgentVersionSkillRefRow.agent_version_id == version.id,
                        SkillRow.scope == "system",
                        SkillRow.project_id.is_(None),
                        SkillRow.status == "active",
                        SkillVersionRow.workflow_status == "published",
                        SkillVersionRow.revoked_at.is_(None),
                    )
                    .order_by(AgentVersionSkillRefRow.sort_order)
                )
            ).all()
            raw_mcp_ids = tuple((await session.execute(select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == version.id).order_by(AgentVersionMcpRefRow.sort_order))).scalars().all())
            mcp_rows = (
                await session.execute(
                    select(AgentVersionMcpRefRow.mcp_server_version_id, McpServerRow.slug)
                    .join(McpServerVersionRow, McpServerVersionRow.id == AgentVersionMcpRefRow.mcp_server_version_id)
                    .join(McpServerRow, McpServerRow.id == McpServerVersionRow.mcp_server_id)
                    .where(
                        AgentVersionMcpRefRow.agent_version_id == version.id,
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                        McpServerRow.status == "active",
                        McpServerVersionRow.workflow_status == "published",
                    )
                    .order_by(AgentVersionMcpRefRow.sort_order)
                )
            ).all()
            if raw_skill_ids != tuple(row[0] for row in skill_rows) or raw_mcp_ids != tuple(row[0] for row in mcp_rows):
                raise AssetCatalogUnavailable("system agent dependency catalog is invalid")
            try:
                model_settings = AgentModelSettings.model_validate({} if version.model_settings is None else version.model_settings)
            except ValidationError:
                raise AssetCatalogUnavailable("system agent catalog is invalid") from None
            if (
                version.payload_schema_version not in (1, 2, 3)
                or (version.payload_schema_version in (1, 2) and not model_settings.is_empty)
                or not isinstance(version.tool_groups, list)
                or (version.model_ref != DEFAULT_MODEL_REF and exact_model_ref(version.model_ref) is None)
            ):
                raise AssetCatalogUnavailable("system agent catalog is invalid")
            payload = AgentPayload(
                description=version.description,
                soul=version.soul,
                model_ref=version.model_ref,
                tool_groups=tuple(version.tool_groups),
                skill_version_ids=tuple(uuid.UUID(str(row[0])) for row in skill_rows),
                mcp_version_ids=tuple(uuid.UUID(str(row[0])) for row in mcp_rows),
                payload_schema_version=version.payload_schema_version,
                agents_instructions=version.agents_instructions,
                identity=version.identity,
                user_context=version.user_context,
                model_settings=model_settings,
            )
            if not agent_payload_checksum_matches(
                payload,
                version.payload_checksum,
            ):
                raise AssetCatalogUnavailable("system agent catalog is invalid")
            snapshots.append(
                AssetCatalogAgentSnapshot(
                    slug=asset.slug,
                    scope=AssetCatalogScope.SYSTEM,
                    asset_id=uuid.UUID(str(asset.id)),
                    version_id=uuid.UUID(str(version.id)),
                    generation=generation,
                    checksum=version.payload_checksum,
                    description=version.description,
                    soul=version.soul,
                    model_ref=version.model_ref,
                    tool_groups=payload.tool_groups,
                    skill_version_ids=payload.skill_version_ids,
                    mcp_version_ids=payload.mcp_version_ids,
                    skill_slugs=tuple(str(row[1]) for row in skill_rows),
                    mcp_slugs=tuple(str(row[1]) for row in mcp_rows),
                    payload_schema_version=version.payload_schema_version,
                    agents_instructions=version.agents_instructions,
                    identity=version.identity,
                    user_context=version.user_context,
                    model_settings=MappingProxyType(model_settings.model_dump(exclude_none=True)),
                )
            )
        return tuple(snapshots)

    @staticmethod
    async def _load_skills(session: AsyncSession, generation: int) -> tuple[AssetCatalogSkillSnapshot, ...]:
        rows = (
            await session.execute(
                select(SkillRow, SkillVersionRow)
                .join(SkillVersionRow, SkillVersionRow.id == SkillRow.current_published_version_id)
                .where(
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                    SkillRow.status == "active",
                    SkillVersionRow.workflow_status == "published",
                    SkillVersionRow.revoked_at.is_(None),
                )
                .order_by(SkillRow.slug)
            )
        ).all()
        snapshots: list[AssetCatalogSkillSnapshot] = []
        for asset, version in rows:
            if asset.status != "active":
                raise AssetCatalogUnavailable("system skill catalog is invalid")
            files = tuple((await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version.id).order_by(SkillVersionFileRow.path))).scalars().all())
            try:
                verified_files = await asyncio.to_thread(
                    SkillService._verified_archive_files,
                    SkillVersionRecord(version, files),
                    "asset-catalog-runtime",
                )
            except AssetValidationFailed:
                raise AssetCatalogUnavailable("system skill catalog is invalid") from None
            if not verified_files or not any(file.path == "SKILL.md" for file in verified_files):
                raise AssetCatalogUnavailable("system skill catalog is invalid")
            requirements: list[str] = []
            for item in version.secret_requirements:
                if isinstance(item, str):
                    requirements.append(item)
                elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                    requirements.append(str(item["name"]))
                else:
                    raise AssetCatalogUnavailable("system skill catalog is invalid")
            snapshots.append(
                AssetCatalogSkillSnapshot(
                    slug=asset.slug,
                    scope=AssetCatalogScope.SYSTEM,
                    asset_id=uuid.UUID(str(asset.id)),
                    version_id=uuid.UUID(str(version.id)),
                    generation=generation,
                    checksum=version.payload_checksum,
                    description=version.description,
                    files=tuple(AssetCatalogSkillFile(file.path, file.content, file.media_type) for file in verified_files),
                    secret_requirements=tuple(requirements),
                )
            )
        return tuple(snapshots)

    @staticmethod
    async def _load_mcp(session: AsyncSession, generation: int) -> tuple[AssetCatalogMcpSnapshot, ...]:
        rows = (
            await session.execute(
                select(McpServerRow, McpServerVersionRow)
                .join(McpServerVersionRow, McpServerVersionRow.id == McpServerRow.current_published_version_id)
                .where(
                    McpServerRow.scope == "system",
                    McpServerRow.project_id.is_(None),
                    McpServerRow.status == "active",
                    McpServerVersionRow.workflow_status == "published",
                )
                .order_by(McpServerRow.slug)
            )
        ).all()
        targets = tuple(McpCredentialClosureTarget(uuid.UUID(str(version.id)), AssetScope.SYSTEM, None) for _asset, version in rows)
        closures = await lock_mcp_credential_closures(session, targets) if targets else {}
        snapshots: list[AssetCatalogMcpSnapshot] = []
        for asset, version in rows:
            if asset.status != "active":
                raise AssetCatalogUnavailable("system MCP catalog is invalid")
            closure = closures[uuid.UUID(str(version.id))]
            record = McpVersionRecord(version, closure.slots, closure.grants)
            canonical_definition = McpService._definition_from_record(record)
            if McpService._checksum(canonical_definition) != version.payload_checksum:
                raise AssetCatalogUnavailable("system MCP catalog is invalid")
            definition = _freeze(
                {
                    "description": canonical_definition.description,
                    "transport": canonical_definition.transport,
                    "command": canonical_definition.command,
                    "args": canonical_definition.args,
                    "url": canonical_definition.url,
                    "env": canonical_definition.env,
                    "headers": canonical_definition.headers,
                    "oauth": canonical_definition.oauth,
                    "routing": canonical_definition.routing,
                    "tool_overrides": canonical_definition.tool_overrides,
                    "timeout_seconds": canonical_definition.timeout_seconds,
                    "credential_slots": tuple(
                        {
                            "name": slot.name,
                            "purpose": slot.purpose,
                            "payload_schema": slot.payload_schema,
                            "required": slot.required,
                        }
                        for slot in canonical_definition.credential_slots
                    ),
                }
            )
            if not isinstance(definition, Mapping):
                raise AssetCatalogUnavailable("system MCP catalog is invalid")
            snapshots.append(
                AssetCatalogMcpSnapshot(
                    slug=asset.slug,
                    scope=AssetCatalogScope.SYSTEM,
                    asset_id=uuid.UUID(str(asset.id)),
                    version_id=uuid.UUID(str(version.id)),
                    generation=generation,
                    checksum=version.payload_checksum,
                    definition=definition,
                    credential_grant_ids=closure.grant_ids,
                )
            )
        return tuple(snapshots)


__all__ = ["AssetCatalogUnavailable", "PostgresAssetCatalogProvider"]
