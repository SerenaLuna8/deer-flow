from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkUnavailable,
)
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunCreate,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.shared_assets.credential_closure import (
    LockedMcpCredentialClosure,
    McpCredentialClosureInvalid,
    McpCredentialClosureTarget,
    lock_mcp_credential_closures,
)
from app.shared_assets.model_refs import ExactModelRefResolver, ModelRefResolver
from app.shared_assets.models import AssetKind, AssetScope, ResolvedAgentSnapshot
from app.shared_assets.skill_credential_closure import (
    LockedSkillCredentialClosure,
    SkillCredentialClosureInvalid,
    SkillCredentialClosureTarget,
    lock_skill_credential_closures,
)
from app.system_runtime_settings.models import LockedAgentRuntimePolicy
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_settings.errors import (
    SystemModelConflict,
    SystemModelInvalid,
    SystemModelNotFound,
    SystemModelStorageUnavailable,
)
from deerflow.mcp_definition_policy import (
    McpDefinitionPolicyError,
    McpEndpointPolicy,
    validate_project_mcp_definition,
)
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    RunSkillCredentialSnapshotRow,
)
from deerflow.persistence.shared_assets.agent_model import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
)
from deerflow.persistence.shared_assets.mcp_model import McpServerRow, McpServerVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow

_FORBIDDEN_PERSISTED_KEY_PARTS = (
    "secret",
    "envelope",
    "key_id",
    "nonce",
    "ciphertext",
    "storage_locator",
)


@dataclass(frozen=True, slots=True)
class RunAssetSnapshot:
    asset_kind: str
    dependency_order: int
    asset_scope: str
    asset_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: str
    catalog_generation: int


@dataclass(frozen=True, slots=True)
class RunMcpGrantSnapshot:
    mcp_version_id: uuid.UUID
    credential_slot_id: uuid.UUID
    credential_grant_id: uuid.UUID
    credential_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RunSkillCredentialSnapshot:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    secret_name: str
    skill_credential_binding_id: uuid.UUID
    binding_revision: int
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID


class RunSnapshotAssetStale(Exception):
    """Internal stale marker remapped at the request-context boundary."""


class AdmittedRunModelSnapshot(Protocol):
    """Minimum secret-free result required by Run admission."""

    logical_name: str


class RunModelSnapshotAdmissionPort(Protocol):
    """Persist one exact database-backed model closure in the caller transaction."""

    async def admit_model_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        purpose: str,
        model_ref: str,
    ) -> AdmittedRunModelSnapshot: ...


class RunRuntimePolicyAdmissionPort(Protocol):
    """Lock and persist the exact agent runtime policy in the caller transaction."""

    async def lock_agent_runtime_for_admission(
        self,
        session: AsyncSession,
    ) -> LockedAgentRuntimePolicy: ...

    async def admit_run_snapshot(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        thread_id: str,
        run_id: str,
        locked_policy: LockedAgentRuntimePolicy | None = None,
    ) -> object: ...


def _apply_runtime_recursion_limit(
    request: PrivateRunCreate,
    policy: LockedAgentRuntimePolicy,
) -> PrivateRunCreate:
    kwargs = dict(request.kwargs)
    raw_config = kwargs.get("config")
    config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    requested = config.get("recursion_limit", 100)
    if type(requested) is not int or requested <= 0:
        requested = 100
    config["recursion_limit"] = min(
        requested,
        policy.value.max_recursion_limit,
    )
    kwargs["config"] = config
    return replace(request, kwargs=kwargs)


def _reject_secret_bearing_keys(value: object, request_id: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_PERSISTED_KEY_PARTS):
                raise PrivateWorkConflict(request_id)
            _reject_secret_bearing_keys(item, request_id)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_bearing_keys(item, request_id)


