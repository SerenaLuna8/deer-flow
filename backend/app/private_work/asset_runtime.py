from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.authorization import (
    PrivateRunAuthorizationBoundary,
    PrivateRunAuthorizationService,
)
from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import AdmittedPrivateRun
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.snapshot_repository import RunSnapshotAssetStale, RunSnapshotRepository
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.shared_assets.binding_model import AssetCatalogStateRow
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import Skill, SkillCategory

logger = logging.getLogger(__name__)

_PRIVATE_SKILL_CLEANUP_ATTEMPTS = 3


class PrivateRuntimeCleanupError(RuntimeError):
    """Stable internal error for a run-owned temporary tree left behind."""


def _remove_private_skill_tree(root: Path) -> None:
    """Remove one private Skill tree with bounded, retryable semantics."""

    for attempt in range(_PRIVATE_SKILL_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 == _PRIVATE_SKILL_CLEANUP_ATTEMPTS:
                raise PrivateRuntimeCleanupError("Private runtime cleanup failed") from None


def _create_private_skill_root(run_id: str, request_id: str) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:80]
    try:
        return Path(tempfile.mkdtemp(prefix=f"deerflow-private-{safe_run_id}-")).resolve()
    except OSError:
        raise PrivateWorkUnavailable(request_id) from None


@dataclass(frozen=True, slots=True)
class PrivateSkillManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    relative_root: str


@dataclass(frozen=True, slots=True)
class PrivateMcpManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    definition: dict[str, object]


@dataclass(frozen=True, slots=True)
class PrivateAgentManifest:
    agent_asset_id: uuid.UUID
    agent_version_id: uuid.UUID
    checksum: str
    catalog_generation: int
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skills: tuple[PrivateSkillManifest, ...]
    mcps: tuple[PrivateMcpManifest, ...]


class _EmptyMcpArgs(BaseModel):
    pass


@dataclass(frozen=True, slots=True)
class _DiscoveredMcpTool:
    version_id: uuid.UUID
    name: str
    description: str
    args_schema: type[BaseModel]


