from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_catalog import (
    AgentCatalogValidationPort,
    RejectingAgentCatalogValidator,
    require_agent_catalog_validation,
)
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_repository import AgentRepository, AgentVersionRecord
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetScope,
    WorkflowStatus,
)
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
AGENT_INSTRUCTION_FIELDS = frozenset(
    {
        "agents_instructions",
        "soul",
        "identity",
        "user_context",
    }
)
MAX_AGENT_INSTRUCTION_FIELD_BYTES = 32 * 1024
MAX_AGENT_INSTRUCTIONS_TOTAL_BYTES = 64 * 1024
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "fk_project_channel_group_binding_challenges_agent",
        "fk_project_channel_group_bindings_agent",
        "uq_agents_project_slug",
        "uq_agents_system_slug",
        "uq_agent_versions_asset_number",
    }
)
_Actor = ProjectContext | SystemAssetGovernanceContext | SystemAssetReadContext
_T = TypeVar("_T")


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


@dataclass(frozen=True)
class CreateAgent:
    slug: str
    display_name: str


@dataclass(frozen=True)
class AgentInstructions:
    agents_instructions: str
    soul: str
    identity: str
    user_context: str


@dataclass(frozen=True)
class AgentCapabilityBindings:
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class AgentAssetView:
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    current_published_version_id: uuid.UUID | None
    version: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime
    description: str = ""


@dataclass(frozen=True)
class AgentVersionView:
    id: uuid.UUID
    agent_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    created_by_user_id: str
    created_at: datetime
    agents_instructions: str = ""
    identity: str = ""
    user_context: str = ""
    payload_schema_version: int = 1
    model_settings: AgentModelSettings = AgentModelSettings()


@dataclass(frozen=True)
class ProjectAgentCreateResult:
    asset: AgentAssetView
    version: AgentVersionView


class AgentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
        *,
        catalog_validator: AgentCatalogValidationPort | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()
        self._catalog_validator = catalog_validator or RejectingAgentCatalogValidator()

    async def create_project_from_design_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        command: CreateAgent,
        payload: AgentPayload,
        *,
        publish: bool = False,
    ) -> ProjectAgentCreateResult:
        """Atomically create a suspended project Agent with immutable v1.

        The caller owns the transaction so a design-session commit can move its
        own state and create the complete immutable Agent package together.
        Normal authoring leaves v1 as a draft; publisher-authorized bootstrap
        callers may publish within their already-authorized transaction.
        """

        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        return await self._create_project_package(
            AgentRepository(session),
            actor,
            command,
            payload,
            publish=publish,
        )

    async def _create_project_package(
        self,
        repository: AgentRepository,
        actor: ProjectContext | SystemAssetGovernanceContext,
        command: CreateAgent,
        payload: AgentPayload,
        *,
        publish: bool,
    ) -> ProjectAgentCreateResult:
        command = self._validate_create(actor, command)
        payload = self._validate_payload(
            actor,
            payload,
            payload_schema_version=self._payload_schema_version(payload),
        )
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        if publish:
            self._require_capability(
                actor,
                Capability.SHARED_ASSETS_MANAGE_BINDINGS,
            )
        if not isinstance(actor, ProjectContext) and actor.project_id is None:
            raise AssetForbidden(actor.request_id)
        await require_agent_catalog_validation(
            self._catalog_validator,
            repository.session,
            request_id=actor.request_id,
            model_ref=payload.model_ref,
            tool_groups=payload.tool_groups,
        )
        await self._validate_dependency_closure(
            repository,
            actor,
            payload.skill_version_ids,
            payload.mcp_version_ids,
        )
        if isinstance(actor, ProjectContext):
            asset = await repository.create_project_asset(actor, command)
        else:
            asset = await repository.create_override_asset(actor, command)
        asset.status = "suspended"
        await repository.session.flush()
        await self._record_governance(
            repository.session,
            actor,
            asset.id,
            None,
            "agent.create",
        )
        payload_schema_version = self._payload_schema_version(payload)
        row = AgentVersionRow(
            agent_id=asset.id,
            version_number=1,
            workflow_status=WorkflowStatus.DRAFT.value,
            description=payload.description,
            agents_instructions=payload.agents_instructions,
            soul=payload.soul,
            identity=payload.identity,
            user_context=payload.user_context,
            model_ref=payload.model_ref,
            model_settings=self._model_settings_json(payload.model_settings),
            tool_groups=list(payload.tool_groups),
            supersedes_version_id=None,
            payload_schema_version=payload_schema_version,
            payload_checksum=self._payload_checksum(
                payload,
                payload_schema_version=payload_schema_version,
            ),
            created_by_user_id=str(actor.user_id),
        )
        record = await self._create_version_record(
            repository,
            actor,
            asset.id,
            row,
            payload.skill_version_ids,
            payload.mcp_version_ids,
        )
        if publish:
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            await repository.session.flush()
            asset.current_published_version_id = record.row.id
        asset.version += 1
        await repository.session.flush()
        await self._record_governance(
            repository.session,
            actor,
            asset.id,
            record.row.id,
            "agent.version.create",
        )
        if publish:
            await self._record_governance(
                repository.session,
                actor,
                asset.id,
                record.row.id,
                "agent.publish",
            )
        return ProjectAgentCreateResult(
            asset=self._asset_view(asset),
            version=self._version_view(record),
        )

    async def create_project(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        command: CreateAgent,
        payload: AgentPayload,
    ) -> ProjectAgentCreateResult:
        """Create one complete project-scoped Agent draft atomically."""

        if not isinstance(actor, (ProjectContext, SystemAssetGovernanceContext)) or (isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is None):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))

        async def operation(repository: AgentRepository) -> ProjectAgentCreateResult:
            return await self._create_project_package(
                repository,
                actor,
                command,
                payload,
                publish=False,
            )

        return await self._execute(actor, operation)

    async def create_version(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        payload: AgentPayload,
        *,
        expected_asset_version: int,
        provided_instruction_fields: frozenset[str] | None = None,
    ) -> AgentVersionView:
        payload = self._validate_payload(
            actor,
            payload,
            payload_schema_version=self._payload_schema_version(payload),
        )
        provided_instruction_fields = self._normalize_provided_instruction_fields(
            actor,
            payload,
            provided_instruction_fields,
        )
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status not in {"active", "suspended"}:
                raise AssetConflict(actor.request_id)
            effective_payload = payload
            if provided_instruction_fields != AGENT_INSTRUCTION_FIELDS:
                base = await self._authoring_base(repository, actor, asset)
                if base is not None:
                    effective_payload = self._merge_instructions(
                        effective_payload,
                        self._instructions_from_record(base),
                        provided_instruction_fields,
                    )
            effective_payload = self._validate_payload(
                actor,
                effective_payload,
                payload_schema_version=self._payload_schema_version(effective_payload),
            )
            await self._validate_dependency_closure(
                repository,
                actor,
                effective_payload.skill_version_ids,
                effective_payload.mcp_version_ids,
            )
            if isinstance(actor, ProjectContext):
                version_number = await repository.next_project_version_number(actor, asset)
            elif actor.project_id is not None:
                version_number = await repository.next_override_version_number(actor, asset)
            else:
                version_number = await repository.next_system_version_number(actor, asset)
            payload_schema_version = self._payload_schema_version(effective_payload)
            row = AgentVersionRow(
                agent_id=asset.id,
                version_number=version_number,
                workflow_status=WorkflowStatus.DRAFT.value,
                description=effective_payload.description,
                agents_instructions=effective_payload.agents_instructions,
                soul=effective_payload.soul,
                identity=effective_payload.identity,
                user_context=effective_payload.user_context,
                model_ref=effective_payload.model_ref,
                model_settings=self._model_settings_json(effective_payload.model_settings),
                tool_groups=list(effective_payload.tool_groups),
                supersedes_version_id=asset.current_published_version_id,
                payload_schema_version=payload_schema_version,
                payload_checksum=self._payload_checksum(
                    effective_payload,
                    payload_schema_version=payload_schema_version,
                ),
                created_by_user_id=str(actor.user_id),
            )
            if isinstance(actor, ProjectContext):
                record = await repository.create_project_version(
                    actor,
                    asset.id,
                    row,
                    effective_payload.skill_version_ids,
                    effective_payload.mcp_version_ids,
                )
            elif actor.project_id is not None:
                record = await repository.create_override_version(
                    actor,
                    asset.id,
                    row,
                    effective_payload.skill_version_ids,
                    effective_payload.mcp_version_ids,
                )
            else:
                record = await repository.create_system_version(
                    actor,
                    asset.id,
                    row,
                    effective_payload.skill_version_ids,
                    effective_payload.mcp_version_ids,
                )
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                asset_id,
                result.id,
                "agent.version.create",
            ),
        )

    async def update_instructions(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        instructions: AgentInstructions,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        instructions = self._validate_instructions(actor, instructions)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status not in {"active", "suspended"}:
                raise AssetConflict(actor.request_id)

            has_published_version = asset.current_published_version_id is not None
            base = await self._authoring_base(repository, actor, asset)
            if base is None:
                description = ""
                model_ref = ""
                tool_groups: tuple[str, ...] = ()
                skill_version_ids: tuple[uuid.UUID, ...] = ()
                mcp_version_ids: tuple[uuid.UUID, ...] = ()
                supersedes_version_id = None
                model_settings = AgentModelSettings()
            else:
                description = base.row.description
                model_ref = base.row.model_ref
                tool_groups = tuple(base.row.tool_groups)
                skill_version_ids = base.skill_version_ids
                mcp_version_ids = base.mcp_version_ids
                # Publishing is a compare-and-swap against the current live
                # pointer. A later draft may inherit content from the latest
                # draft, but its publication base remains the live version
                # (or ``None`` for an Agent that has never been published).
                supersedes_version_id = asset.current_published_version_id
                model_settings = self._model_settings_from_row(
                    base.row.model_settings,
                    actor.request_id,
                )

            if has_published_version:
                await self._validate_dependency_closure(
                    repository,
                    actor,
                    skill_version_ids,
                    mcp_version_ids,
                )

            payload = AgentPayload(
                description=description,
                soul=instructions.soul,
                model_ref=model_ref,
                tool_groups=tool_groups,
                skill_version_ids=skill_version_ids,
                mcp_version_ids=mcp_version_ids,
                agents_instructions=instructions.agents_instructions,
                identity=instructions.identity,
                user_context=instructions.user_context,
                model_settings=model_settings,
            )
            version_number = await self._next_version_number(repository, actor, asset)
            payload_schema_version = self._payload_schema_version(payload)
            row = AgentVersionRow(
                agent_id=asset.id,
                version_number=version_number,
                workflow_status=WorkflowStatus.DRAFT.value,
                description=payload.description,
                agents_instructions=payload.agents_instructions,
                soul=payload.soul,
                identity=payload.identity,
                user_context=payload.user_context,
                model_ref=payload.model_ref,
                model_settings=self._model_settings_json(payload.model_settings),
                tool_groups=list(payload.tool_groups),
                supersedes_version_id=supersedes_version_id,
                payload_schema_version=payload_schema_version,
                payload_checksum=self._payload_checksum(
                    payload,
                    payload_schema_version=payload_schema_version,
                ),
                created_by_user_id=str(actor.user_id),
            )
            record = await self._create_version_record(
                repository,
                actor,
                asset.id,
                row,
                payload.skill_version_ids,
                payload.mcp_version_ids,
            )
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                asset_id,
                result.id,
                "agent.instructions.update",
            ),
        )

    async def update_capability_bindings(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        bindings: AgentCapabilityBindings,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        bindings = self._validate_capability_bindings(actor, bindings)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(
                repository,
                actor,
                asset_id,
                for_update=True,
            )
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status not in {"active", "suspended"}:
                raise AssetConflict(actor.request_id)
            base = await self._authoring_base(repository, actor, asset)
            if base is None:
                raise AssetConflict(actor.request_id)
            await self._validate_dependency_closure(
                repository,
                actor,
                bindings.skill_version_ids,
                bindings.mcp_version_ids,
            )
            payload = AgentPayload(
                description=base.row.description,
                soul=base.row.soul,
                model_ref=base.row.model_ref,
                tool_groups=tuple(base.row.tool_groups),
                skill_version_ids=bindings.skill_version_ids,
                mcp_version_ids=bindings.mcp_version_ids,
                agents_instructions=base.row.agents_instructions,
                identity=base.row.identity,
                user_context=base.row.user_context,
                payload_schema_version=base.row.payload_schema_version,
                model_settings=self._model_settings_from_row(
                    base.row.model_settings,
                    actor.request_id,
                ),
            )
            payload = self._validate_payload(
                actor,
                payload,
                payload_schema_version=base.row.payload_schema_version,
            )
            payload_schema_version = base.row.payload_schema_version
            row = AgentVersionRow(
                agent_id=asset.id,
                version_number=await self._next_version_number(
                    repository,
                    actor,
                    asset,
                ),
                workflow_status=WorkflowStatus.DRAFT.value,
                description=payload.description,
                agents_instructions=payload.agents_instructions,
                soul=payload.soul,
                identity=payload.identity,
                user_context=payload.user_context,
                model_ref=payload.model_ref,
                model_settings=self._model_settings_json(payload.model_settings),
                tool_groups=list(payload.tool_groups),
                supersedes_version_id=asset.current_published_version_id,
                payload_schema_version=payload_schema_version,
                payload_checksum=self._payload_checksum(
                    payload,
                    payload_schema_version=payload_schema_version,
                ),
                created_by_user_id=str(actor.user_id),
            )
            record = await self._create_version_record(
                repository,
                actor,
                asset.id,
                row,
                payload.skill_version_ids,
                payload.mcp_version_ids,
            )
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                asset_id,
                result.id,
                "agent.capability_bindings.update",
            ),
        )

    async def restore_version(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(
                repository,
                actor,
                asset_id,
                for_update=True,
            )
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status not in {"active", "suspended"}:
                raise AssetConflict(actor.request_id)
            target = await self._get_version(
                repository,
                actor,
                asset.id,
                version_id,
                for_update=True,
            )
            if target.row.workflow_status not in {
                WorkflowStatus.PUBLISHED.value,
                WorkflowStatus.DRAFT.value,
            }:
                raise AssetConflict(actor.request_id)
            await self._validate_dependency_closure(
                repository,
                actor,
                target.skill_version_ids,
                target.mcp_version_ids,
            )
            payload = AgentPayload(
                description=target.row.description,
                soul=target.row.soul,
                model_ref=target.row.model_ref,
                tool_groups=tuple(target.row.tool_groups),
                skill_version_ids=target.skill_version_ids,
                mcp_version_ids=target.mcp_version_ids,
                agents_instructions=target.row.agents_instructions,
                identity=target.row.identity,
                user_context=target.row.user_context,
                payload_schema_version=target.row.payload_schema_version,
                model_settings=self._model_settings_from_row(
                    target.row.model_settings,
                    actor.request_id,
                ),
            )
            payload = self._validate_payload(
                actor,
                payload,
                payload_schema_version=target.row.payload_schema_version,
            )
            if (
                self._payload_checksum(
                    payload,
                    payload_schema_version=target.row.payload_schema_version,
                )
                != target.row.payload_checksum
            ):
                raise AssetValidationFailed(actor.request_id)
            row = AgentVersionRow(
                agent_id=asset.id,
                version_number=await self._next_version_number(
                    repository,
                    actor,
                    asset,
                ),
                workflow_status=WorkflowStatus.DRAFT.value,
                description=payload.description,
                agents_instructions=payload.agents_instructions,
                soul=payload.soul,
                identity=payload.identity,
                user_context=payload.user_context,
                model_ref=payload.model_ref,
                model_settings=self._model_settings_json(payload.model_settings),
                tool_groups=list(payload.tool_groups),
                supersedes_version_id=asset.current_published_version_id,
                payload_schema_version=target.row.payload_schema_version,
                payload_checksum=target.row.payload_checksum,
                created_by_user_id=str(actor.user_id),
            )
            record = await self._create_version_record(
                repository,
                actor,
                asset.id,
                row,
                payload.skill_version_ids,
                payload.mcp_version_ids,
            )
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                asset_id,
                result.id,
                "agent.version.restore",
            ),
        )

    async def publish(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentVersionView:
        self._require_capability(
            actor,
            Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        )

        async def operation(repository: AgentRepository) -> AgentVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status not in {"active", "suspended"}:
                raise AssetConflict(actor.request_id)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            if record.row.workflow_status != WorkflowStatus.DRAFT.value:
                raise AssetConflict(actor.request_id)
            if record.row.supersedes_version_id != asset.current_published_version_id:
                raise AssetConflict(actor.request_id)
            await self._validate_dependency_closure(
                repository,
                actor,
                record.skill_version_ids,
                record.mcp_version_ids,
            )
            current_payload = AgentPayload(
                description=record.row.description,
                soul=record.row.soul,
                model_ref=record.row.model_ref,
                tool_groups=tuple(record.row.tool_groups),
                skill_version_ids=record.skill_version_ids,
                mcp_version_ids=record.mcp_version_ids,
                agents_instructions=record.row.agents_instructions,
                identity=record.row.identity,
                user_context=record.row.user_context,
                model_settings=self._model_settings_from_row(
                    record.row.model_settings,
                    actor.request_id,
                ),
            )
            current_payload = self._validate_payload(
                actor,
                current_payload,
                payload_schema_version=record.row.payload_schema_version,
            )
            if (
                self._payload_checksum(
                    current_payload,
                    payload_schema_version=record.row.payload_schema_version,
                )
                != record.row.payload_checksum
            ):
                raise AssetValidationFailed(actor.request_id)
            await require_agent_catalog_validation(
                self._catalog_validator,
                repository.session,
                request_id=actor.request_id,
                model_ref=current_payload.model_ref,
                tool_groups=current_payload.tool_groups,
            )
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            asset.current_published_version_id = record.row.id
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                asset_id,
                result.id,
                "agent.publish",
            ),
        )

    async def delete(
        self,
        actor: ProjectContext,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> None:
        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> bool:
            cleared_default = await repository.clear_current_project_default(
                actor,
                asset_id,
            )
            asset = await repository.get_project_asset(
                actor,
                asset_id,
                for_update=True,
            )
            self._require_expected_version(
                actor,
                asset,
                expected_asset_version,
            )
            if asset.current_published_version_id is not None:
                self._require_capability(
                    actor,
                    Capability.SHARED_ASSETS_MANAGE_BINDINGS,
                )
            await repository.archive_project_asset(
                actor,
                asset,
            )
            return cleared_default

        async def governance(session: AsyncSession, cleared_default: bool) -> None:
            if cleared_default:
                await self._record_governance(
                    session,
                    actor,
                    asset_id,
                    None,
                    "agent.default.clear",
                )
            await self._record_governance(
                session,
                actor,
                asset_id,
                None,
                "agent.delete",
            )

        await self._execute(
            actor,
            operation,
            governance=governance,
        )

    async def suspend(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        return await self._suspend_asset(
            actor,
            asset_id,
            expected_asset_version=expected_asset_version,
        )

    async def activate(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)

        async def operation(repository: AgentRepository) -> AgentAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "suspended" or asset.current_published_version_id is None:
                raise AssetConflict(actor.request_id)
            published = await self._get_version(
                repository,
                actor,
                asset.id,
                asset.current_published_version_id,
                for_update=True,
            )
            if published.row.workflow_status != WorkflowStatus.PUBLISHED.value:
                raise AssetConflict(actor.request_id)
            await require_agent_catalog_validation(
                self._catalog_validator,
                repository.session,
                request_id=actor.request_id,
                model_ref=published.row.model_ref,
                tool_groups=tuple(published.row.tool_groups),
            )
            await self._validate_dependency_closure(
                repository,
                actor,
                published.skill_version_ids,
                published.mcp_version_ids,
            )
            asset.status = "active"
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.id,
                result.current_published_version_id,
                "agent.activate",
            ),
        )

    async def get(self, actor: _Actor, asset_id: uuid.UUID) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> AgentAssetView:
            return self._asset_view(await self._get_asset(repository, actor, asset_id))

        return await self._execute(actor, operation)

    async def list_visible(self, actor: _Actor) -> tuple[AgentAssetView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> tuple[AgentAssetView, ...]:
            if isinstance(actor, ProjectContext):
                rows = await repository.list_project_visible(actor)
            elif actor.project_id is not None:
                rows = await repository.list_override_visible(actor)
            else:
                rows = await repository.list_system_visible(actor)
            descriptions = await repository.current_published_descriptions(
                tuple(row.id for row in rows),
            )
            return tuple(
                self._asset_view(
                    row,
                    description=descriptions.get(row.id, ""),
                )
                for row in rows
            )

        return await self._execute(actor, operation)

    async def get_version_history(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
    ) -> tuple[AgentVersionView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> tuple[AgentVersionView, ...]:
            if isinstance(actor, ProjectContext):
                records = await repository.get_project_version_history(actor, asset_id)
            elif actor.project_id is not None:
                records = await repository.get_override_version_history(actor, asset_id)
            else:
                records = await repository.get_system_version_history(actor, asset_id)
            return tuple(self._version_view(record) for record in records)

        return await self._execute(actor, operation)

    async def _suspend_asset(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> AgentAssetView:
        async def operation(repository: AgentRepository) -> AgentAssetView:
            await repository.ensure_not_current_project_default(actor, asset_id)
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            asset.status = "suspended"
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result.id,
                None,
                "agent.suspend",
            ),
        )

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[AgentRepository], Awaitable[_T]],
        governance: Callable[[AsyncSession, _T], Awaitable[None]] | None = None,
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await operation(AgentRepository(session))
                    if governance is not None:
                        await governance(session, result)
                    return result
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(actor.request_id) from None
            raise AssetStorageUnavailable(actor.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(actor.request_id) from None

    @staticmethod
    async def _get_asset(
        repository: AgentRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentRow:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_asset(actor, asset_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_asset(actor, asset_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_asset(actor, asset_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _get_version(
        repository: AgentRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersionRecord:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.get_override_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_version(
                actor,
                asset_id,
                version_id,
                for_update=for_update,
            )
        raise AssetForbidden("unknown")

    @staticmethod
    def _validate_create(actor: _Actor, command: CreateAgent) -> CreateAgent:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateAgent):
            raise AssetValidationFailed(request_id)
        slug = command.slug.strip()
        display_name = command.display_name.strip()
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(request_id)
        return CreateAgent(slug=slug, display_name=display_name)

    @staticmethod
    def _validate_payload(
        actor: _Actor,
        payload: AgentPayload,
        *,
        payload_schema_version: int,
    ) -> AgentPayload:
        request_id = getattr(actor, "request_id", "unknown")
        if (
            not isinstance(payload, AgentPayload)
            or not isinstance(payload_schema_version, int)
            or isinstance(payload_schema_version, bool)
            or payload_schema_version not in (1, 2, 3)
            or not isinstance(payload.model_settings, AgentModelSettings)
        ):
            raise AssetValidationFailed(request_id)
        try:
            model_settings = AgentModelSettings.model_validate(
                payload.model_settings,
            )
        except ValidationError:
            raise AssetValidationFailed(request_id) from None
        if not all(
            isinstance(value, str)
            for value in (
                payload.description,
                payload.agents_instructions,
                payload.soul,
                payload.identity,
                payload.user_context,
                payload.model_ref,
            )
        ):
            raise AssetValidationFailed(request_id)
        try:
            normalized = AgentPayload(
                description=payload.description,
                soul=payload.soul,
                model_ref=payload.model_ref,
                tool_groups=tuple(payload.tool_groups),
                skill_version_ids=tuple(payload.skill_version_ids),
                mcp_version_ids=tuple(payload.mcp_version_ids),
                agents_instructions=payload.agents_instructions,
                identity=payload.identity,
                user_context=payload.user_context,
                payload_schema_version=payload_schema_version,
                model_settings=model_settings,
            )
        except TypeError:
            raise AssetValidationFailed(request_id) from None
        if normalized.model_ref != DEFAULT_MODEL_REF and exact_model_ref(normalized.model_ref) is None:
            raise AssetValidationFailed(request_id)
        if payload_schema_version in (1, 2) and not normalized.model_settings.is_empty:
            raise AssetValidationFailed(request_id)
        if payload_schema_version == 1:
            if not normalized.soul.strip():
                raise AssetValidationFailed(request_id)
        else:
            AgentService._validate_instruction_sizes(
                actor,
                AgentInstructions(
                    agents_instructions=normalized.agents_instructions,
                    soul=normalized.soul,
                    identity=normalized.identity,
                    user_context=normalized.user_context,
                ),
            )
        if any(not isinstance(group, str) or not group.strip() for group in normalized.tool_groups):
            raise AssetValidationFailed(request_id)
        if len(set(normalized.tool_groups)) != len(normalized.tool_groups):
            raise AssetValidationFailed(request_id)
        for values in (normalized.skill_version_ids, normalized.mcp_version_ids):
            if any(not isinstance(value, uuid.UUID) for value in values) or len(set(values)) != len(values):
                raise AssetValidationFailed(request_id)
        return normalized

    @staticmethod
    def _validate_instructions(
        actor: _Actor,
        instructions: AgentInstructions,
    ) -> AgentInstructions:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(instructions, AgentInstructions):
            raise AssetValidationFailed(request_id)
        if not all(
            isinstance(value, str)
            for value in (
                instructions.agents_instructions,
                instructions.soul,
                instructions.identity,
                instructions.user_context,
            )
        ):
            raise AssetValidationFailed(request_id)
        AgentService._validate_instruction_sizes(actor, instructions)
        return instructions

    @staticmethod
    def _validate_capability_bindings(
        actor: _Actor,
        bindings: AgentCapabilityBindings,
    ) -> AgentCapabilityBindings:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(bindings, AgentCapabilityBindings):
            raise AssetValidationFailed(request_id)
        try:
            skill_version_ids = tuple(bindings.skill_version_ids)
            mcp_version_ids = tuple(bindings.mcp_version_ids)
        except TypeError:
            raise AssetValidationFailed(request_id) from None
        for values in (skill_version_ids, mcp_version_ids):
            if any(not isinstance(value, uuid.UUID) for value in values) or len(set(values)) != len(values):
                raise AssetValidationFailed(request_id)
        return AgentCapabilityBindings(skill_version_ids, mcp_version_ids)

    @staticmethod
    def _validate_instruction_sizes(
        actor: _Actor,
        instructions: AgentInstructions,
    ) -> None:
        encoded_sizes = tuple(
            len(value.encode("utf-8"))
            for value in (
                instructions.agents_instructions,
                instructions.soul,
                instructions.identity,
                instructions.user_context,
            )
        )
        if any(size > MAX_AGENT_INSTRUCTION_FIELD_BYTES for size in encoded_sizes) or sum(encoded_sizes) > MAX_AGENT_INSTRUCTIONS_TOTAL_BYTES:
            raise AssetValidationFailed(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _require_capability(actor: _Actor, capability: Capability) -> None:
        if isinstance(actor, SystemAssetGovernanceContext):
            if actor.project_id is not None or capability is Capability.SHARED_ASSETS_READ:
                return
            raise AssetForbidden(actor.request_id)
        if isinstance(actor, SystemAssetReadContext) and capability is Capability.SHARED_ASSETS_READ:
            return
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        request_id = getattr(actor, "request_id", "unknown")
        raise AssetForbidden(request_id)

    @staticmethod
    def _require_expected_version(actor: _Actor, asset: AgentRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or asset.version != expected:
            raise AssetConflict(actor.request_id)

    async def _validate_dependency_closure(
        self,
        repository: AgentRepository,
        actor: _Actor,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> None:
        if isinstance(actor, ProjectContext):
            resolved_skill_ids = await repository.resolve_project_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_project_mcp_versions(actor, mcp_version_ids)
        elif isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            resolved_skill_ids = await repository.resolve_override_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_override_mcp_versions(actor, mcp_version_ids)
        elif isinstance(actor, SystemAssetGovernanceContext):
            resolved_skill_ids = await repository.resolve_system_skill_versions(actor, skill_version_ids)
            resolved_mcp_ids = await repository.resolve_system_mcp_versions(actor, mcp_version_ids)
        else:
            raise AssetForbidden("unknown")
        if set(resolved_skill_ids) != set(skill_version_ids) or set(resolved_mcp_ids) != set(mcp_version_ids):
            raise AssetValidationFailed(actor.request_id)
        skill_slugs = await repository.lock_skill_version_slugs(
            skill_version_ids,
        )
        if len(skill_slugs) != len(skill_version_ids) or len({slug.casefold() for slug in skill_slugs}) != len(skill_slugs):
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _payload_schema_version(payload: object) -> int:
        if isinstance(payload, AgentPayload) and isinstance(payload.model_settings, AgentModelSettings) and not payload.model_settings.is_empty:
            return 3
        return 2

    @staticmethod
    def _model_settings_json(
        settings: AgentModelSettings,
    ) -> dict[str, object]:
        if not isinstance(settings, AgentModelSettings):
            raise ValueError("invalid Agent model settings")
        try:
            canonical = AgentModelSettings.model_validate(settings)
        except ValidationError:
            raise ValueError("invalid Agent model settings") from None
        return canonical.model_dump(exclude_none=True)

    @staticmethod
    def _model_settings_from_row(
        value: object,
        request_id: str,
    ) -> AgentModelSettings:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise AssetValidationFailed(request_id)
        try:
            return AgentModelSettings.model_validate(value)
        except ValidationError:
            raise AssetValidationFailed(request_id) from None

    @staticmethod
    def _payload_checksum(
        payload: AgentPayload,
        *,
        payload_schema_version: int = 1,
    ) -> str:
        return agent_payload_checksum(
            payload,
            payload_schema_version=payload_schema_version,
        )

    @staticmethod
    def _asset_view(
        row: AgentRow,
        *,
        description: str = "",
    ) -> AgentAssetView:
        return AgentAssetView(
            id=row.id,
            scope=AssetScope(row.scope),
            project_id=row.project_id,
            slug=row.slug,
            display_name=row.display_name,
            status=row.status,
            current_published_version_id=row.current_published_version_id,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            description=description,
        )

    @staticmethod
    def _version_view(record: AgentVersionRecord) -> AgentVersionView:
        row = record.row
        return AgentVersionView(
            id=row.id,
            agent_id=row.agent_id,
            version_number=row.version_number,
            workflow_status=WorkflowStatus(row.workflow_status),
            description=row.description,
            soul=row.soul,
            model_ref=row.model_ref,
            tool_groups=tuple(row.tool_groups),
            skill_version_ids=record.skill_version_ids,
            mcp_version_ids=record.mcp_version_ids,
            supersedes_version_id=row.supersedes_version_id,
            payload_checksum=row.payload_checksum,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            agents_instructions=row.agents_instructions,
            identity=row.identity,
            user_context=row.user_context,
            payload_schema_version=row.payload_schema_version,
            model_settings=AgentService._model_settings_from_row(
                row.model_settings,
                "unknown",
            ),
        )

    @staticmethod
    def _normalize_provided_instruction_fields(
        actor: _Actor,
        payload: AgentPayload,
        provided_instruction_fields: frozenset[str] | None,
    ) -> frozenset[str]:
        if provided_instruction_fields is None:
            return frozenset(field_name for field_name in AGENT_INSTRUCTION_FIELDS if getattr(payload, field_name) != "")
        try:
            normalized = frozenset(provided_instruction_fields)
        except TypeError:
            raise AssetValidationFailed(getattr(actor, "request_id", "unknown")) from None
        if any(not isinstance(field_name, str) or field_name not in AGENT_INSTRUCTION_FIELDS for field_name in normalized):
            raise AssetValidationFailed(getattr(actor, "request_id", "unknown"))
        return normalized

    @staticmethod
    def _instructions_from_record(record: AgentVersionRecord) -> AgentInstructions:
        return AgentInstructions(
            agents_instructions=record.row.agents_instructions,
            soul=record.row.soul,
            identity=record.row.identity,
            user_context=record.row.user_context,
        )

    @staticmethod
    def _merge_instructions(
        payload: AgentPayload,
        instructions: AgentInstructions,
        provided_instruction_fields: frozenset[str],
    ) -> AgentPayload:
        return AgentPayload(
            description=payload.description,
            soul=(payload.soul if "soul" in provided_instruction_fields else instructions.soul),
            model_ref=payload.model_ref,
            tool_groups=payload.tool_groups,
            skill_version_ids=payload.skill_version_ids,
            mcp_version_ids=payload.mcp_version_ids,
            agents_instructions=(payload.agents_instructions if "agents_instructions" in provided_instruction_fields else instructions.agents_instructions),
            identity=(payload.identity if "identity" in provided_instruction_fields else instructions.identity),
            user_context=(payload.user_context if "user_context" in provided_instruction_fields else instructions.user_context),
            payload_schema_version=payload.payload_schema_version,
            model_settings=payload.model_settings,
        )

    @classmethod
    async def _authoring_base(
        cls,
        repository: AgentRepository,
        actor: _Actor,
        asset: AgentRow,
    ) -> AgentVersionRecord | None:
        if isinstance(actor, ProjectContext):
            latest = await repository.get_latest_project_version(actor, asset.id)
        elif isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            latest = await repository.get_latest_override_version(actor, asset.id)
        elif isinstance(actor, SystemAssetGovernanceContext):
            latest = await repository.get_latest_system_version(actor, asset.id)
        else:
            raise AssetForbidden("unknown")

        live_version_id = asset.current_published_version_id
        if latest is not None and (latest.row.id == live_version_id or (latest.row.workflow_status == WorkflowStatus.DRAFT.value and latest.row.supersedes_version_id == live_version_id)):
            return latest
        if live_version_id is not None:
            return await cls._get_version(
                repository,
                actor,
                asset.id,
                live_version_id,
            )
        return None

    @staticmethod
    async def _next_version_number(
        repository: AgentRepository,
        actor: _Actor,
        asset: AgentRow,
    ) -> int:
        if isinstance(actor, ProjectContext):
            return await repository.next_project_version_number(actor, asset)
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.next_override_version_number(actor, asset)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.next_system_version_number(actor, asset)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _create_version_record(
        repository: AgentRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        row: AgentVersionRow,
        skill_version_ids: Sequence[uuid.UUID],
        mcp_version_ids: Sequence[uuid.UUID],
    ) -> AgentVersionRecord:
        if isinstance(actor, ProjectContext):
            return await repository.create_project_version(
                actor,
                asset_id,
                row,
                skill_version_ids,
                mcp_version_ids,
            )
        if isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is not None:
            return await repository.create_override_version(
                actor,
                asset_id,
                row,
                skill_version_ids,
                mcp_version_ids,
            )
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.create_system_version(
                actor,
                asset_id,
                row,
                skill_version_ids,
                mcp_version_ids,
            )
        raise AssetForbidden("unknown")

    async def _record_governance(
        self,
        session: AsyncSession,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
    ) -> None:
        if isinstance(actor, ProjectContext):
            await self._governance_sink.append_project(
                session,
                actor=actor.user_id,
                project_id=actor.project_id,
                asset_id=asset_id,
                version_id=version_id,
                action=action,
                request_id=actor.request_id,
                asset_kind="agent",
            )
            return
        if not isinstance(actor, SystemAssetGovernanceContext):
            return
        await self._governance_sink.append_override(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            request_id=actor.request_id,
            asset_kind="agent",
        )
