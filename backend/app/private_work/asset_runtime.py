from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.agent_runtime_identity import (
    AgentRuntimeIdentity as _AgentRuntimeIdentity,
)
from app.private_work.agent_runtime_identity import (
    agent_runtime_identities as _agent_runtime_identities,
)
from app.private_work.agent_runtime_identity import (
    main_pool_prefix as _main_pool_prefix,
)
from app.private_work.asset_runtime_contracts import (
    PrivateMcpManifest,
)
from app.private_work.authorization import PrivateRunAuthorizationBoundary
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
from app.private_work.mcp_runtime_contracts import (
    DiscoveredMcpTool as _DiscoveredMcpTool,  # noqa: F401 - compatibility alias
)
from app.private_work.mcp_runtime_contracts import (
    mcp_tool_inventory_description as _mcp_tool_inventory_description,  # noqa: F401 - compatibility alias
)
from app.private_work.mcp_runtime_contracts import (
    mcp_tool_inventory_payload as _mcp_tool_inventory_payload,  # noqa: F401 - compatibility alias
)
from app.private_work.mcp_runtime_contracts import (
    safe_mcp_definition_copy as _safe_copy,
)
from app.private_work.mcp_runtime_contracts import (
    validate_project_mcp_material_policy as _validate_project_mcp_material_policy,  # noqa: F401 - compatibility alias
)
from app.private_work.mcp_runtime_contracts import (
    validate_project_mcp_snapshot_policy as _validate_project_mcp_snapshot_policy,  # noqa: F401 - compatibility alias
)
from app.private_work.private_agent_manifest import (
    build_private_agent_manifest as _private_agent_manifest,
)
from app.private_work.private_agent_runtime import (
    PrivateAgentRuntime,
    _validated_mcp_runtime_timeout,
)
from app.private_work.private_skill_runtime import (
    PrivateRuntimeCleanupError,
)
from app.private_work.private_skill_runtime import (
    create_private_skill_root as _create_private_skill_root,
)
from app.private_work.private_skill_runtime import (
    remove_private_skill_tree as _remove_private_skill_tree,
)
from app.private_work.private_skill_runtime import (
    write_skill_tree as _write_skill_tree,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import AdmittedPrivateRun
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.capabilities import Capability
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.run_snapshot_codec import (
    RunAssetSnapshotInvalid,
    decode_run_asset_snapshot,
)
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.sandbox.sandbox import AuthorizationRevoked

logger = logging.getLogger(__name__)

_DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 15
_DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS = 60
_BUILTIN_MAIN_AGENT_SOURCE_KEY = "builtin:agent:project-assistant"


class PrivateAssetRuntime:
    """Build run-scoped Agent/Skill/MCP state from persisted exact IDs only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        run_session_reuse: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        self._tool_call_timeout_seconds = _validated_mcp_runtime_timeout(tool_call_timeout_seconds)
        self._run_session_reuse = bool(run_session_reuse)

    async def materialize(
        self,
        context: PrivateWorkContext,
        admitted: AdmittedPrivateRun,
        *,
        authorization_boundary: object | None = None,
        delegate_model_names: Mapping[uuid.UUID, str] | None = None,
        runtime_kind: Literal["chat", "skill_builder"] = "chat",
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
        before_snapshot_read = getattr(
            authorization_boundary,
            "before_checkpoint_read",
            None,
        )
        if callable(before_snapshot_read):
            await before_snapshot_read()
        skill_snapshots: tuple[ResolvedSkillSnapshot, ...]
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        lead_skill_snapshots: tuple[ResolvedSkillSnapshot, ...]
        lead_mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        delegated_agents: tuple[ResolvedAgentSnapshot, ...]
        agent_identities: tuple[_AgentRuntimeIdentity, ...]
        try:
            async with self._session_factory() as session, session.begin():
                required_capabilities = (
                    (
                        Capability.SHARED_ASSETS_READ,
                        Capability.SHARED_ASSETS_EDIT,
                    )
                    if runtime_kind == "skill_builder"
                    else (
                        Capability.PRIVATE_WORK_CREATE,
                        Capability.SHARED_ASSETS_EXECUTE,
                    )
                )
                await self._revalidator.require(
                    session,
                    context,
                    *required_capabilities,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=admitted.run.run_id,
                    lock=True,
                )
                execution_job_id = getattr(
                    authorization_boundary,
                    "execution_job_id",
                    None,
                )
                executable_status = run is not None and (run.status == "pending" or (run.status == "running" and run.job_id == admitted.job.job_id and execution_job_id == admitted.job.job_id))
                if run is None or run.thread_id != admitted.thread_id or not executable_status:
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
                skill_credentials = await self._snapshots.list_skill_credentials_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
                    raise RunSnapshotAssetStale
                if tuple(asset.dependency_order for asset in assets) != tuple(range(len(assets))):
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
                    try:
                        snapshot = decode_run_asset_snapshot(asset.snapshot_json)
                    except RunAssetSnapshotInvalid:
                        raise RunSnapshotAssetStale from None
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

                agent_count = 0
                while agent_count < len(resolved) and type(resolved[agent_count]) is ResolvedAgentSnapshot:
                    agent_count += 1
                skill_end = agent_count
                while skill_end < len(resolved) and type(resolved[skill_end]) is ResolvedSkillSnapshot:
                    skill_end += 1
                if any(type(item) is not ResolvedMcpSnapshot for item in resolved[skill_end:]):
                    raise RunSnapshotAssetStale
                agents = tuple(resolved[:agent_count])
                if not agents or any(type(item) is not ResolvedAgentSnapshot for item in agents):
                    raise RunSnapshotAssetStale
                agent = agents[0]
                delegated_agents = agents[1:]
                skill_snapshots = tuple(resolved[agent_count:skill_end])
                mcp_snapshots = tuple(resolved[skill_end:])
                agent_identities = _agent_runtime_identities(agents)
                is_builtin_main = agent.scope is AssetScope.SYSTEM and agent_identities[0].source_key == _BUILTIN_MAIN_AGENT_SOURCE_KEY
                if any(identity.source_key == _BUILTIN_MAIN_AGENT_SOURCE_KEY for identity in agent_identities[1:]):
                    raise RunSnapshotAssetStale
                skill_by_version = {item.version_id: item for item in skill_snapshots}
                mcp_by_version = {item.version_id: item for item in mcp_snapshots}
                if len(skill_by_version) != len(skill_snapshots) or len(mcp_by_version) != len(mcp_snapshots):
                    raise RunSnapshotAssetStale
                if is_builtin_main:
                    if agent.skill_version_ids or agent.payload.mcp_version_ids:
                        raise RunSnapshotAssetStale
                    lead_skill_snapshots = _main_pool_prefix(  # type: ignore[assignment]
                        skill_snapshots
                    )
                    lead_mcp_snapshots = _main_pool_prefix(  # type: ignore[assignment]
                        mcp_snapshots
                    )
                else:
                    if delegated_agents:
                        raise RunSnapshotAssetStale
                    try:
                        lead_skill_snapshots = tuple(skill_by_version[version_id] for version_id in agent.skill_version_ids)
                        lead_mcp_snapshots = tuple(mcp_by_version[version_id] for version_id in agent.payload.mcp_version_ids)
                    except KeyError:
                        raise RunSnapshotAssetStale from None

                required_skill_versions = {item.version_id for item in lead_skill_snapshots}
                required_mcp_versions = {item.version_id for item in lead_mcp_snapshots}
                for delegated in delegated_agents:
                    required_skill_versions.update(delegated.skill_version_ids)
                    required_mcp_versions.update(delegated.payload.mcp_version_ids)
                if required_skill_versions != set(skill_by_version) or required_mcp_versions != set(mcp_by_version):
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
                persisted_skill_credentials = tuple(
                    sorted(
                        skill_credentials,
                        key=lambda item: (
                            item.skill_version_id.int,
                            item.secret_name,
                            item.skill_credential_binding_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                await self._snapshots.lock_admitted_skill_credentials_in_session(
                    session,
                    context,
                    persisted_skill_credentials,
                    declared_targets=frozenset((snapshot.version_id, requirement.name) for snapshot in skill_snapshots for requirement in snapshot.secret_requirements),
                    required_targets=frozenset((snapshot.version_id, requirement.name) for snapshot in skill_snapshots for requirement in snapshot.secret_requirements if not requirement.optional),
                )
        except (RunSnapshotAssetStale, AssetResolutionUnavailable, AssetValidationFailed, AssetForbidden):
            raise PrivateWorkAssetStale(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        if delegated_agents and delegate_model_names is None:
            raise PrivateWorkAssetStale(context.request_id)
        root = _create_private_skill_root(admitted.run.run_id, context.request_id)
        runtime: PrivateAgentRuntime | None = None
        try:
            root.chmod(0o700)
            skill_manifests, skills = await asyncio.to_thread(_write_skill_tree, root, skill_snapshots)
            skill_manifest_by_version = {item.version_id: item for item in skill_manifests}
            skill_by_version = {
                snapshot.version_id: skill
                for snapshot, skill in zip(
                    skill_snapshots,
                    skills,
                    strict=True,
                )
            }
            mcp_manifests = tuple(
                PrivateMcpManifest(
                    asset_id=snapshot.asset_id,
                    version_id=snapshot.version_id,
                    definition=_safe_copy(snapshot.definition),  # type: ignore[arg-type]
                )
                for snapshot in mcp_snapshots
            )
            mcp_manifest_by_version = {item.version_id: item for item in mcp_manifests}
            lead_skill_manifests = tuple(skill_manifest_by_version[item.version_id] for item in lead_skill_snapshots)
            lead_skills = tuple(skill_by_version[item.version_id] for item in lead_skill_snapshots)
            lead_mcp_manifests = tuple(mcp_manifest_by_version[item.version_id] for item in lead_mcp_snapshots)
            safe_manifest = _private_agent_manifest(
                agent,
                skills=lead_skill_manifests,
                mcps=lead_mcp_manifests,
            )
            delegate_manifests = tuple(
                _private_agent_manifest(
                    delegated,
                    runtime_key=identity.runtime_key,
                    skills=tuple(skill_manifest_by_version[version_id] for version_id in delegated.skill_version_ids),
                    mcps=tuple(mcp_manifest_by_version[version_id] for version_id in delegated.payload.mcp_version_ids),
                )
                for delegated, identity in zip(
                    delegated_agents,
                    agent_identities[1:],
                    strict=True,
                )
            )
            runtime = PrivateAgentRuntime(
                context=context,
                run_id=admitted.run.run_id,
                resolver=self._resolver,
                session_factory=self._session_factory,
                safe_manifest=safe_manifest,
                skill_root=root,
                skills=lead_skills,
                mcp_snapshots=mcp_snapshots,
                authorization_boundary=authorization_boundary,
                endpoint_policy=self._endpoint_policy,
                http_client_factory=self._http_client_factory,
                discovery_timeout_seconds=self._discovery_timeout_seconds,
                tool_call_timeout_seconds=self._tool_call_timeout_seconds,
                delegate_manifests=delegate_manifests,
                all_skill_manifests=skill_manifests,
                all_skills=skills,
                delegate_model_names=delegate_model_names,
                run_session_reuse=self._run_session_reuse,
            )
            await runtime.discover_mcp_tools()
            return runtime
        except Exception as error:
            try:
                if runtime is None:
                    await asyncio.to_thread(_remove_private_skill_tree, root)
                else:
                    # Discovery may already have populated the Run session
                    # cache. Close it and clear its derived-secret closure
                    # before propagating a failed materialization.
                    await runtime.aclose()
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