def _safe_copy(value: object) -> object:
    """Copy only the resolver's JSON-like, secret-free MCP definition.

    Credential schema *field names* such as ``client_secret`` and ``key_id``
    describe required input; they are not credential material.  The M3
    resolver owns the plaintext boundary and deliberately excludes envelopes
    and decrypted payloads from this definition, so this copy validates shape
    instead of guessing secrecy from key names.
    """

    if isinstance(value, Mapping):
        return {str(key): _safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_safe_copy(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise RunSnapshotAssetStale


def _write_skill_tree(
    root: Path,
    skill_snapshots: tuple[ResolvedSkillSnapshot, ...],
) -> tuple[tuple[PrivateSkillManifest, ...], tuple[Skill, ...]]:
    manifests: list[PrivateSkillManifest] = []
    skills: list[Skill] = []
    for snapshot in skill_snapshots:
        relative_root = snapshot.asset_id.hex
        skill_root = root / SkillCategory.CUSTOM.value / relative_root
        skill_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        for archive_file in snapshot.files:
            relative = Path(archive_file.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise RunSnapshotAssetStale
            destination = (skill_root / relative).resolve()
            if skill_root.resolve() not in destination.parents:
                raise RunSnapshotAssetStale
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(archive_file.content)
            destination.chmod(0o600)
        parsed = parse_skill_file(skill_root / "SKILL.md", SkillCategory.CUSTOM, Path(relative_root))
        if parsed is None:
            raise RunSnapshotAssetStale
        manifests.append(
            PrivateSkillManifest(
                asset_id=snapshot.asset_id,
                version_id=snapshot.version_id,
                relative_root=relative_root,
            )
        )
        skills.append(replace(parsed, enabled=True, runtime_read_only=True))
    return tuple(manifests), tuple(skills)


class PrivateAgentRuntime:
    """Run-owned exact assets.  Its repr and public manifest are secret-free."""

    __slots__ = (
        "_authorization_boundary",
        "_closed",
        "_context",
        "_mcp_snapshots",
        "_mcp_tools",
        "_resolver",
        "_run_id",
        "_session_factory",
        "safe_manifest",
        "skill_root",
        "skills",
    )

    def __init__(
        self,
        *,
        context: PrivateWorkContext,
        run_id: str,
        resolver: ProjectAssetResolver,
        session_factory: async_sessionmaker[AsyncSession],
        safe_manifest: PrivateAgentManifest,
        skill_root: Path,
        skills: tuple[Skill, ...],
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...],
        authorization_boundary: object,
    ) -> None:
        self._context = context
        self._run_id = run_id
        self._resolver = resolver
        self._session_factory = session_factory
        self._mcp_snapshots = mcp_snapshots
        self._authorization_boundary = authorization_boundary
        self._closed = False
        self.safe_manifest = safe_manifest
        self.skill_root = skill_root
        self.skills = skills
        self._mcp_tools: tuple[StructuredTool, ...] = ()

    def __repr__(self) -> str:
        return f"PrivateAgentRuntime(run_id={self._run_id!r}, agent_version_id={self.agent_version_id!r}, closed={self._closed!r})"

    @property
    def agent_version_id(self) -> uuid.UUID:
        return self.safe_manifest.agent_version_id

    @property
    def model_ref(self) -> str:
        return self.safe_manifest.model_ref

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def soul(self) -> str:
        return self.safe_manifest.soul

    @property
    def tool_groups(self) -> tuple[str, ...]:
        return self.safe_manifest.tool_groups

    @property
    def mcp_definitions(self) -> tuple[PrivateMcpManifest, ...]:
        return self.safe_manifest.mcps

    @property
    def mcp_tools(self) -> tuple[object, ...]:
        return self._mcp_tools

    def set_authorization_boundary(self, boundary: object) -> None:
        self._authorization_boundary = boundary

    async def discover_mcp_tools(self) -> None:
        """Copy remote schemas into run-local proxies, never remote tool objects."""

        schemas: list[_DiscoveredMcpTool] = []
        for snapshot in self._mcp_snapshots:
            discovered = await self.invoke_with_mcp_material(
                snapshot.version_id,
                lambda definition, material, version_id=snapshot.version_id: self._discover_exact_mcp(
                    version_id,
                    definition,
                    material,
                    authorization_boundary=self._authorization_boundary,
                ),
            )
            schemas.extend(discovered)
        self._mcp_tools = tuple(self._proxy_tool(schema) for schema in schemas)

    def _proxy_tool(self, schema: _DiscoveredMcpTool) -> StructuredTool:
        async def invoke(**arguments):
            return await self.invoke_with_mcp_material(
                schema.version_id,
                lambda definition, material: self._invoke_exact_mcp(
                    schema.version_id,
                    definition,
                    material,
                    schema.name,
                    arguments,
                    authorization_boundary=self._authorization_boundary,
                ),
            )

        return StructuredTool.from_function(
            coroutine=invoke,
            name=schema.name,
            description=schema.description,
            args_schema=schema.args_schema,
            metadata={"deerflow_private_mcp": True},
        )

    @staticmethod
    def _material_values(
        material: Mapping[str, Mapping[str, object]],
    ) -> tuple[str | bytes | bool | int | float, ...]:
        values: list[str | bytes | bool | int | float] = []

        def collect(value: object) -> None:
            if isinstance(value, Mapping):
                for item in value.values():
                    collect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif isinstance(value, (str, bytes)) and value:
                values.append(value)
            elif isinstance(value, (bool, int, float)):
                values.append(value)

        collect(material)
        return tuple(values)

    @classmethod
    def _assert_mcp_result_secret_free(
        cls,
        result: object,
        material: Mapping[str, Mapping[str, object]],
    ) -> None:
        forbidden = cls._material_values(material)
        cls._assert_value_secret_free(
            result,
            forbidden,
            PrivateWorkUnavailable,
        )

    @staticmethod
    def _scalar_contains_secret(
        value: str | bytes | bool | int | float,
        forbidden: tuple[str | bytes | bool | int | float, ...],
    ) -> bool:
        for secret in forbidden:
            if isinstance(value, str) and isinstance(secret, str):
                if secret in value:
                    return True
            elif isinstance(value, bytes) and isinstance(secret, bytes):
                if secret in value:
                    return True
            elif isinstance(value, str) and isinstance(secret, bytes):
                decoded = secret.decode("utf-8", errors="ignore")
                if decoded and decoded in value:
                    return True
            elif isinstance(value, bytes) and isinstance(secret, str):
                encoded = secret.encode()
                if encoded and encoded in value:
                    return True
            elif type(value) is type(secret) and value == secret:
                return True
        return False

    @classmethod
    def _assert_value_secret_free(
        cls,
        result: object,
        forbidden: tuple[str | bytes | bool | int | float, ...],
        error_type: type[PrivateWorkError],
    ) -> None:
        seen: set[int] = set()

        def inspect_value(value: object) -> None:
            if value is None:
                return
            if isinstance(value, (str, bytes, bool, int, float)):
                if cls._scalar_contains_secret(value, forbidden):
                    raise error_type("unknown")
                return
            identity = id(value)
            if identity in seen:
                raise error_type("unknown")
            seen.add(identity)
            if isinstance(value, Mapping):
                for key, item in value.items():
                    inspect_value(key)
                    inspect_value(item)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    inspect_value(item)
                return
            if isinstance(value, BaseModel):
                inspect_value(value.model_dump(mode="python"))
                return
            if is_dataclass(value) and not isinstance(value, type):
                inspect_value({field.name: getattr(value, field.name) for field in fields(value)})
                return
            raise error_type("unknown")

        inspect_value(result)

    @staticmethod
    async def _with_one_shot_mcp_tools(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        operation: Callable[[tuple[object, ...]], Awaitable[Any]],
        authorization_boundary: object | None = None,
    ) -> Any:
        client = None
        merged_config = None
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig
            from deerflow.mcp.client import build_servers_config
            from deerflow.mcp.tools import _catalog_mcp_definition, _merge_catalog_mcp_secrets

            server_name = f"project_{version_id.hex[:16]}"
            raw = _catalog_mcp_definition(definition)
            server = McpServerConfig.model_validate(raw)
            extensions = ExtensionsConfig(mcpServers={server_name: server})
            server_config = build_servers_config(extensions)[server_name]
            merged_config = _merge_catalog_mcp_secrets(server_config, material)
            client = MultiServerMCPClient(
                {server_name: merged_config},
                tool_name_prefix=True,
            )
            if authorization_boundary is not None:
                await authorization_boundary.before_mcp_call()
            remote_tools = tuple(await client.get_tools(server_name=server_name))
            return await operation(remote_tools)
        finally:
            if client is not None:
                close = getattr(client, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            merged_config = None
            client = None

    @classmethod
    async def _discover_exact_mcp(
        cls,
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        authorization_boundary: object | None = None,
    ) -> tuple[_DiscoveredMcpTool, ...]:
        forbidden_values = cls._material_values(material)

        async def copy_schemas(remote_tools: tuple[object, ...]) -> tuple[_DiscoveredMcpTool, ...]:
            copied: list[_DiscoveredMcpTool] = []
            for remote in remote_tools:
                name = str(getattr(remote, "name", ""))
                description = str(getattr(remote, "description", ""))
                args_schema = getattr(remote, "args_schema", None)
                if args_schema is None:
                    get_schema = getattr(remote, "get_input_schema", None)
                    args_schema = get_schema() if callable(get_schema) else _EmptyMcpArgs
                if not name or not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
                    raise PrivateWorkAssetStale("unknown")
                cls._assert_value_secret_free(
                    (name, description, args_schema.model_json_schema()),
                    forbidden_values,
                    PrivateWorkAssetStale,
                )
                copied.append(
                    _DiscoveredMcpTool(
                        version_id=version_id,
                        name=name,
                        description=description,
                        args_schema=args_schema,
                    )
                )
            return tuple(copied)

        if authorization_boundary is None:
            return await cls._with_one_shot_mcp_tools(
                version_id,
                definition,
                material,
                copy_schemas,
            )
        return await cls._with_one_shot_mcp_tools(
            version_id,
            definition,
            material,
            copy_schemas,
            authorization_boundary,
        )

    @staticmethod
    async def _invoke_exact_mcp(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        tool_name: str,
        arguments: Mapping[str, object],
        authorization_boundary: object | None = None,
    ) -> Any:
        try:

            async def call_selected(discovered: tuple[object, ...]) -> Any:
                selected = next((tool for tool in discovered if getattr(tool, "name", None) == tool_name), None)
                if selected is None:
                    raise PrivateWorkAssetStale("unknown")
                if authorization_boundary is not None:
                    await authorization_boundary.before_mcp_call()
                result = await selected.ainvoke(dict(arguments))
                PrivateAgentRuntime._assert_mcp_result_secret_free(
                    result,
                    material,
                )
                return result

            if authorization_boundary is None:
                return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                    version_id,
                    definition,
                    material,
                    call_selected,
                )
            return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                version_id,
                definition,
                material,
                call_selected,
                authorization_boundary,
            )
        except PrivateWorkError:
            raise
        except AuthorizationRevoked:
            raise
        except Exception:
            raise PrivateWorkUnavailable("unknown") from None

    async def invoke_with_mcp_material(
        self,
        mcp_version_id: uuid.UUID,
        operation: Callable[[Mapping[str, object], Mapping[str, Mapping[str, object]]], Awaitable[Any]],
    ) -> Any:
        """Materialize plaintext into one local MCP call and release it at return."""

        if self._closed:
            raise PrivateWorkAssetStale(self._context.request_id)
        snapshot = next((item for item in self._mcp_snapshots if item.version_id == mcp_version_id), None)
        if snapshot is None:
            raise PrivateWorkAssetStale(self._context.request_id)
        if self._authorization_boundary is not None:
            await self._authorization_boundary.before_mcp_call()
        materialized = await self._materialize_mcp_call(snapshot)
        try:
            try:
                return await operation(snapshot.definition, materialized.by_slot)
            finally:
                del materialized
        except (AssetResolutionUnavailable, AssetValidationFailed, AssetForbidden):
            raise PrivateWorkAssetStale(self._context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(self._context.request_id) from None
        except AuthorizationRevoked:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def _materialize_mcp_call(self, snapshot: ResolvedMcpSnapshot):
        """Compare and decrypt the exact closure in one caller-owned transaction."""

        repository = RunSnapshotRepository(self._session_factory)
        try:
            async with self._session_factory() as session, session.begin():
                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._run_id,
                    lock=False,
                )
                if not active:
                    raise AuthorizationRevoked
                current = await resolve_project_context_in_transaction(
                    session,
                    self._context.user_id,
                    self._context.project_id,
                    self._context.request_id,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=self._context.resource_scope,
                    run_id=self._run_id,
                    lock=True,
                )
                if run is None or run.status not in {"pending", "running"}:
                    raise RunSnapshotAssetStale
                assets = await repository.list_assets_in_session(
                    session,
                    self._context,
                    self._run_id,
                    lock=True,
                )
                matching_assets = tuple(asset for asset in assets if asset.asset_kind == AssetKind.MCP.value and asset.version_id == snapshot.version_id)
                if len(matching_assets) != 1:
                    raise RunSnapshotAssetStale
                asset = matching_assets[0]
                if asset.asset_id != snapshot.asset_id or asset.asset_scope != snapshot.scope.value or asset.payload_checksum != snapshot.checksum or asset.catalog_generation != snapshot.catalog_generation:
                    raise RunSnapshotAssetStale
                persisted = tuple(
                    sorted(
                        (
                            grant
                            for grant in await repository.list_mcp_grants_in_session(
                                session,
                                self._context,
                                self._run_id,
                                lock=True,
                            )
                            if grant.mcp_version_id == snapshot.version_id
                        ),
                        key=lambda item: (
                            item.mcp_version_id.int,
                            item.credential_slot_id.int,
                            item.credential_grant_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                materialized = await self._resolver.materialize_mcp_secrets_in_session(
                    session,
                    current,
                    snapshot,
                    expected_grants=tuple(
                        (
                            grant.credential_slot_id,
                            grant.credential_grant_id,
                            grant.credential_version_id,
                        )
                        for grant in persisted
                    ),
                )
                generation = await session.scalar(select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1).with_for_update())
                if generation != snapshot.catalog_generation:
                    raise RunSnapshotAssetStale
                return materialized
        except (
            RunSnapshotAssetStale,
            AssetResolutionUnavailable,
            AssetValidationFailed,
            AssetForbidden,
        ):
            raise PrivateWorkAssetStale(self._context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(self._context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aclose(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(_remove_private_skill_tree, self.skill_root)
        self._closed = True


class PrivateAssetRuntime:
    """Build run-scoped Agent/Skill/MCP state from persisted exact IDs only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(session_factory)

    async def materialize(
        self,
        context: PrivateWorkContext,
        admitted: AdmittedPrivateRun,
        *,
        authorization_boundary: PrivateRunAuthorizationBoundary | None = None,
    ) -> PrivateAgentRuntime:
        context = require_issued_private_work_context(context)
        if type(admitted) is not AdmittedPrivateRun:
            raise PrivateWorkNotFound(context.request_id)
        authorization_boundary = authorization_boundary or PrivateRunAuthorizationBoundary(
            self._session_factory,
            project_id=admitted.run.project_id,
            owner_user_id=admitted.run.owner_user_id,
            run_id=admitted.run.run_id,
        )
        skill_snapshots: tuple[ResolvedSkillSnapshot, ...]
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=admitted.run.run_id,
                    lock=True,
                )
                if run is None or run.thread_id != admitted.thread_id or run.status != "pending":
                    raise PrivateWorkNotFound(context.request_id)
                assets = await self._snapshots.list_assets_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                grants = await self._snapshots.list_mcp_grants_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
                    raise RunSnapshotAssetStale
                persisted_generation = assets[0].catalog_generation
                if any(asset.catalog_generation != persisted_generation for asset in assets):
                    raise RunSnapshotAssetStale

                resolved: list[ResolvedAgentSnapshot | ResolvedSkillSnapshot | ResolvedMcpSnapshot] = []
                for asset in assets:
                    try:
                        kind = AssetKind(asset.asset_kind)
                    except ValueError:
                        raise RunSnapshotAssetStale from None
                    snapshot = await self._resolver.resolve_project_asset_snapshot_in_session(
                        session,
                        current,
                        AssetSelection(kind, asset.asset_id, asset.version_id),
                    )
                    if (
                        snapshot.kind is not kind
                        or snapshot.scope.value != asset.asset_scope
                        or snapshot.asset_id != asset.asset_id
                        or snapshot.version_id != asset.version_id
                        or snapshot.checksum != asset.payload_checksum
                        or snapshot.catalog_generation != persisted_generation
                    ):
                        raise RunSnapshotAssetStale
                    resolved.append(snapshot)

                agent = resolved[0]
                if type(agent) is not ResolvedAgentSnapshot:
                    raise RunSnapshotAssetStale
                expected_versions = (*agent.payload.skill_version_ids, *agent.payload.mcp_version_ids)
                if tuple(asset.version_id for asset in assets[1:]) != expected_versions:
                    raise RunSnapshotAssetStale
                skill_snapshots = tuple(item for item in resolved[1:] if type(item) is ResolvedSkillSnapshot)
                mcp_snapshots = tuple(item for item in resolved[1:] if type(item) is ResolvedMcpSnapshot)
                if len(skill_snapshots) != len(agent.payload.skill_version_ids) or len(mcp_snapshots) != len(agent.payload.mcp_version_ids):
                    raise RunSnapshotAssetStale
                current_grants = await self._snapshots.current_mcp_grants_in_session(
                    session,
                    context,
                    tuple(asset for asset in assets if asset.asset_kind == AssetKind.MCP.value),
                )
                persisted_grants = tuple(
                    sorted(
                        grants,
                        key=lambda item: (
                            item.mcp_version_id.int,
                            item.credential_slot_id.int,
                            item.credential_grant_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                if current_grants != persisted_grants:
                    raise RunSnapshotAssetStale
                generation = await session.scalar(select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1).with_for_update())
                if generation != persisted_generation:
                    raise RunSnapshotAssetStale
        except (RunSnapshotAssetStale, AssetResolutionUnavailable, AssetValidationFailed, AssetForbidden):
            raise PrivateWorkAssetStale(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        root = _create_private_skill_root(admitted.run.run_id, context.request_id)
        try:
            root.chmod(0o700)
            skill_manifests, skills = await asyncio.to_thread(_write_skill_tree, root, skill_snapshots)
            mcp_manifests = tuple(
                PrivateMcpManifest(
                    asset_id=snapshot.asset_id,
                    version_id=snapshot.version_id,
                    definition=_safe_copy(snapshot.definition),  # type: ignore[arg-type]
                )
                for snapshot in mcp_snapshots
            )
            safe_manifest = PrivateAgentManifest(
                agent_asset_id=agent.asset_id,
                agent_version_id=agent.version_id,
                checksum=agent.checksum,
                catalog_generation=agent.catalog_generation,
                description=agent.payload.description,
                soul=agent.payload.soul,
                model_ref=agent.payload.model_ref,
                tool_groups=agent.payload.tool_groups,
                skills=skill_manifests,
                mcps=mcp_manifests,
            )
            runtime = PrivateAgentRuntime(
                context=context,
                run_id=admitted.run.run_id,
                resolver=self._resolver,
                session_factory=self._session_factory,
                safe_manifest=safe_manifest,
                skill_root=root,
                skills=skills,
                mcp_snapshots=mcp_snapshots,
                authorization_boundary=authorization_boundary,
            )
            await runtime.discover_mcp_tools()
            return runtime
        except Exception as error:
            try:
                await asyncio.to_thread(_remove_private_skill_tree, root)
            except PrivateRuntimeCleanupError:
                logger.warning("Private runtime cleanup failed after materialization")
            if isinstance(error, PrivateWorkError):
                raise type(error)(context.request_id) from None
            if isinstance(error, AuthorizationRevoked):
                raise
            if isinstance(
                error,
                (
                    RunSnapshotAssetStale,
                    AssetResolutionUnavailable,
                    AssetValidationFailed,
                    AssetForbidden,
                ),
            ):
                raise PrivateWorkAssetStale(context.request_id) from None
            raise PrivateWorkUnavailable(context.request_id) from None