class RunSnapshotRepository:
    """Atomically persist a private run and its exact, secret-free asset closure."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        model_ref_resolver: ModelRefResolver | None = None,
        model_catalog: RunModelSnapshotAdmissionPort | None = None,
        runtime_policy: RunRuntimePolicyAdmissionPort | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._model_ref_resolver = model_ref_resolver or ExactModelRefResolver()
        self._model_catalog = model_catalog
        self._runtime_policy = runtime_policy
        self._endpoint_policy = endpoint_policy

    @staticmethod
    def _asset_allowed(
        *,
        asset_scope: str,
        asset_project_id: uuid.UUID | None,
        project_id: uuid.UUID,
    ) -> bool:
        return (asset_scope == AssetScope.SYSTEM.value and asset_project_id is None) or (asset_scope == AssetScope.PROJECT.value and asset_project_id == project_id)

    @staticmethod
    async def _agent(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
        project_id: uuid.UUID,
    ) -> tuple[AgentRow, AgentVersionRow]:
        row = (
            await session.execute(
                select(AgentRow, AgentVersionRow)
                .join(AgentVersionRow, AgentVersionRow.agent_id == AgentRow.id)
                .where(
                    AgentRow.id == snapshot.asset_id,
                    AgentVersionRow.id == snapshot.version_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise RunSnapshotAssetStale
        asset, version = row
        if (
            asset.scope != snapshot.scope.value
            or asset.status != "active"
            or version.workflow_status != "published"
            or version.payload_checksum != snapshot.checksum
            or not RunSnapshotRepository._asset_allowed(
                asset_scope=asset.scope,
                asset_project_id=asset.project_id,
                project_id=project_id,
            )
        ):
            raise RunSnapshotAssetStale
        return asset, version

    @staticmethod
    async def _skills(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
    ) -> list[tuple[SkillRow, SkillVersionRow]]:
        rows: list[tuple[SkillRow, SkillVersionRow]] = []
        for version_id in version_ids:
            row = (
                await session.execute(
                    select(SkillRow, SkillVersionRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.skill_id == SkillRow.id,
                    )
                    .where(SkillVersionRow.id == version_id)
                    .with_for_update(
                        read=True,
                        of=[SkillRow, SkillVersionRow],
                    )
                )
            ).one_or_none()
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or version.workflow_status != "published"
            ):
                raise RunSnapshotAssetStale
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _mcps(
        session: AsyncSession,
        version_ids: tuple[uuid.UUID, ...],
        project_id: uuid.UUID,
        *,
        endpoint_policy: McpEndpointPolicy | None = None,
    ) -> list[tuple[McpServerRow, McpServerVersionRow]]:
        rows: list[tuple[McpServerRow, McpServerVersionRow]] = []
        for version_id in version_ids:
            row = (
                await session.execute(
                    select(McpServerRow, McpServerVersionRow)
                    .join(
                        McpServerVersionRow,
                        McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    )
                    .where(McpServerVersionRow.id == version_id)
                    .with_for_update(
                        read=True,
                        of=[McpServerRow, McpServerVersionRow],
                    )
                )
            ).one_or_none()
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if (
                not RunSnapshotRepository._asset_allowed(
                    asset_scope=asset.scope,
                    asset_project_id=asset.project_id,
                    project_id=project_id,
                )
                or asset.status != "active"
                or version.workflow_status != "published"
                or (asset.scope == AssetScope.PROJECT.value and version.transport == "stdio")
            ):
                raise RunSnapshotAssetStale
            if asset.scope == AssetScope.PROJECT.value:
                try:
                    validate_project_mcp_definition(
                        transport=version.transport,
                        url=version.url,
                        env=version.non_secret_env,
                        headers=version.non_secret_headers,
                        oauth=version.oauth_metadata,
                        credential_slot_schemas=(),
                        endpoint_policy=endpoint_policy,
                    )
                except (AttributeError, McpDefinitionPolicyError, TypeError):
                    raise RunSnapshotAssetStale from None
            rows.append((asset, version))
        return rows

    @staticmethod
    async def _validate_dependency_order(
        session: AsyncSession,
        snapshot: ResolvedAgentSnapshot,
    ) -> None:
        skill_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionSkillRefRow.skill_version_id)
                    .where(AgentVersionSkillRefRow.agent_version_id == snapshot.version_id)
                    .order_by(
                        AgentVersionSkillRefRow.sort_order,
                        AgentVersionSkillRefRow.skill_version_id,
                    )
                )
            ).scalars()
        )
        mcp_ids = tuple(
            (
                await session.execute(
                    select(AgentVersionMcpRefRow.mcp_server_version_id)
                    .where(AgentVersionMcpRefRow.agent_version_id == snapshot.version_id)
                    .order_by(
                        AgentVersionMcpRefRow.sort_order,
                        AgentVersionMcpRefRow.mcp_server_version_id,
                    )
                )
            ).scalars()
        )
        if skill_ids != snapshot.payload.skill_version_ids or mcp_ids != snapshot.payload.mcp_version_ids or snapshot.dependency_version_ids != (*skill_ids, *mcp_ids):
            raise RunSnapshotAssetStale

    @staticmethod
    async def _credential_closures(
        session: AsyncSession,
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
    ) -> dict[uuid.UUID, LockedMcpCredentialClosure]:
        targets = tuple(
            McpCredentialClosureTarget(
                version_id=uuid.UUID(str(version.id)),
                scope=AssetScope(asset.scope),
                project_id=(uuid.UUID(str(asset.project_id)) if asset.scope == AssetScope.PROJECT.value and asset.project_id is not None else None),
            )
            for asset, version in mcps
        )
        try:
            return await lock_mcp_credential_closures(
                session,
                targets,
                load_envelopes=False,
            )
        except McpCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None

    @staticmethod
    def _validate_project_mcp_credential_slots(
        mcps: list[tuple[McpServerRow, McpServerVersionRow]],
        closures: Mapping[uuid.UUID, LockedMcpCredentialClosure],
        *,
        endpoint_policy: McpEndpointPolicy | None,
    ) -> None:
        """Validate the locked credential-slot schemas before admitting work."""

        for asset, version in mcps:
            if asset.scope != AssetScope.PROJECT.value:
                continue
            try:
                closure = closures[uuid.UUID(str(version.id))]
                validate_project_mcp_definition(
                    transport=version.transport,
                    url=version.url,
                    env=version.non_secret_env,
                    headers=version.non_secret_headers,
                    oauth=version.oauth_metadata,
                    credential_slot_schemas=tuple(slot.payload_schema for slot in closure.slots),
                    endpoint_policy=endpoint_policy,
                )
            except (
                AttributeError,
                KeyError,
                McpDefinitionPolicyError,
                TypeError,
                ValueError,
            ):
                raise RunSnapshotAssetStale from None

    async def create_run_with_snapshot(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> PrivateRunRecord:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                return await self.create_run_with_snapshot_in_session(
                    session,
                    context,
                    thread_id,
                    request,
                    resolved_agent,
                )
        except RunSnapshotAssetStale:
            raise PrivateWorkAssetStale(context.request_id) from None
        except PrivateRunConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except IntegrityError:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def create_run_with_snapshot_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        request: PrivateRunCreate,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> PrivateRunRecord:
        """Write a pending run and exact closure in a caller-owned transaction."""

        context = require_issued_private_work_context(context)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise PrivateWorkConflict(context.request_id)
        if type(request) is not PrivateRunCreate or type(resolved_agent) is not ResolvedAgentSnapshot:
            raise PrivateWorkConflict(context.request_id)
        if resolved_agent.kind is not AssetKind.AGENT or resolved_agent.catalog_generation < 0:
            raise PrivateWorkConflict(context.request_id)
        _reject_secret_bearing_keys(request.metadata, context.request_id)
        _reject_secret_bearing_keys(request.kwargs, context.request_id)
        exact_model_name = (
            self._model_ref_resolver.resolve(
                resolved_agent.payload.model_ref,
            )
            if self._model_catalog is None
            else None
        )
        if self._model_catalog is None and exact_model_name is None:
            raise RunSnapshotAssetStale
        safe_request = replace(
            request,
            assistant_id=str(resolved_agent.asset_id),
            status="pending",
            multitask_strategy="reject",
            model_name=exact_model_name,
        )
        (
            skills,
            mcps,
            closures,
            skill_credential_closures,
        ) = await self.validate_agent_closure_in_session(
            session,
            context,
            resolved_agent,
        )
        locked_runtime_policy: LockedAgentRuntimePolicy | None = None
        if self._runtime_policy is not None:
            try:
                locked_runtime_policy = await self._runtime_policy.lock_agent_runtime_for_admission(
                    session,
                )
                safe_request = _apply_runtime_recursion_limit(
                    safe_request,
                    locked_runtime_policy,
                )
            except SystemRuntimePolicyRepositoryInvariant:
                raise RunSnapshotAssetStale from None
        run = await PrivateRunRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=safe_request,
        )
        if self._runtime_policy is not None:
            try:
                await self._runtime_policy.admit_run_snapshot(
                    session,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    locked_policy=locked_runtime_policy,
                )
            except SystemRuntimePolicyRepositoryInvariant:
                raise RunSnapshotAssetStale from None
        if self._model_catalog is not None:
            try:
                model_snapshot = await self._model_catalog.admit_model_snapshot(
                    session,
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    purpose="lead",
                    model_ref=resolved_agent.payload.model_ref,
                )
            except (
                SystemModelConflict,
                SystemModelInvalid,
                SystemModelNotFound,
            ):
                raise RunSnapshotAssetStale from None
            except SystemModelStorageUnavailable:
                raise PrivateWorkUnavailable(context.request_id) from None
            exact_model_name = model_snapshot.logical_name
            if locked_runtime_policy is not None:
                auxiliary_model_refs = (
                    ("title", locked_runtime_policy.value.title.model_name),
                    (
                        "summarization",
                        locked_runtime_policy.value.summarization.model_name,
                    ),
                    ("memory", locked_runtime_policy.value.memory.model_name),
                )
                try:
                    for purpose, model_ref in auxiliary_model_refs:
                        if model_ref is not None:
                            await self._model_catalog.admit_model_snapshot(
                                session,
                                project_id=context.project_id,
                                owner_user_id=str(context.user_id),
                                thread_id=thread_id,
                                run_id=run.run_id,
                                purpose=purpose,
                                model_ref=model_ref,
                            )
                except (
                    SystemModelConflict,
                    SystemModelInvalid,
                    SystemModelNotFound,
                ):
                    raise RunSnapshotAssetStale from None
                except SystemModelStorageUnavailable:
                    raise PrivateWorkUnavailable(context.request_id) from None
            if (
                not isinstance(exact_model_name, str)
                or not exact_model_name
                or not await PrivateRunRepository(session).update_model_name(
                    scope=context.resource_scope,
                    run_id=run.run_id,
                    model_name=exact_model_name,
                )
            ):
                raise RunSnapshotAssetStale
            refreshed = await PrivateRunRepository(session).get(
                scope=context.resource_scope,
                run_id=run.run_id,
                lock=True,
            )
            if refreshed is None:
                raise RunSnapshotAssetStale
            run = refreshed
        asset_rows = [
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                asset_kind=AssetKind.AGENT.value,
                dependency_order=0,
                asset_scope=resolved_agent.scope.value,
                asset_id=resolved_agent.asset_id,
                version_id=resolved_agent.version_id,
                payload_checksum=resolved_agent.checksum,
                catalog_generation=resolved_agent.catalog_generation,
            )
        ]
        dependency_order = 1
        for asset, version in skills:
            asset_rows.append(
                RunAssetVersionRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.SKILL.value,
                    dependency_order=dependency_order,
                    asset_scope=asset.scope,
                    asset_id=asset.id,
                    version_id=version.id,
                    payload_checksum=version.payload_checksum,
                    catalog_generation=resolved_agent.catalog_generation,
                )
            )
            dependency_order += 1
        for asset, version in mcps:
            asset_rows.append(
                RunAssetVersionRow(
                    project_id=context.project_id,
                    owner_user_id=str(context.user_id),
                    thread_id=thread_id,
                    run_id=run.run_id,
                    asset_kind=AssetKind.MCP.value,
                    dependency_order=dependency_order,
                    asset_scope=asset.scope,
                    asset_id=asset.id,
                    version_id=version.id,
                    payload_checksum=version.payload_checksum,
                    catalog_generation=resolved_agent.catalog_generation,
                )
            )
            dependency_order += 1
        session.add_all(asset_rows)
        session.add_all(
            RunMcpGrantSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                mcp_version_id=material.grant.mcp_server_version_id,
                credential_slot_id=material.slot.id,
                credential_grant_id=material.grant.id,
                credential_version_id=material.version.id,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        )
        session.add_all(
            RunSkillCredentialSnapshotRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run.run_id,
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.env_name,
                skill_credential_binding_id=material.binding_id,
                binding_revision=material.binding_revision,
                credential_id=material.credential_id,
                credential_version_id=material.credential_version_id,
            )
            for _asset, version in skills
            for material in skill_credential_closures[uuid.UUID(str(version.id))].materials
        )
        await session.flush()
        return run

    async def validate_agent_closure_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        resolved_agent: ResolvedAgentSnapshot,
    ) -> tuple[
        list[tuple[SkillRow, SkillVersionRow]],
        list[tuple[McpServerRow, McpServerVersionRow]],
        dict[uuid.UUID, LockedMcpCredentialClosure],
        dict[uuid.UUID, LockedSkillCredentialClosure],
    ]:
        """Lock and validate an Agent plus its exact credential-grant closure."""

        context = require_issued_private_work_context(context)
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise RunSnapshotAssetStale
        if type(resolved_agent) is not ResolvedAgentSnapshot:
            raise RunSnapshotAssetStale
        if resolved_agent.kind is not AssetKind.AGENT or resolved_agent.catalog_generation < 0:
            raise RunSnapshotAssetStale
        project_id = context.project_id
        await self._agent(session, resolved_agent, project_id)
        await self._validate_dependency_order(session, resolved_agent)
        skills = await self._skills(
            session,
            resolved_agent.payload.skill_version_ids,
            project_id,
        )
        try:
            skill_credential_closures = await lock_skill_credential_closures(
                session,
                project_id,
                tuple(
                    SkillCredentialClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
        mcps = await self._mcps(
            session,
            resolved_agent.payload.mcp_version_ids,
            project_id,
            endpoint_policy=self._endpoint_policy,
        )
        closures = await self._credential_closures(session, mcps)
        self._validate_project_mcp_credential_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        return skills, mcps, closures, skill_credential_closures

    async def list_assets(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunAssetSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_assets_in_session(session, context, run_id)
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_assets_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunAssetSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunAssetVersionRow)
            .where(
                RunAssetVersionRow.project_id == context.project_id,
                RunAssetVersionRow.owner_user_id == str(context.user_id),
                RunAssetVersionRow.run_id == run_id,
            )
            .order_by(RunAssetVersionRow.dependency_order)
        )
        if lock:
            statement = statement.with_for_update(of=RunAssetVersionRow)
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunAssetSnapshot(
                asset_kind=row.asset_kind,
                dependency_order=row.dependency_order,
                asset_scope=row.asset_scope,
                asset_id=row.asset_id,
                version_id=row.version_id,
                payload_checksum=row.payload_checksum,
                catalog_generation=row.catalog_generation,
            )
            for row in rows
        )

    async def list_mcp_grants(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_mcp_grants_in_session(session, context, run_id)
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_mcp_grants_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunMcpGrantSnapshotRow)
            .where(
                RunMcpGrantSnapshotRow.project_id == context.project_id,
                RunMcpGrantSnapshotRow.owner_user_id == str(context.user_id),
                RunMcpGrantSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunMcpGrantSnapshotRow.mcp_version_id,
                RunMcpGrantSnapshotRow.credential_slot_id,
            )
        )
        if lock:
            statement = statement.with_for_update(of=RunMcpGrantSnapshotRow)
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunMcpGrantSnapshot(
                mcp_version_id=row.mcp_version_id,
                credential_slot_id=row.credential_slot_id,
                credential_grant_id=row.credential_grant_id,
                credential_version_id=row.credential_version_id,
            )
            for row in rows
        )

    async def current_mcp_grants_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        mcp_assets: tuple[RunAssetSnapshot, ...],
    ) -> tuple[RunMcpGrantSnapshot, ...]:
        """Lock the current exact closure and return only its secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.asset_kind != AssetKind.MCP.value for asset in mcp_assets):
            raise RunSnapshotAssetStale
        mcps = await self._mcps(
            session,
            tuple(asset.version_id for asset in mcp_assets),
            context.project_id,
            endpoint_policy=self._endpoint_policy,
        )
        by_version = {uuid.UUID(str(version.id)): (asset, version) for asset, version in mcps}
        for persisted in mcp_assets:
            row = by_version.get(persisted.version_id)
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if asset.id != persisted.asset_id or asset.scope != persisted.asset_scope or version.payload_checksum != persisted.payload_checksum:
                raise RunSnapshotAssetStale
        closures = await self._credential_closures(session, mcps)
        self._validate_project_mcp_credential_slots(
            mcps,
            closures,
            endpoint_policy=self._endpoint_policy,
        )
        current = [
            RunMcpGrantSnapshot(
                mcp_version_id=material.grant.mcp_server_version_id,
                credential_slot_id=material.slot.id,
                credential_grant_id=material.grant.id,
                credential_version_id=material.version.id,
            )
            for _asset, version in mcps
            for material in closures[uuid.UUID(str(version.id))].materials
        ]
        return tuple(
            sorted(
                current,
                key=lambda item: (
                    item.mcp_version_id.int,
                    item.credential_slot_id.int,
                    item.credential_grant_id.int,
                    item.credential_version_id.int,
                ),
            )
        )

    async def list_skill_credentials(
        self,
        context: PrivateWorkContext,
        run_id: str,
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session:
                return await self.list_skill_credentials_in_session(
                    session,
                    context,
                    run_id,
                )
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_skill_credentials_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        run_id: str,
        *,
        lock: bool = False,
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        context = require_issued_private_work_context(context)
        statement = (
            select(RunSkillCredentialSnapshotRow)
            .where(
                RunSkillCredentialSnapshotRow.project_id == context.project_id,
                RunSkillCredentialSnapshotRow.owner_user_id == str(context.user_id),
                RunSkillCredentialSnapshotRow.run_id == run_id,
            )
            .order_by(
                RunSkillCredentialSnapshotRow.skill_version_id,
                RunSkillCredentialSnapshotRow.secret_name,
            )
        )
        if lock:
            statement = statement.with_for_update(
                of=RunSkillCredentialSnapshotRow,
            )
        rows = (await session.execute(statement)).scalars()
        return tuple(
            RunSkillCredentialSnapshot(
                skill_id=row.skill_id,
                skill_version_id=row.skill_version_id,
                secret_name=row.secret_name,
                skill_credential_binding_id=(row.skill_credential_binding_id),
                binding_revision=row.binding_revision,
                credential_id=row.credential_id,
                credential_version_id=row.credential_version_id,
            )
            for row in rows
        )

    async def current_skill_credentials_in_session(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        skill_assets: tuple[RunAssetSnapshot, ...],
    ) -> tuple[RunSkillCredentialSnapshot, ...]:
        """Lock the current Skill Credential closure and return secret-free IDs."""

        context = require_issued_private_work_context(context)
        if any(asset.asset_kind != AssetKind.SKILL.value for asset in skill_assets):
            raise RunSnapshotAssetStale
        skills = await self._skills(
            session,
            tuple(asset.version_id for asset in skill_assets),
            context.project_id,
        )
        by_version = {uuid.UUID(str(version.id)): (asset, version) for asset, version in skills}
        for persisted in skill_assets:
            row = by_version.get(persisted.version_id)
            if row is None:
                raise RunSnapshotAssetStale
            asset, version = row
            if asset.id != persisted.asset_id or asset.scope != persisted.asset_scope or version.payload_checksum != persisted.payload_checksum:
                raise RunSnapshotAssetStale
        try:
            closures = await lock_skill_credential_closures(
                session,
                context.project_id,
                tuple(
                    SkillCredentialClosureTarget(
                        skill_id=uuid.UUID(str(asset.id)),
                        skill_version_id=uuid.UUID(str(version.id)),
                    )
                    for asset, version in skills
                ),
                load_envelopes=False,
                require_required=True,
            )
        except SkillCredentialClosureInvalid:
            raise RunSnapshotAssetStale from None
        current = [
            RunSkillCredentialSnapshot(
                skill_id=material.skill_id,
                skill_version_id=material.skill_version_id,
                secret_name=material.env_name,
                skill_credential_binding_id=material.binding_id,
                binding_revision=material.binding_revision,
                credential_id=material.credential_id,
                credential_version_id=material.credential_version_id,
            )
            for _asset, version in skills
            for material in closures[uuid.UUID(str(version.id))].materials
        ]
        return tuple(
            sorted(
                current,
                key=lambda item: (
                    item.skill_version_id.int,
                    item.secret_name,
                    item.skill_credential_binding_id.int,
                    item.credential_version_id.int,
                ),
            )
        )
