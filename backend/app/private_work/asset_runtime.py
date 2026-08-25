from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import and_, exists, select
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
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import AdmittedPrivateRun
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.run_skill_tree_materializer import (
    LegacyInlineRunSkillPlan,
    LegacyInlineRunSkillSourceAdapter,
    MaterializationAttemptIdentity,
    MaterializationAuthorityReadback,
    PinnedSkillVersionPlan,
    PinnedSkillVersionSourceAdapter,
    RunSkillMaterializationAuthority,
    RunSkillTreeMaterializationPlan,
    RunSkillTreeMaterializationStale,
    RunSkillTreeMaterializer,
)
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
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
    ResolvedRunAssetFact,
    RunSkillVersionManifest,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.run_snapshot_codec import (
    RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
    RunAssetSnapshotInvalid,
    decode_run_asset_snapshot,
    decode_run_skill_version_manifest,
)
from app.shared_assets.skill_secret_policy import (
    parse_skill_secret_declarations,
)
from deerflow.config import get_app_config, get_paths
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import McpEndpointPolicy
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunSkillVersionRefRow,
)
from deerflow.persistence.shared_assets.skill_model import (
    SkillRow,
    SkillVersionRow,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.sandbox.sandbox_provider import NotAcquired

logger = logging.getLogger(__name__)

_DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 15
_DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS = 60
_BUILTIN_MAIN_AGENT_SOURCE_KEY = "builtin:agent:project-assistant"


@dataclass(frozen=True, slots=True)
class _RuntimeAssetFact:
    asset_kind: str
    dependency_order: int
    asset_scope: str
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int
    snapshot_schema_version: int

    def as_run_asset_fact(self) -> ResolvedRunAssetFact:
        return ResolvedRunAssetFact(
            kind=AssetKind(self.asset_kind),
            dependency_order=self.dependency_order,
            scope=AssetScope(self.asset_scope),
            asset_id=self.asset_id,
            version_id=self.version_id,
            checksum=self.payload_checksum,
            catalog_generation=self.catalog_generation,
        )


class _AssetRuntimeMaterializationAuthority(RunSkillMaterializationAuthority):
    def __init__(
        self,
        authorization_boundary: object,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        revalidator: PrivateWorkRevalidator,
        context: PrivateWorkContext,
        capabilities: tuple[Capability, ...],
        fingerprint_reader: Callable[
            [AsyncSession, ProjectContext],
            Awaitable[str],
        ],
    ) -> None:
        execution_suffix = getattr(
            authorization_boundary,
            "lock_and_assert_materialization_active_in_session",
            None,
        )
        governance_prefix = getattr(revalidator, "require", None)
        if (
            not callable(session_factory)
            or not callable(governance_prefix)
            or type(context) is not PrivateWorkContext
            or type(capabilities) is not tuple
            or not capabilities
            or any(type(value) is not Capability for value in capabilities)
            or not callable(execution_suffix)
            or not callable(fingerprint_reader)
        ):
            raise RunSnapshotAssetStale
        self._session_factory = session_factory
        self._governance_prefix = governance_prefix
        self._context = context
        self._capabilities = capabilities
        self._execution_suffix = execution_suffix
        self._fingerprint_reader = fingerprint_reader

    async def read_materialization_authority(
        self,
        *,
        boundary: Literal["initial", "version", "final"],
        dependency_order: int | None,
    ) -> MaterializationAuthorityReadback:
        del boundary, dependency_order
        async with self._session_factory() as session, session.begin():
            locked_context = await self._governance_prefix(
                session,
                self._context,
                *self._capabilities,
                lock_mode="share",
            )
            attempt_identity = await self._execution_suffix(
                session,
                locked_context,
            )
            observed_fingerprint = await self._fingerprint_reader(
                session,
                locked_context,
            )
        if type(attempt_identity) is not MaterializationAttemptIdentity or type(observed_fingerprint) is not str:
            raise RunSnapshotAssetStale
        return MaterializationAuthorityReadback(
            attempt_identity=attempt_identity,
            plan_fingerprint=observed_fingerprint,
        )


def _asset_plan_fingerprint(
    facts: tuple[_RuntimeAssetFact, ...],
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    thread_id: str,
    run_id: str,
    runtime_kind: Literal["chat", "skill_builder"],
    mcp_secrets: tuple[object, ...],
    skill_secrets: tuple[object, ...],
) -> str:
    value = {
        "project_id": str(project_id),
        "owner_user_id": owner_user_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "runtime_kind": runtime_kind,
        "assets": [
            {
                "asset_kind": fact.asset_kind,
                "dependency_order": fact.dependency_order,
                "asset_scope": fact.asset_scope,
                "asset_id": str(fact.asset_id),
                "version_id": str(fact.version_id),
                "payload_checksum": fact.payload_checksum,
                "catalog_generation": fact.catalog_generation,
                "snapshot_schema_version": fact.snapshot_schema_version,
            }
            for fact in facts
        ],
        "mcp_secrets": [
            {
                "mcp_server_id": str(value.mcp_server_id),
                "mcp_server_version_id": str(value.mcp_server_version_id),
                "slot_id": str(value.slot_id),
                "secret_revision": value.secret_revision,
                "secret_generation_id": str(value.secret_generation_id),
                "secret_generation_digest": value.secret_generation_digest,
            }
            for value in mcp_secrets
        ],
        "skill_secrets": [
            {
                "skill_id": str(value.skill_id),
                "skill_version_id": str(value.skill_version_id),
                "secret_name": value.secret_name,
                "secret_revision": value.secret_revision,
                "secret_generation_id": str(value.secret_generation_id),
                "secret_generation_digest": value.secret_generation_digest,
            }
            for value in skill_secrets
        ],
    }
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


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
        skill_tree_materializer: RunSkillTreeMaterializer | None = None,
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
        if skill_tree_materializer is not None and type(skill_tree_materializer) is not RunSkillTreeMaterializer:
            raise TypeError("skill_tree_materializer is invalid")
        self._skill_tree_materializer = skill_tree_materializer

    def _required_skill_tree_materializer(self) -> RunSkillTreeMaterializer:
        materializer = self._skill_tree_materializer
        if materializer is None:
            config = get_app_config()
            materializer = RunSkillTreeMaterializer(
                materialization_root=(get_paths().run_skill_materialization_root()),
                worker_config=config.worker,
                legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(
                    self._session_factory,
                ),
                pinned_source_adapter=PinnedSkillVersionSourceAdapter(
                    self._session_factory,
                ),
            )
            self._skill_tree_materializer = materializer
        return materializer

    @staticmethod
    async def _asset_facts(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ) -> tuple[_RuntimeAssetFact, ...]:
        rows = (
            await session.execute(
                select(
                    RunAssetVersionRow.asset_kind,
                    RunAssetVersionRow.dependency_order,
                    RunAssetVersionRow.asset_scope,
                    RunAssetVersionRow.asset_id,
                    RunAssetVersionRow.version_id,
                    RunAssetVersionRow.payload_checksum,
                    RunAssetVersionRow.catalog_generation,
                    RunAssetVersionRow.snapshot_schema_version,
                )
                .where(
                    RunAssetVersionRow.project_id == context.project_id,
                    RunAssetVersionRow.owner_user_id == str(context.user_id),
                    RunAssetVersionRow.thread_id == thread_id,
                    RunAssetVersionRow.run_id == run_id,
                )
                .order_by(RunAssetVersionRow.dependency_order)
            )
        ).all()
        try:
            return tuple(
                _RuntimeAssetFact(
                    asset_kind=asset_kind,
                    dependency_order=dependency_order,
                    asset_scope=asset_scope,
                    asset_id=uuid.UUID(str(asset_id)),
                    version_id=uuid.UUID(str(version_id)),
                    payload_checksum=payload_checksum,
                    catalog_generation=catalog_generation,
                    snapshot_schema_version=snapshot_schema_version,
                )
                for (
                    asset_kind,
                    dependency_order,
                    asset_scope,
                    asset_id,
                    version_id,
                    payload_checksum,
                    catalog_generation,
                    snapshot_schema_version,
                ) in rows
            )
        except (TypeError, ValueError):
            raise RunSnapshotAssetStale from None

    @staticmethod
    async def _small_snapshot(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        fact: _RuntimeAssetFact,
    ) -> ResolvedAgentSnapshot | ResolvedMcpSnapshot:
        if fact.asset_kind not in {AssetKind.AGENT.value, AssetKind.MCP.value} or fact.snapshot_schema_version not in {2, 3}:
            raise RunSnapshotAssetStale
        value = (
            await session.execute(
                select(RunAssetVersionRow.snapshot_json).where(
                    RunAssetVersionRow.project_id == context.project_id,
                    RunAssetVersionRow.owner_user_id == str(context.user_id),
                    RunAssetVersionRow.thread_id == thread_id,
                    RunAssetVersionRow.run_id == run_id,
                    RunAssetVersionRow.asset_kind == fact.asset_kind,
                    RunAssetVersionRow.dependency_order == fact.dependency_order,
                    RunAssetVersionRow.asset_scope == fact.asset_scope,
                    RunAssetVersionRow.asset_id == fact.asset_id,
                    RunAssetVersionRow.version_id == fact.version_id,
                    RunAssetVersionRow.payload_checksum == fact.payload_checksum,
                    RunAssetVersionRow.snapshot_schema_version == fact.snapshot_schema_version,
                )
            )
        ).scalar_one_or_none()
        if not isinstance(value, Mapping):
            raise RunSnapshotAssetStale
        try:
            snapshot = decode_run_asset_snapshot(value)
        except RunAssetSnapshotInvalid:
            raise RunSnapshotAssetStale from None
        if type(snapshot) not in {ResolvedAgentSnapshot, ResolvedMcpSnapshot}:
            raise RunSnapshotAssetStale
        return snapshot

    @staticmethod
    async def _legacy_skill_plan(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        fact: _RuntimeAssetFact,
    ) -> LegacyInlineRunSkillPlan:
        if fact.asset_kind != AssetKind.SKILL.value or fact.snapshot_schema_version not in {2, 3}:
            raise RunSnapshotAssetStale
        asset = RunAssetVersionRow
        ref = RunSkillVersionRefRow
        skill = SkillRow
        version = SkillVersionRow
        row = (
            await session.execute(
                select(
                    skill.scope,
                    skill.project_id,
                    version.files_sealed,
                    version.payload_checksum,
                    version.file_count,
                    version.content_size_bytes,
                    version.secret_requirements,
                )
                .select_from(asset)
                .join(skill, skill.id == asset.asset_id)
                .join(
                    version,
                    and_(
                        version.skill_id == asset.asset_id,
                        version.id == asset.version_id,
                    ),
                )
                .where(
                    asset.project_id == context.project_id,
                    asset.owner_user_id == str(context.user_id),
                    asset.thread_id == thread_id,
                    asset.run_id == run_id,
                    asset.asset_kind == fact.asset_kind,
                    asset.dependency_order == fact.dependency_order,
                    asset.asset_scope == fact.asset_scope,
                    asset.asset_id == fact.asset_id,
                    asset.version_id == fact.version_id,
                    asset.payload_checksum == fact.payload_checksum,
                    asset.catalog_generation == fact.catalog_generation,
                    asset.snapshot_schema_version == fact.snapshot_schema_version,
                    ~exists().where(
                        and_(
                            ref.project_id == asset.project_id,
                            ref.owner_user_id == asset.owner_user_id,
                            ref.thread_id == asset.thread_id,
                            ref.run_id == asset.run_id,
                            ref.asset_kind == asset.asset_kind,
                            ref.dependency_order == asset.dependency_order,
                        )
                    ),
                )
            )
        ).one_or_none()
        if row is None:
            raise RunSnapshotAssetStale
        try:
            scope = AssetScope(fact.asset_scope)
            declarations = parse_skill_secret_declarations(
                row.secret_requirements,
                request_id=context.request_id,
            )
            plan = LegacyInlineRunSkillPlan(
                dependency_order=fact.dependency_order,
                scope=scope,
                asset_id=fact.asset_id,
                version_id=fact.version_id,
                payload_checksum=fact.payload_checksum,
                catalog_generation=fact.catalog_generation,
                snapshot_schema_version=fact.snapshot_schema_version,
                file_count=row.file_count,
                content_size_bytes=row.content_size_bytes,
                secret_requirements=tuple(
                    SkillSecretRequirementSnapshot(
                        name=value.name,
                        target_env=value.target_env,
                        optional=value.optional,
                    )
                    for value in declarations
                ),
            )
        except (AssetValidationFailed, TypeError, ValueError):
            raise RunSnapshotAssetStale from None
        expected_project = context.project_id if scope is AssetScope.PROJECT else None
        if row.scope != fact.asset_scope or row.project_id != expected_project or row.files_sealed is not True or row.payload_checksum != fact.payload_checksum:
            raise RunSnapshotAssetStale
        return plan

    @staticmethod
    async def _pinned_skill_plan(
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        fact: _RuntimeAssetFact,
    ) -> PinnedSkillVersionPlan:
        if fact.asset_kind != AssetKind.SKILL.value or fact.snapshot_schema_version != RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION:
            # v2/v3 is deliberately fail-closed here; it is never loaded as a
            # closure-wide byte-bearing JSONB entity.
            raise RunSnapshotAssetStale
        asset = RunAssetVersionRow
        ref = RunSkillVersionRefRow
        version = SkillVersionRow
        skill = SkillRow
        row = (
            await session.execute(
                select(
                    asset.snapshot_json,
                    ref.asset_scope,
                    ref.skill_project_id,
                    ref.skill_id,
                    ref.skill_version_id,
                    ref.payload_checksum,
                    ref.file_count,
                    ref.content_size_bytes,
                    ref.snapshot_schema_version,
                    skill.scope,
                    skill.project_id,
                    version.files_sealed,
                    version.payload_checksum.label("version_checksum"),
                    version.file_count.label("version_file_count"),
                    version.content_size_bytes.label("version_content_size"),
                    version.secret_requirements,
                )
                .select_from(asset)
                .join(
                    ref,
                    and_(
                        ref.project_id == asset.project_id,
                        ref.owner_user_id == asset.owner_user_id,
                        ref.thread_id == asset.thread_id,
                        ref.run_id == asset.run_id,
                        ref.asset_kind == asset.asset_kind,
                        ref.dependency_order == asset.dependency_order,
                        ref.skill_id == asset.asset_id,
                        ref.skill_version_id == asset.version_id,
                        ref.payload_checksum == asset.payload_checksum,
                        ref.snapshot_schema_version == asset.snapshot_schema_version,
                    ),
                )
                .join(skill, skill.id == asset.asset_id)
                .join(
                    version,
                    and_(
                        version.skill_id == asset.asset_id,
                        version.id == asset.version_id,
                    ),
                )
                .where(
                    asset.project_id == context.project_id,
                    asset.owner_user_id == str(context.user_id),
                    asset.thread_id == thread_id,
                    asset.run_id == run_id,
                    asset.asset_kind == fact.asset_kind,
                    asset.dependency_order == fact.dependency_order,
                    asset.asset_scope == fact.asset_scope,
                    asset.asset_id == fact.asset_id,
                    asset.version_id == fact.version_id,
                    asset.payload_checksum == fact.payload_checksum,
                    asset.catalog_generation == fact.catalog_generation,
                    asset.snapshot_schema_version == fact.snapshot_schema_version,
                )
            )
        ).one_or_none()
        if row is None or not isinstance(row.snapshot_json, Mapping):
            raise RunSnapshotAssetStale
        try:
            manifest = decode_run_skill_version_manifest(
                row.snapshot_json,
            )
            declarations = parse_skill_secret_declarations(
                row.secret_requirements,
                request_id=context.request_id,
            )
            scope = AssetScope(fact.asset_scope)
        except (RunAssetSnapshotInvalid, ValueError):
            raise RunSnapshotAssetStale from None
        expected_project = context.project_id if scope is AssetScope.PROJECT else None
        if (
            type(manifest) is not RunSkillVersionManifest
            or manifest.scope is not scope
            or manifest.asset_id != fact.asset_id
            or manifest.version_id != fact.version_id
            or manifest.checksum != fact.payload_checksum
            or manifest.catalog_generation != fact.catalog_generation
            or row.asset_scope != fact.asset_scope
            or row.skill_project_id != expected_project
            or row.skill_id != fact.asset_id
            or row.skill_version_id != fact.version_id
            or row.payload_checksum != fact.payload_checksum
            or row.file_count != manifest.file_count
            or row.content_size_bytes != manifest.content_size_bytes
            or row.snapshot_schema_version != RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION
            or row.scope != fact.asset_scope
            or row.project_id != expected_project
            or row.files_sealed is not True
            or row.version_checksum != fact.payload_checksum
            or row.version_file_count != manifest.file_count
            or row.version_content_size != manifest.content_size_bytes
        ):
            raise RunSnapshotAssetStale
        try:
            return PinnedSkillVersionPlan(
                dependency_order=fact.dependency_order,
                scope=scope,
                asset_id=fact.asset_id,
                version_id=fact.version_id,
                payload_checksum=fact.payload_checksum,
                catalog_generation=fact.catalog_generation,
                dependency_version_ids=manifest.dependency_version_ids,
                file_count=manifest.file_count,
                content_size_bytes=manifest.content_size_bytes,
                secret_requirements=tuple(
                    SkillSecretRequirementSnapshot(
                        name=value.name,
                        target_env=value.target_env,
                        optional=value.optional,
                    )
                    for value in declarations
                ),
            )
        except (TypeError, ValueError):
            raise RunSnapshotAssetStale from None

    async def _read_plan_fingerprint_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        run_id: str,
        runtime_kind: Literal["chat", "skill_builder"],
    ) -> str:
        facts = await self._asset_facts(
            session,
            context,
            thread_id,
            run_id,
        )
        mcp_secrets = tuple(
            sorted(
                await self._snapshots.list_mcp_secrets_in_session(
                    session,
                    context,
                    run_id,
                ),
                key=lambda item: (
                    item.mcp_server_version_id.int,
                    item.slot_id.int,
                    item.secret_generation_id.int,
                ),
            )
        )
        skill_secrets = tuple(
            sorted(
                await self._snapshots.list_skill_secrets_in_session(
                    session,
                    context,
                    run_id,
                ),
                key=lambda item: (
                    item.skill_version_id.int,
                    item.secret_name,
                    item.secret_generation_id.int,
                ),
            )
        )
        return _asset_plan_fingerprint(
            facts,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            thread_id=thread_id,
            run_id=run_id,
            runtime_kind=runtime_kind,
            mcp_secrets=mcp_secrets,
            skill_secrets=skill_secrets,
        )

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
        execution_boundary_provided = authorization_boundary is not None
        authorization_boundary = authorization_boundary or PrivateRunAuthorizationBoundary(
            self._session_factory,
            project_id=admitted.run.project_id,
            owner_user_id=admitted.run.owner_user_id,
            run_id=admitted.run.run_id,
        )
        skill_plans: tuple[
            PinnedSkillVersionPlan | LegacyInlineRunSkillPlan,
            ...,
        ]
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        lead_skill_plans: tuple[
            PinnedSkillVersionPlan | LegacyInlineRunSkillPlan,
            ...,
        ]
        lead_mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        delegated_agents: tuple[ResolvedAgentSnapshot, ...]
        agent_identities: tuple[_AgentRuntimeIdentity, ...]
        asset_facts: tuple[_RuntimeAssetFact, ...]
        persisted_mcp_secrets: tuple[object, ...]
        persisted_skill_secrets: tuple[object, ...]
        initial_attempt_identity: MaterializationAttemptIdentity | None = None
        plan_fingerprint: str
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
        try:
            async with self._session_factory() as session, session.begin():
                locked_context = await self._revalidator.require(
                    session,
                    context,
                    *required_capabilities,
                    lock_mode="share",
                )
                execution_suffix = getattr(
                    authorization_boundary,
                    "lock_and_assert_materialization_active_in_session",
                    None,
                )
                if execution_boundary_provided and not callable(execution_suffix):
                    raise RunSnapshotAssetStale
                if callable(execution_suffix):
                    initial_attempt_identity = await execution_suffix(
                        session,
                        locked_context,
                    )
                    if type(initial_attempt_identity) is not MaterializationAttemptIdentity or str(initial_attempt_identity.job_id) != str(admitted.job.job_id):
                        raise RunSnapshotAssetStale
                run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=admitted.run.run_id,
                    lock=False,
                )
                execution_job_id = getattr(
                    authorization_boundary,
                    "execution_job_id",
                    None,
                )
                executable_status = run is not None and (run.status == "pending" or (run.status == "running" and run.job_id == admitted.job.job_id and execution_job_id == admitted.job.job_id))
                if run is None or run.thread_id != admitted.thread_id or not executable_status:
                    raise PrivateWorkNotFound(context.request_id)
                asset_facts = await self._asset_facts(
                    session,
                    context,
                    run.thread_id,
                    run.run_id,
                )
                mcp_secrets = await self._snapshots.list_mcp_secrets_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                skill_secrets = await self._snapshots.list_skill_secrets_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                if not asset_facts or asset_facts[0].asset_kind != AssetKind.AGENT.value:
                    raise RunSnapshotAssetStale
                if tuple(asset.dependency_order for asset in asset_facts) != tuple(range(len(asset_facts))):
                    raise RunSnapshotAssetStale
                persisted_generation = asset_facts[0].catalog_generation
                if any(asset.catalog_generation != persisted_generation for asset in asset_facts):
                    raise RunSnapshotAssetStale
                agents_list: list[ResolvedAgentSnapshot] = []
                skill_plan_list: list[PinnedSkillVersionPlan | LegacyInlineRunSkillPlan] = []
                mcp_list: list[ResolvedMcpSnapshot] = []
                last_rank = 0
                kind_rank = {
                    AssetKind.AGENT.value: 0,
                    AssetKind.SKILL.value: 1,
                    AssetKind.MCP.value: 2,
                }
                for fact in asset_facts:
                    rank = kind_rank.get(fact.asset_kind)
                    if rank is None or rank < last_rank:
                        raise RunSnapshotAssetStale
                    last_rank = rank
                    if fact.asset_kind == AssetKind.SKILL.value:
                        if fact.snapshot_schema_version in {2, 3}:
                            skill_plan = await self._legacy_skill_plan(
                                session,
                                context,
                                run.thread_id,
                                run.run_id,
                                fact,
                            )
                        elif fact.snapshot_schema_version == RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION:
                            skill_plan = await self._pinned_skill_plan(
                                session,
                                context,
                                run.thread_id,
                                run.run_id,
                                fact,
                            )
                        else:
                            raise RunSnapshotAssetStale
                        skill_plan_list.append(skill_plan)
                        continue
                    snapshot = await self._small_snapshot(
                        session,
                        context,
                        run.thread_id,
                        run.run_id,
                        fact,
                    )
                    if (
                        snapshot.kind.value != fact.asset_kind
                        or snapshot.scope.value != fact.asset_scope
                        or snapshot.asset_id != fact.asset_id
                        or snapshot.version_id != fact.version_id
                        or snapshot.checksum != fact.payload_checksum
                        or snapshot.catalog_generation != persisted_generation
                    ):
                        raise RunSnapshotAssetStale
                    if type(snapshot) is ResolvedAgentSnapshot:
                        agents_list.append(snapshot)
                    elif type(snapshot) is ResolvedMcpSnapshot:
                        mcp_list.append(snapshot)
                    else:
                        raise RunSnapshotAssetStale
                agents = tuple(agents_list)
                skill_plans = tuple(skill_plan_list)
                mcp_snapshots = tuple(mcp_list)
                if not agents:
                    raise RunSnapshotAssetStale
                agent = agents[0]
                delegated_agents = agents[1:]
                agent_identities = _agent_runtime_identities(agents)
                is_builtin_main = agent.scope is AssetScope.SYSTEM and agent_identities[0].source_key == _BUILTIN_MAIN_AGENT_SOURCE_KEY
                if any(identity.source_key == _BUILTIN_MAIN_AGENT_SOURCE_KEY for identity in agent_identities[1:]):
                    raise RunSnapshotAssetStale
                skill_by_version = {item.version_id: item for item in skill_plans}
                mcp_by_version = {item.version_id: item for item in mcp_snapshots}
                if len(skill_by_version) != len(skill_plans) or len(mcp_by_version) != len(mcp_snapshots):
                    raise RunSnapshotAssetStale
                if is_builtin_main:
                    if agent.skill_version_ids or agent.payload.mcp_version_ids:
                        raise RunSnapshotAssetStale
                    lead_skill_plans = _main_pool_prefix(  # type: ignore[assignment]
                        skill_plans
                    )
                    lead_mcp_snapshots = _main_pool_prefix(  # type: ignore[assignment]
                        mcp_snapshots
                    )
                else:
                    if delegated_agents:
                        raise RunSnapshotAssetStale
                    try:
                        lead_skill_plans = tuple(skill_by_version[version_id] for version_id in agent.skill_version_ids)
                        lead_mcp_snapshots = tuple(mcp_by_version[version_id] for version_id in agent.payload.mcp_version_ids)
                    except KeyError:
                        raise RunSnapshotAssetStale from None

                required_skill_versions = {item.version_id for item in lead_skill_plans}
                required_mcp_versions = {item.version_id for item in lead_mcp_snapshots}
                for delegated in delegated_agents:
                    required_skill_versions.update(delegated.skill_version_ids)
                    required_mcp_versions.update(delegated.payload.mcp_version_ids)
                if required_skill_versions != set(skill_by_version) or required_mcp_versions != set(mcp_by_version):
                    raise RunSnapshotAssetStale
                current_mcp_secrets = await self._snapshots.current_mcp_secrets_in_session(
                    session,
                    context,
                    tuple(asset.as_run_asset_fact() for asset in asset_facts if asset.asset_kind == AssetKind.MCP.value),
                )
                persisted_mcp_secrets = tuple(
                    sorted(
                        mcp_secrets,
                        key=lambda item: (
                            item.mcp_server_version_id.int,
                            item.slot_id.int,
                            item.secret_generation_id.int,
                        ),
                    )
                )
                if current_mcp_secrets != persisted_mcp_secrets:
                    raise RunSnapshotAssetStale
                persisted_skill_secrets = tuple(
                    sorted(
                        skill_secrets,
                        key=lambda item: (
                            item.skill_version_id.int,
                            item.secret_name,
                            item.secret_generation_id.int,
                        ),
                    )
                )
                await self._snapshots.lock_admitted_skill_secrets_in_session(
                    session,
                    context,
                    persisted_skill_secrets,
                    declared_targets=frozenset((plan.version_id, requirement.name) for plan in skill_plans for requirement in plan.secret_requirements),
                    required_targets=frozenset((plan.version_id, requirement.name) for plan in skill_plans for requirement in plan.secret_requirements if not requirement.optional),
                )
                plan_fingerprint = _asset_plan_fingerprint(
                    asset_facts,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=admitted.thread_id,
                    run_id=admitted.run.run_id,
                    runtime_kind=runtime_kind,
                    mcp_secrets=persisted_mcp_secrets,
                    skill_secrets=persisted_skill_secrets,
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
        pending_tree = None
        runtime: PrivateAgentRuntime | None = None
        not_acquired: NotAcquired | None = None
        try:
            if skill_plans:
                if initial_attempt_identity is None:
                    raise RunSnapshotAssetStale
                try:
                    materialization_project_id = uuid.UUID(
                        str(context.project_id),
                    )
                except (TypeError, ValueError):
                    raise RunSnapshotAssetStale from None
                materialization_plan = RunSkillTreeMaterializationPlan(
                    project_id=materialization_project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=admitted.thread_id,
                    run_id=admitted.run.run_id,
                    runtime_kind=runtime_kind,
                    attempt_identity=initial_attempt_identity,
                    plan_fingerprint=plan_fingerprint,
                    skill_versions=skill_plans,
                )

                async def read_plan_fingerprint(
                    session: AsyncSession,
                    locked_context: ProjectContext,
                ) -> str:
                    if locked_context.user_id != context.user_id or locked_context.project_id != context.project_id or locked_context.membership_id != context.membership_id or locked_context.membership_version != context.membership_version:
                        raise RunSnapshotAssetStale
                    return await self._read_plan_fingerprint_in_session(
                        session,
                        context,
                        thread_id=admitted.thread_id,
                        run_id=admitted.run.run_id,
                        runtime_kind=runtime_kind,
                    )

                pending_tree = await self._required_skill_tree_materializer().materialize(
                    plan=materialization_plan,
                    authority=_AssetRuntimeMaterializationAuthority(
                        authorization_boundary,
                        session_factory=self._session_factory,
                        revalidator=self._revalidator,
                        context=context,
                        capabilities=required_capabilities,
                        fingerprint_reader=read_plan_fingerprint,
                    ),
                )
                skill_manifests = pending_tree.manifests
                skills = pending_tree.skills
                not_acquired = NotAcquired(
                    owner_id=pending_tree.source.owner_id,
                )
            else:
                skill_manifests = ()
                skills = ()
            skill_manifest_by_version = {item.version_id: item for item in skill_manifests}
            skill_by_version = {
                plan.version_id: skill
                for plan, skill in zip(
                    skill_plans,
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
            lead_skill_manifests = tuple(skill_manifest_by_version[item.version_id] for item in lead_skill_plans)
            lead_skills = tuple(skill_by_version[item.version_id] for item in lead_skill_plans)
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
            if pending_tree is not None:
                pending_tree.transfer_to(runtime)
                pending_tree = None
            await runtime.discover_mcp_tools()
            return runtime
        except BaseException as error:
            try:
                if pending_tree is not None:
                    await pending_tree.aclose()
                elif runtime is not None:
                    # Discovery may already have populated the Run session
                    # cache. Close it and clear its derived-secret closure
                    # before propagating a failed materialization.
                    await runtime.aclose(not_acquired)
            except Exception:
                logger.warning("Private runtime cleanup failed after materialization")
            if isinstance(error, asyncio.CancelledError):
                raise
            if not isinstance(error, Exception):
                raise
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
                    RunSkillTreeMaterializationStale,
                ),
            ):
                raise PrivateWorkAssetStale(context.request_id) from None
            raise PrivateWorkUnavailable(context.request_id) from None
