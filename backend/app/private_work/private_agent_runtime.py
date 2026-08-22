from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus, unquote_plus

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.asset_runtime_contracts import (
    PrivateAgentManifest,
    PrivateMcpManifest,
    PrivateSkillManifest,
)
from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.mcp_run_sessions import (
    McpRunSession,
    McpRunSessionCache,
    McpRunSessionKey,
)
from app.private_work.mcp_runtime_contracts import (
    DiscoveredMcpTool as _DiscoveredMcpTool,
)
from app.private_work.mcp_runtime_contracts import (
    mcp_tool_inventory_payload as _mcp_tool_inventory_payload,
)
from app.private_work.mcp_runtime_contracts import (
    validate_project_mcp_material_policy as _validate_project_mcp_material_policy,
)
from app.private_work.mcp_runtime_contracts import (
    validate_project_mcp_snapshot_policy as _validate_project_mcp_snapshot_policy,
)
from app.private_work.mcp_session_owner import (
    RunMcpClientSessionOwner as _RunMcpClientSessionOwner,
)
from app.private_work.private_skill_runtime import (
    PrivateRuntimeCleanupError,
)
from app.private_work.private_skill_runtime import (
    remove_private_skill_tree as _remove_private_skill_tree,
)
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.context import resolve_project_context_in_transaction
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.mcp_secret_store import mcp_secret_closure_digest
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRepository,
)
from app.shared_assets.models import (
    AgentModelSettings,
    AssetKind,
    AssetScope,
    ResolvedMcpSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.skill_secret_store import skill_secret_recipient
from app.system_settings.model_refs import DEFAULT_MODEL_REF, resolve_model_ref
from deerflow.agents.lead_agent.prompt import AgentPromptBundle
from deerflow.config.app_config import peek_current_app_config
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp.schema_projection import (
    safe_mcp_args_model as _safe_mcp_args_model,
)
from deerflow.mcp_definition_policy import (
    McpDefinitionPolicyError,
    McpEndpointPolicy,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.secrets import (
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
)
from deerflow.skills.types import Skill
from deerflow.subagents.runtime_catalog import (
    RuntimeAgentCatalog,
    build_runtime_agent_catalog,
    build_runtime_agent_profile,
)
from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool
from deerflow.utils.asyncio import joined_to_thread

logger = logging.getLogger("app.private_work.asset_runtime")

_DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 15
_DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS = 60
_MCP_CLOSE_TIMEOUT_SECONDS = 1
_MCP_TOOL_INVENTORY_WRITE_TIMEOUT_SECONDS = 2
_MAX_MCP_TOOLS_PER_SERVER = 128
_VALID_MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_MCP_SECRET_SUBSTRING_MIN_LENGTH = 8
_MCP_DISCOVERY_UNAVAILABLE = "mcp_discovery_unavailable"
_MCP_CATALOG_INVALID = "mcp_catalog_invalid"


class _McpDiscoveryUnavailable(PrivateWorkUnavailable):
    """Remote discovery failed after the exact MCP closure was materialized."""


class _McpCatalogInvalid(PrivateWorkAssetStale):
    """The remote MCP catalog was rejected after closure revalidation."""


def _validated_mcp_runtime_timeout(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 300:
        raise ValueError("invalid private MCP runtime timeout")
    return value


class _EmptyMcpArgs(BaseModel):
    pass


class PrivateAgentRuntime:
    """Run-owned exact assets.  Its repr and public manifest are secret-free."""

    __slots__ = (
        "_agent_catalog",
        "_all_skill_manifests",
        "_all_skills",
        "_authorization_boundary",
        "_capability_issues",
        "_closed",
        "_closing",
        "_context",
        "_delegate_manifests",
        "_delegate_model_names",
        "_discovery_timeout_seconds",
        "_endpoint_policy",
        "_http_client_factory",
        "_mcp_run_sessions",
        "_mcp_snapshots",
        "_mcp_tools",
        "_mcp_tools_by_version",
        "_resolver",
        "_run_id",
        "_session_factory",
        "_tool_call_timeout_seconds",
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
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        delegate_manifests: tuple[PrivateAgentManifest, ...] = (),
        all_skill_manifests: tuple[PrivateSkillManifest, ...] | None = None,
        all_skills: tuple[Skill, ...] | None = None,
        delegate_model_names: Mapping[uuid.UUID, str] | None = None,
        run_session_reuse: bool = True,
    ) -> None:
        self._context = context
        self._run_id = run_id
        self._resolver = resolver
        self._session_factory = session_factory
        self._mcp_snapshots = mcp_snapshots
        self._mcp_run_sessions = McpRunSessionCache() if run_session_reuse else None
        self._delegate_manifests = delegate_manifests
        self._delegate_model_names = None if delegate_model_names is None else dict(delegate_model_names)
        self._authorization_boundary = authorization_boundary
        self._capability_issues: tuple[str, ...] = ()
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        self._tool_call_timeout_seconds = _validated_mcp_runtime_timeout(tool_call_timeout_seconds)
        self._closed = False
        self._closing = False
        self.safe_manifest = safe_manifest
        self.skill_root = skill_root
        self.skills = skills
        self._all_skill_manifests = safe_manifest.skills if all_skill_manifests is None else all_skill_manifests
        self._all_skills = skills if all_skills is None else all_skills
        if len(self._all_skill_manifests) != len(self._all_skills):
            raise ValueError("private Skill manifests do not match runtime Skills")
        self._mcp_tools: tuple[StructuredTool, ...] = ()
        self._mcp_tools_by_version: dict[
            uuid.UUID,
            tuple[StructuredTool, ...],
        ] = {}
        self._agent_catalog = build_runtime_agent_catalog(())

    def __repr__(self) -> str:
        return f"PrivateAgentRuntime(run_id={self._run_id!r}, agent_version_id={self.agent_version_id!r}, closed={self._closed!r})"

    @property
    def agent_version_id(self) -> uuid.UUID:
        return self.safe_manifest.agent_version_id

    @property
    def model_ref(self) -> str:
        return self.safe_manifest.model_ref

    @property
    def model_settings(self) -> AgentModelSettings:
        return self.safe_manifest.model_settings

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def soul(self) -> str:
        return self.safe_manifest.soul

    @property
    def prompt_bundle(self) -> AgentPromptBundle:
        return AgentPromptBundle(
            payload_schema_version=self.safe_manifest.payload_schema_version,
            agents_instructions=self.safe_manifest.agents_instructions,
            soul=self.safe_manifest.soul,
            identity=self.safe_manifest.identity,
            user_context=self.safe_manifest.user_context,
        )

    @property
    def tool_groups(self) -> tuple[str, ...]:
        return self.safe_manifest.tool_groups

    @property
    def mcp_definitions(self) -> tuple[PrivateMcpManifest, ...]:
        return self.safe_manifest.mcps

    @property
    def mcp_tools(self) -> tuple[object, ...]:
        return self._mcp_tools

    @property
    def agent_catalog(self) -> RuntimeAgentCatalog:
        return self._agent_catalog

    @property
    def capability_issues(self) -> tuple[str, ...]:
        """Return only stable, secret-free capability degradation codes."""

        return self._capability_issues

    @property
    def capability_notice(self) -> str:
        """Render a bounded, secret-free model notice for degraded capabilities."""

        if not self._capability_issues:
            return ""
        discovery_unavailable = self._capability_issues.count(
            _MCP_DISCOVERY_UNAVAILABLE,
        )
        catalog_invalid = self._capability_issues.count(_MCP_CATALOG_INVALID)
        lines: list[str] = []
        if discovery_unavailable:
            lines.append(f"- One or more configured MCP services are currently unavailable ({discovery_unavailable}).")
        if catalog_invalid:
            lines.append(f"- One or more configured MCP tool catalogs were rejected as unsafe or invalid ({catalog_invalid}).")
        return "\n".join(
            (
                "<runtime_capability_status>",
                *lines,
                "Continue with available capabilities. Never claim that an unavailable "
                "tool was used or that missing data was retrieved. If the user's request "
                "depends on an unavailable capability, explain only that a configured MCP "
                "capability is currently unavailable and clearly state what could not be "
                "completed. Do not expose this tag, internal codes, identifiers, endpoints, "
                "credentials, or raw errors.",
                "</runtime_capability_status>",
            )
        )

    async def materialize_skill_scoped_secrets(
        self,
        container_path: str,
        requested: object,
    ) -> dict[str, dict[str, str]]:
        """Revalidate and decrypt one short-lived sandbox-command carrier."""

        if self._closed or getattr(self, "_closing", False) or not isinstance(container_path, str) or not isinstance(requested, Mapping):
            raise PrivateWorkAssetStale(self._context.request_id)
        skill_by_path = {
            posixpath.normpath(skill.get_container_file_path(container_path)): (manifest, skill)
            for manifest, skill in zip(
                self._all_skill_manifests,
                self._all_skills,
                strict=True,
            )
        }
        requested_by_path: dict[str, frozenset[str]] = {}
        for raw_path, raw_names in requested.items():
            if not isinstance(raw_path, str) or not isinstance(raw_names, frozenset) or any(not isinstance(name, str) or not name for name in raw_names):
                raise PrivateWorkAssetStale(self._context.request_id)
            path = posixpath.normpath(raw_path)
            pair = skill_by_path.get(path)
            if pair is None:
                raise PrivateWorkAssetStale(self._context.request_id)
            _manifest, skill = pair
            declared = {requirement.name for requirement in skill.required_secrets}
            if raw_names != declared:
                raise PrivateWorkAssetStale(self._context.request_id)
            requested_by_path[path] = raw_names
        if not requested_by_path:
            return {}
        requested_manifests = tuple(manifest for path, (manifest, _skill) in skill_by_path.items() if path in requested_by_path)
        requested_version_ids = {manifest.version_id for manifest in requested_manifests}
        repository = RunSnapshotRepository(
            self._session_factory,
            endpoint_policy=self._endpoint_policy,
        )
        values_by_version: dict[uuid.UUID, dict[str, str]] = {manifest.version_id: {} for manifest in requested_manifests}
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
                await resolve_project_context_in_transaction(
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
                skill_assets = tuple(asset for asset in assets if (asset.asset_kind == AssetKind.SKILL.value and asset.version_id in requested_version_ids))
                if tuple((asset.asset_id, asset.version_id) for asset in skill_assets) != tuple((manifest.asset_id, manifest.version_id) for manifest in requested_manifests):
                    raise RunSnapshotAssetStale
                persisted = tuple(
                    sorted(
                        (
                            item
                            for item in await repository.list_skill_secrets_in_session(
                                session,
                                self._context,
                                self._run_id,
                                lock=True,
                            )
                            if item.skill_version_id in requested_version_ids
                        ),
                        key=lambda item: (
                            item.skill_version_id.int,
                            item.secret_name,
                            item.secret_generation_id.int,
                        ),
                    )
                )
                skill_runtime_by_version = {manifest.version_id: (path, skill) for path, (manifest, skill) in skill_by_path.items() if manifest.version_id in requested_version_ids}
                if set(skill_runtime_by_version) != requested_version_ids:
                    raise RunSnapshotAssetStale
                materials = await repository.lock_admitted_skill_secrets_in_session(
                    session,
                    self._context,
                    persisted,
                    declared_targets=frozenset((manifest.version_id, requirement.name) for manifest in requested_manifests for requirement in skill_runtime_by_version[manifest.version_id][1].required_secrets),
                    required_targets=frozenset((manifest.version_id, requirement.name) for manifest in requested_manifests for requirement in skill_runtime_by_version[manifest.version_id][1].required_secrets if not requirement.optional),
                    load_envelopes=True,
                )
                secret_key: SecretKey | None = None
                for manifest in requested_manifests:
                    skill_path = skill_runtime_by_version[manifest.version_id][0]
                    requested_names = requested_by_path.get(
                        skill_path,
                        frozenset(),
                    )
                    for material in materials:
                        if material.skill_version_id != manifest.version_id:
                            continue
                        if material.secret_name not in requested_names:
                            continue
                        envelope = material.envelope
                        if envelope is None:
                            raise RunSnapshotAssetStale
                        if secret_key is None:
                            try:
                                secret_key = SecretKey.from_environment()
                            except SecretKeyInvalid:
                                raise AssetStorageUnavailable(self._context.request_id) from None
                        try:
                            plaintext = await joined_to_thread(
                                envelope.materialize,
                                recipient=skill_secret_recipient(
                                    self._context.project_id,
                                    material.skill_id,
                                    material.skill_version_id,
                                    material.secret_name,
                                ),
                                key=secret_key,
                            )
                            value = plaintext.decode("utf-8")
                            if not value or "\x00" in value:
                                raise RunSnapshotAssetStale
                            values_by_version[manifest.version_id][material.secret_name] = value
                        except (SecretMaterializationFailed, UnicodeError):
                            raise RunSnapshotAssetStale from None
            result = {path: dict(values_by_version[manifest.version_id]) for path, (manifest, _skill) in skill_by_path.items() if path in requested_by_path}
            return result
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
        except AuthorizationRevoked:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        finally:
            for values in values_by_version.values():
                values.clear()
            values_by_version.clear()

    def set_authorization_boundary(self, boundary: object) -> None:
        self._authorization_boundary = boundary

    async def discover_mcp_tools(self) -> None:
        """Copy remote schemas into run-local proxies, never remote tool objects."""

        schemas: list[_DiscoveredMcpTool] = []
        capability_issues: list[str] = []
        for snapshot in self._mcp_snapshots:
            session_key = self._mcp_session_key(snapshot.version_id)
            session_cache = self._mcp_run_sessions if session_key is not None else None

            async def discover_exact(
                definition: Mapping[str, object],
                material: Mapping[str, Mapping[str, object]],
                *,
                version_id: uuid.UUID = snapshot.version_id,
                current_session_cache: McpRunSessionCache | None = session_cache,
                current_session_key: McpRunSessionKey | None = session_key,
            ) -> tuple[_DiscoveredMcpTool, ...]:
                try:
                    return await self._discover_exact_mcp(
                        version_id,
                        definition,
                        material,
                        authorization_boundary=self._authorization_boundary,
                        http_client_factory=self._http_client_factory,
                        discovery_timeout_seconds=self._discovery_timeout_seconds,
                        session_cache=current_session_cache,
                        session_key=current_session_key,
                    )
                except AuthorizationRevoked:
                    raise
                except PrivateWorkAssetStale:
                    # The exact closure has already been revalidated and
                    # materialized. A stale signal from this inner operation is
                    # therefore a rejected remote catalog, not snapshot drift.
                    raise _McpCatalogInvalid(self._context.request_id) from None
                except PrivateWorkUnavailable:
                    raise _McpDiscoveryUnavailable(
                        self._context.request_id,
                    ) from None
                except Exception:
                    raise _McpDiscoveryUnavailable(
                        self._context.request_id,
                    ) from None

            try:
                discovered = await self.invoke_with_mcp_material(
                    snapshot.version_id,
                    discover_exact,
                )
            except _McpCatalogInvalid:
                await self._record_mcp_tool_inventory(
                    snapshot,
                    error_code=_MCP_CATALOG_INVALID,
                )
                capability_issues.append(_MCP_CATALOG_INVALID)
                continue
            except _McpDiscoveryUnavailable:
                await self._record_mcp_tool_inventory(
                    snapshot,
                    error_code=_MCP_DISCOVERY_UNAVAILABLE,
                )
                capability_issues.append(_MCP_DISCOVERY_UNAVAILABLE)
                continue
            except PrivateWorkAssetStale:
                await self._record_mcp_tool_inventory(
                    snapshot,
                    error_code=_MCP_CATALOG_INVALID,
                )
                raise
            except PrivateWorkUnavailable:
                await self._record_mcp_tool_inventory(
                    snapshot,
                    error_code=_MCP_DISCOVERY_UNAVAILABLE,
                )
                raise
            await self._record_mcp_tool_inventory(snapshot, tools=discovered)
            schemas.extend(discovered)
        tools_by_version: dict[uuid.UUID, list[StructuredTool]] = {snapshot.version_id: [] for snapshot in self._mcp_snapshots}
        for schema in schemas:
            tools_by_version.setdefault(schema.version_id, []).append(self._proxy_tool(schema))
        self._mcp_tools_by_version = {version_id: tuple(tools) for version_id, tools in tools_by_version.items()}
        try:
            lead_mcp_version_ids = tuple(manifest.version_id for manifest in self.safe_manifest.mcps)
            if not lead_mcp_version_ids and not self._delegate_manifests:
                # Compatibility for isolated runtime tests and older callers
                # that construct a single-Agent runtime directly. Production
                # materialization always supplies the lead manifest closure.
                lead_mcp_version_ids = tuple(snapshot.version_id for snapshot in self._mcp_snapshots)
            self._mcp_tools = tuple(tool for version_id in lead_mcp_version_ids for tool in self._mcp_tools_by_version[version_id])
        except KeyError:
            raise PrivateWorkAssetStale(self._context.request_id) from None
        self._capability_issues = tuple(capability_issues)
        self._build_agent_catalog()

    def _build_agent_catalog(self) -> None:
        if not self._delegate_manifests:
            self._agent_catalog = build_runtime_agent_catalog(())
            return
        app_config = peek_current_app_config()
        if app_config is None:
            raise PrivateWorkAssetStale(self._context.request_id)
        if self._delegate_model_names is not None and set(self._delegate_model_names) != {manifest.agent_version_id for manifest in self._delegate_manifests}:
            raise PrivateWorkAssetStale(self._context.request_id)
        skills_by_version = {
            manifest.version_id: skill
            for manifest, skill in zip(
                self._all_skill_manifests,
                self._all_skills,
                strict=True,
            )
        }
        profiles = []
        try:
            for manifest in self._delegate_manifests:
                if manifest.runtime_key is None:
                    raise RunSnapshotAssetStale
                if self._delegate_model_names is None:
                    model = resolve_model_ref(app_config, manifest.model_ref)
                    model_name = getattr(model, "name", None)
                else:
                    model_name = self._delegate_model_names.get(manifest.agent_version_id)
                    model = app_config.get_model_config(model_name) if isinstance(model_name, str) else None
                if not isinstance(model_name, str) or not model_name:
                    raise RunSnapshotAssetStale
                if getattr(model, "name", None) != model_name:
                    raise RunSnapshotAssetStale
                if manifest.model_ref != DEFAULT_MODEL_REF and manifest.model_ref != model_name:
                    raise RunSnapshotAssetStale
                runtime_skills = tuple(skills_by_version[item.version_id] for item in manifest.skills)
                mcp_tools = tuple(tool for item in manifest.mcps for tool in self._mcp_tools_by_version[item.version_id])
                profiles.append(
                    build_runtime_agent_profile(
                        key=manifest.runtime_key,
                        description=manifest.description,
                        model_name=model_name,
                        model_settings=manifest.model_settings,
                        tool_groups=manifest.tool_groups,
                        prompt_bundle=AgentPromptBundle(
                            payload_schema_version=manifest.payload_schema_version,
                            agents_instructions=manifest.agents_instructions,
                            soul=manifest.soul,
                            identity=manifest.identity,
                            user_context=manifest.user_context,
                        ),
                        runtime_skills=runtime_skills,
                        mcp_tools=mcp_tools,
                    )
                )
            self._agent_catalog = build_runtime_agent_catalog(tuple(profiles))
        except (KeyError, TypeError, ValueError, RunSnapshotAssetStale):
            raise PrivateWorkAssetStale(self._context.request_id) from None

    async def _record_mcp_tool_inventory(
        self,
        snapshot: ResolvedMcpSnapshot,
        *,
        tools: tuple[_DiscoveredMcpTool, ...] | None = None,
        error_code: Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None = None,
    ) -> None:
        """Best-effort diagnostic write after a fresh, Worker-owned discovery."""

        if (tools is None) == (error_code is None):
            return
        try:
            context = require_issued_private_work_context(self._context)
        except PrivateWorkNotFound:
            return
        try:
            async with asyncio.timeout(_MCP_TOOL_INVENTORY_WRITE_TIMEOUT_SECONDS):
                await self._persist_mcp_tool_inventory(
                    context,
                    snapshot,
                    tools=tools,
                    error_code=error_code,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Inventory is observational only and must never turn a valid Run
            # discovery into a runtime failure or expose raw storage details.
            logger.warning("MCP tool inventory update was skipped")

    async def _persist_mcp_tool_inventory(
        self,
        context: PrivateWorkContext,
        snapshot: ResolvedMcpSnapshot,
        *,
        tools: tuple[_DiscoveredMcpTool, ...] | None,
        error_code: Literal[
            "mcp_discovery_unavailable",
            "mcp_catalog_invalid",
        ]
        | None,
    ) -> None:
        snapshots = RunSnapshotRepository(
            self._session_factory,
            endpoint_policy=self._endpoint_policy,
        )
        async with self._session_factory() as session, session.begin():
            if not await PrivateRunAuthorizationService.is_active(
                session,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                run_id=self._run_id,
                lock=False,
            ):
                return
            await resolve_project_context_in_transaction(
                session,
                context.user_id,
                context.project_id,
                context.request_id,
                lock=True,
            )
            run = await PrivateRunRepository(session).get(
                scope=context.resource_scope,
                run_id=self._run_id,
                lock=True,
            )
            if run is None or run.status not in {"pending", "running"}:
                return
            assets = await snapshots.list_assets_in_session(
                session,
                context,
                self._run_id,
                lock=True,
            )
            matching = tuple(asset for asset in assets if asset.asset_kind == AssetKind.MCP.value and asset.version_id == snapshot.version_id)
            if len(matching) != 1:
                return
            asset = matching[0]
            if asset.asset_id != snapshot.asset_id or asset.asset_scope != snapshot.scope.value or asset.payload_checksum != snapshot.checksum:
                return
            persisted = tuple(
                sorted(
                    (
                        secret
                        for secret in await snapshots.list_mcp_secrets_in_session(
                            session,
                            context,
                            self._run_id,
                            lock=True,
                        )
                        if secret.mcp_server_version_id == snapshot.version_id
                    ),
                    key=lambda item: (
                        item.mcp_server_version_id.int,
                        item.slot_id.int,
                        item.secret_generation_id.int,
                    ),
                )
            )
            current = await snapshots.current_mcp_secrets_in_session(
                session,
                context,
                matching,
            )
            current_for_version = tuple(item for item in current if item.mcp_server_version_id == snapshot.version_id)
            if current_for_version != persisted:
                return
            generation_ids = tuple(item.secret_generation_id for item in persisted)
            if set(generation_ids) != set(snapshot.secret_generation_ids):
                return
            inventory = McpToolInventoryRepository(session)
            common = {
                "project_id": context.project_id,
                "mcp_server_id": snapshot.asset_id,
                "mcp_server_version_id": snapshot.version_id,
                "payload_checksum": snapshot.checksum,
                "secret_digest": mcp_secret_closure_digest(persisted),
            }
            if tools is not None:
                await inventory.record_success(
                    **common,
                    tools=_mcp_tool_inventory_payload(tools),
                )
            elif error_code is not None:
                await inventory.record_failure(
                    **common,
                    public_error_code=error_code,
                )

    def _mcp_session_key(self, version_id: uuid.UUID) -> McpRunSessionKey | None:
        """Bind session reuse to the exact snapshot and secret closure."""
        if self._mcp_run_sessions is None:
            return None
        snapshot = next((item for item in self._mcp_snapshots if item.version_id == version_id), None)
        if snapshot is None or snapshot.scope is not AssetScope.PROJECT or snapshot.definition.get("transport") not in {"http", "sse"}:
            return None
        return (
            snapshot.version_id,
            snapshot.checksum,
            snapshot.secret_digest,
        )

    def _proxy_tool(self, schema: _DiscoveredMcpTool) -> StructuredTool:
        session_key = self._mcp_session_key(schema.version_id)
        session_cache = self._mcp_run_sessions if session_key is not None else None

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
                    http_client_factory=self._http_client_factory,
                    discovery_timeout_seconds=self._discovery_timeout_seconds,
                    tool_call_timeout_seconds=self._tool_call_timeout_seconds,
                    session_cache=session_cache,
                    session_key=session_key,
                ),
            )

        proxy = StructuredTool.from_function(
            coroutine=invoke,
            name=schema.name,
            description=schema.description,
            args_schema=schema.args_schema,
        )
        from deerflow.tools.mcp_metadata import tag_private_mcp_tool

        tag_private_mcp_tool(proxy)
        tag_mcp_tool(proxy)
        if schema.routing is not None:
            tag_mcp_routing(proxy, schema.routing)
        return proxy

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
        *,
        extra_forbidden_values: tuple[
            str | bytes | bool | int | float,
            ...,
        ] = (),
    ) -> None:
        forbidden = (*cls._material_values(material), *extra_forbidden_values)
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
        text_values: tuple[str, ...] = ()
        if isinstance(value, str):
            decoded_values: list[str] = []
            current = value
            for _ in range(2):
                decoded = unquote_plus(current)
                if decoded == current:
                    break
                decoded_values.append(decoded)
                current = decoded
            text_values = (value, *decoded_values)
        for secret in forbidden:
            if isinstance(value, str) and isinstance(secret, str):
                if any(candidate == secret or (len(secret) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and secret in candidate) for candidate in text_values):
                    return True
            elif isinstance(value, bytes) and isinstance(secret, bytes):
                if value == secret or (len(secret) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and secret in value):
                    return True
            elif isinstance(value, str) and isinstance(secret, bytes):
                decoded = secret.decode("utf-8", errors="ignore")
                if decoded and any(candidate == decoded or (len(decoded) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and decoded in candidate) for candidate in text_values):
                    return True
            elif isinstance(value, bytes) and isinstance(secret, str):
                encoded = secret.encode()
                if encoded and (value == encoded or (len(encoded) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and encoded in value)):
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
    async def _close_project_mcp_client(client: object) -> None:
        if client is None:
            return
        close = getattr(client, "aclose", None)
        if not callable(close):
            return
        try:
            async with asyncio.timeout(_MCP_CLOSE_TIMEOUT_SECONDS):
                await close()
        except Exception:
            pass

    @staticmethod
    async def _open_project_mcp_session(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        _bind_live_client_session: bool = False,
    ) -> McpRunSession:
        """Load one MCP tool set and return its closable transport owner.

        Returns ``(client, tools, derived_secrets)``. The derived-secrets list
        is live: the OAuth interceptor keeps appending refreshed tokens to it,
        so result sanitization always sees the current closure. The caller
        owns closing the client. Run reuse opts into tools bound to one entered
        ClientSession; discovery/system one-shot callers keep the adapter's
        connection-bound wrappers.
        """
        discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        client = None
        derived_secrets: list[str] = []
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            from deerflow.mcp.client import build_servers_config
            from deerflow.mcp.config import ExtensionsConfig, McpServerConfig
            from deerflow.mcp.oauth import OAuthTokenManager
            from deerflow.mcp.tools import (
                _catalog_mcp_definition,
                _catalog_oauth_configs,
                _merge_catalog_mcp_secrets,
            )

            server_name = f"project_{version_id.hex[:16]}"
            raw = _catalog_mcp_definition(definition)
            server = McpServerConfig.model_validate(raw)
            extensions = ExtensionsConfig(mcpServers={server_name: server})
            server_config = build_servers_config(extensions)[server_name]
            merged_config = _merge_catalog_mcp_secrets(server_config, material)
            # Query values are URL-encoded before transport. Track both that
            # derived representation and the materialized URL so a remote MCP
            # cannot echo either form into tool metadata or results.
            has_query_material = False
            for payload in material.values():
                query = payload.get("query")
                if not isinstance(query, Mapping) or not query:
                    continue
                has_query_material = True
                for value in query.values():
                    if isinstance(value, str):
                        encoded = quote_plus(value)
                        if encoded and encoded not in derived_secrets:
                            derived_secrets.append(encoded)
            if has_query_material:
                materialized_url = merged_config.get("url")
                if isinstance(materialized_url, str) and materialized_url and materialized_url not in derived_secrets:
                    derived_secrets.append(materialized_url)
            if merged_config.get("transport") in {"http", "sse"} and http_client_factory is not None:
                merged_config["httpx_client_factory"] = http_client_factory

            def remember_authorization(authorization: str | None) -> None:
                if not authorization:
                    return
                candidates = (authorization, authorization.partition(" ")[2])
                for candidate in candidates:
                    if candidate and candidate not in derived_secrets:
                        derived_secrets.append(candidate)

            try:
                async with asyncio.timeout(discovery_timeout_seconds):
                    if authorization_boundary is not None:
                        await authorization_boundary.before_mcp_call()
                    tool_interceptors: list[object] = []
                    catalog_oauth = _catalog_oauth_configs(
                        extensions,
                        {server_name: material},
                    )
                    if catalog_oauth:
                        token_manager = OAuthTokenManager(catalog_oauth)
                        authorization = await token_manager.get_authorization_header(server_name)
                        remember_authorization(authorization)
                        if authorization:
                            headers = dict(merged_config.get("headers") or {})
                            headers["Authorization"] = authorization
                            merged_config["headers"] = headers

                        async def catalog_oauth_interceptor(
                            request: Any,
                            handler: Any,
                        ) -> Any:
                            refreshed = await token_manager.get_authorization_header(request.server_name)
                            remember_authorization(refreshed)
                            if not refreshed:
                                return await handler(request)
                            headers = dict(request.headers or {})
                            headers["Authorization"] = refreshed
                            return await handler(request.override(headers=headers))

                        tool_interceptors.append(catalog_oauth_interceptor)
                    client_kwargs: dict[str, object] = {
                        "tool_name_prefix": True,
                    }
                    if tool_interceptors:
                        client_kwargs["tool_interceptors"] = tool_interceptors
                    client = MultiServerMCPClient(
                        {server_name: merged_config},
                        **client_kwargs,
                    )
                    if _bind_live_client_session:
                        from langchain_mcp_adapters.tools import load_mcp_tools

                        if merged_config.get("transport") not in {"http", "sse"}:
                            raise PrivateWorkUnavailable("unknown")
                        adapter_client = client

                        async def load_live_tools(session: object) -> tuple[object, ...]:
                            return tuple(
                                await load_mcp_tools(
                                    session,  # type: ignore[arg-type]
                                    callbacks=adapter_client.callbacks,
                                    tool_interceptors=tool_interceptors,
                                    server_name=server_name,
                                    tool_name_prefix=True,
                                )
                            )

                        client, remote_tools = await _RunMcpClientSessionOwner.open(
                            adapter_client.session(server_name),
                            load_live_tools,
                        )
                    else:
                        remote_tools = tuple(await client.get_tools(server_name=server_name))
            except TimeoutError:
                raise PrivateWorkUnavailable("unknown") from None
            return client, remote_tools, derived_secrets
        except BaseException:
            await PrivateAgentRuntime._close_project_mcp_client(client)
            derived_secrets.clear()
            raise

    @staticmethod
    async def _open_reused_project_mcp_session(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
    ) -> McpRunSession:
        """Open the project HTTP/SSE session used by the Run-level cache."""

        if definition.get("transport") not in {"http", "sse"}:
            raise PrivateWorkUnavailable("unknown")
        return await PrivateAgentRuntime._open_project_mcp_session(
            version_id,
            definition,
            material,
            authorization_boundary,
            http_client_factory=http_client_factory,
            discovery_timeout_seconds=discovery_timeout_seconds,
            _bind_live_client_session=True,
        )

    @staticmethod
    async def _with_one_shot_mcp_tools(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        operation: Callable[
            [
                tuple[object, ...],
                list[str],
            ],
            Awaitable[Any],
        ],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        operation_timeout_seconds: int | None = None,
    ) -> Any:
        if operation_timeout_seconds is not None:
            operation_timeout_seconds = _validated_mcp_runtime_timeout(operation_timeout_seconds)
        client = None
        derived_secrets: list[str] = []
        try:
            client, remote_tools, derived_secrets = await PrivateAgentRuntime._open_project_mcp_session(
                version_id,
                definition,
                material,
                authorization_boundary,
                http_client_factory=http_client_factory,
                discovery_timeout_seconds=discovery_timeout_seconds,
            )
            if operation_timeout_seconds is None:
                return await operation(remote_tools, derived_secrets)
            try:
                async with asyncio.timeout(operation_timeout_seconds):
                    return await operation(remote_tools, derived_secrets)
            except TimeoutError:
                raise PrivateWorkUnavailable("unknown") from None
        finally:
            await PrivateAgentRuntime._close_project_mcp_client(client)
            client = None
            derived_secrets.clear()

    @classmethod
    async def _discover_exact_mcp(
        cls,
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int | None = None,
        session_cache: McpRunSessionCache | None = None,
        session_key: McpRunSessionKey | None = None,
    ) -> tuple[_DiscoveredMcpTool, ...]:
        forbidden_values = cls._material_values(material)

        async def copy_schemas(
            remote_tools: tuple[object, ...],
            derived_secrets: list[str] | None = None,
        ) -> tuple[_DiscoveredMcpTool, ...]:
            if len(remote_tools) > _MAX_MCP_TOOLS_PER_SERVER:
                raise PrivateWorkAssetStale("unknown")
            from deerflow.mcp.config import (
                McpServerConfig,
                resolve_effective_mcp_routing,
            )
            from deerflow.mcp.tools import _catalog_mcp_definition

            try:
                server_config = McpServerConfig.model_validate(_catalog_mcp_definition(definition))
            except Exception:
                raise PrivateWorkAssetStale("unknown") from None
            server_prefix = f"project_{version_id.hex[:16]}_"
            copied: list[_DiscoveredMcpTool] = []
            provider_names: set[str] = set()
            for index, remote in enumerate(remote_tools):
                raw_name = getattr(remote, "name", None)
                if not isinstance(raw_name, str) or _VALID_MCP_TOOL_NAME.fullmatch(raw_name) is None:
                    raise PrivateWorkAssetStale("unknown")
                name = raw_name
                description = str(getattr(remote, "description", ""))
                args_schema = getattr(remote, "args_schema", None)
                if args_schema is None:
                    get_schema = getattr(remote, "get_input_schema", None)
                    args_schema = get_schema() if callable(get_schema) else _EmptyMcpArgs
                try:
                    if isinstance(args_schema, Mapping):
                        args_schema = _safe_mcp_args_model(
                            args_schema,
                            model_name=(f"PrivateMcpArgs{version_id.hex}{index}"),
                        )
                    if not name or len(name) > 255 or len(description) > 20_000 or not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
                        raise ValueError
                    original_name = name[len(server_prefix) :] if name.startswith(server_prefix) else name
                    if not original_name or len(original_name) > 255 or _VALID_MCP_TOOL_NAME.fullmatch(original_name) is None or original_name in provider_names:
                        raise ValueError
                    provider_names.add(original_name)
                    routing = resolve_effective_mcp_routing(
                        server_config,
                        original_name,
                    )
                except Exception:
                    raise PrivateWorkAssetStale("unknown")
                cls._assert_value_secret_free(
                    (
                        name,
                        description,
                        args_schema.model_json_schema(),
                        routing,
                    ),
                    (*forbidden_values, *(derived_secrets or ())),
                    PrivateWorkAssetStale,
                )
                copied.append(
                    _DiscoveredMcpTool(
                        version_id=version_id,
                        name=name,
                        provider_name=original_name,
                        description=description,
                        args_schema=args_schema,
                        routing=(dict(routing) if routing.get("mode") != "off" else None),
                    )
                )
            return tuple(copied)

        effective_timeout = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS if discovery_timeout_seconds is None else discovery_timeout_seconds
        if session_cache is not None and session_key is not None:

            async def open_session() -> McpRunSession:
                return await cls._open_reused_project_mcp_session(
                    version_id,
                    definition,
                    material,
                    authorization_boundary,
                    http_client_factory=http_client_factory,
                    discovery_timeout_seconds=effective_timeout,
                )

            return await session_cache.call(
                session_key,
                open_session,
                copy_schemas,
                call_timeout_seconds=_validated_mcp_runtime_timeout(effective_timeout),
            )
        if authorization_boundary is None and http_client_factory is None and discovery_timeout_seconds is None:
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
            http_client_factory=http_client_factory,
            discovery_timeout_seconds=effective_timeout,
            operation_timeout_seconds=effective_timeout,
        )

    @staticmethod
    async def _invoke_exact_mcp(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        tool_name: str,
        arguments: Mapping[str, object],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        session_cache: McpRunSessionCache | None = None,
        session_key: McpRunSessionKey | None = None,
    ) -> Any:
        try:

            async def call_selected(
                discovered: tuple[object, ...],
                derived_secrets: list[str] | None = None,
            ) -> Any:
                selected = next((tool for tool in discovered if getattr(tool, "name", None) == tool_name), None)
                if selected is None:
                    raise PrivateWorkAssetStale("unknown")
                if authorization_boundary is not None:
                    dispatch_check = getattr(
                        authorization_boundary,
                        "before_mcp_tool_dispatch",
                        None,
                    )
                    if callable(dispatch_check):
                        await dispatch_check()
                    else:
                        await authorization_boundary.before_mcp_call()
                result = await selected.ainvoke(dict(arguments))
                PrivateAgentRuntime._assert_mcp_result_secret_free(
                    result,
                    material,
                    extra_forbidden_values=tuple(derived_secrets or ()),
                )
                return result

            if session_cache is not None and session_key is not None:
                # Run-level session reuse (U3): DB-side revalidation already
                # happened in invoke_with_mcp_material; only the transport
                # handshake is skipped here.
                async def open_session() -> McpRunSession:
                    return await PrivateAgentRuntime._open_reused_project_mcp_session(
                        version_id,
                        definition,
                        material,
                        authorization_boundary,
                        http_client_factory=http_client_factory,
                        discovery_timeout_seconds=discovery_timeout_seconds,
                    )

                return await session_cache.call(
                    session_key,
                    open_session,
                    call_selected,
                    call_timeout_seconds=_validated_mcp_runtime_timeout(tool_call_timeout_seconds),
                )

            if authorization_boundary is None:
                return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                    version_id,
                    definition,
                    material,
                    call_selected,
                    http_client_factory=http_client_factory,
                    discovery_timeout_seconds=discovery_timeout_seconds,
                    operation_timeout_seconds=tool_call_timeout_seconds,
                )
            return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                version_id,
                definition,
                material,
                call_selected,
                authorization_boundary,
                http_client_factory=http_client_factory,
                discovery_timeout_seconds=discovery_timeout_seconds,
                operation_timeout_seconds=tool_call_timeout_seconds,
            )
        except PrivateWorkError:
            raise
        except AuthorizationRevoked:
            raise
        except Exception:
            raise PrivateWorkUnavailable("unknown") from None

    def _validate_project_mcp_snapshot(
        self,
        snapshot: ResolvedMcpSnapshot,
    ) -> None:
        _validate_project_mcp_snapshot_policy(
            snapshot,
            endpoint_policy=self._endpoint_policy,
            http_client_factory=self._http_client_factory,
        )

    @staticmethod
    def _validate_project_mcp_material(
        snapshot: ResolvedMcpSnapshot,
        material: Mapping[str, Mapping[str, object]],
    ) -> None:
        _validate_project_mcp_material_policy(snapshot, material)

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
        try:
            self._validate_project_mcp_snapshot(snapshot)
        except McpDefinitionPolicyError:
            raise PrivateWorkAssetStale(self._context.request_id) from None
        if self._authorization_boundary is not None:
            await self._authorization_boundary.before_mcp_call()
        materialized = await self._materialize_mcp_call(snapshot)
        try:
            try:
                self._validate_project_mcp_material(
                    snapshot,
                    materialized.by_slot,
                )
                return await operation(snapshot.definition, materialized.by_slot)
            finally:
                del materialized
        except (
            AssetResolutionUnavailable,
            AssetValidationFailed,
            AssetForbidden,
            McpDefinitionPolicyError,
        ):
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

        repository = RunSnapshotRepository(
            self._session_factory,
            endpoint_policy=self._endpoint_policy,
        )
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
                if asset.asset_id != snapshot.asset_id or asset.asset_scope != snapshot.scope.value or asset.payload_checksum != snapshot.checksum:
                    raise RunSnapshotAssetStale
                persisted = tuple(
                    sorted(
                        (
                            secret
                            for secret in await repository.list_mcp_secrets_in_session(
                                session,
                                self._context,
                                self._run_id,
                                lock=True,
                            )
                            if secret.mcp_server_version_id == snapshot.version_id
                        ),
                        key=lambda item: (
                            item.mcp_server_version_id.int,
                            item.slot_id.int,
                            item.secret_generation_id.int,
                        ),
                    )
                )
                materialized = await self._resolver.materialize_mcp_secrets_in_session(
                    session,
                    current,
                    snapshot,
                    expected_secrets=tuple(
                        (
                            secret.slot_id,
                            secret.secret_generation_id,
                            secret.secret_generation_digest,
                        )
                        for secret in persisted
                    ),
                )
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
        if getattr(self, "_closed", False):
            return
        if getattr(self, "_closing", False):
            raise PrivateRuntimeCleanupError("Private runtime cleanup is already in progress")
        self._closing = True
        # Run end: close reused MCP transports and clear their derived-secret
        # closures before removing the skill tree. Best-effort by design.
        sessions = getattr(self, "_mcp_run_sessions", None)
        if sessions is not None:
            try:
                await sessions.aclose()
            except Exception:
                logger.warning(
                    "Private MCP run-session cleanup failed for run %s",
                    self._run_id,
                )
        try:
            await asyncio.to_thread(
                _remove_private_skill_tree,
                self.skill_root,
            )
        except Exception:
            self._closing = False
            raise
        self._closed = True
        self._closing = False
