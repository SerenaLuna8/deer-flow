from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypeVar

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_catalog import AgentCatalogValidationPort, RejectingAgentCatalogValidator, require_agent_catalog_validation
from app.shared_assets.agent_payload_checksum import agent_payload_checksum, agent_payload_checksum_matches
from app.shared_assets.agent_repository import AgentDefinitionRecord, AgentRepository
from app.shared_assets.contexts import SystemAssetGovernanceContext, SystemAssetReadContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetStorageUnavailable, AssetValidationFailed, SharedAssetError
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import AgentModelSettings, AgentPayload, AssetScope, SkillAssetRef
from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from deerflow.persistence.shared_assets import AgentRow

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
AGENT_INSTRUCTION_FIELDS = frozenset({"agents_instructions", "soul", "identity", "user_context"})
MAX_AGENT_INSTRUCTION_FIELD_BYTES = 32 * 1024
MAX_AGENT_INSTRUCTIONS_TOTAL_BYTES = 64 * 1024
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "fk_project_channel_group_binding_challenges_agent",
        "fk_project_channel_group_bindings_agent",
        "uq_agents_definition_id",
        "uq_agents_project_slug",
        "uq_agents_system_slug",
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
    skill_refs: tuple[SkillAssetRef, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class AgentAssetView:
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    definition_id: uuid.UUID
    revision: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime
    description: str = ""


@dataclass(frozen=True)
class AgentDefinitionView:
    definition_id: uuid.UUID
    agent_id: uuid.UUID
    description: str
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_ref: str
    model_settings: AgentModelSettings
    tool_groups: tuple[str, ...]
    skill_refs: tuple[SkillAssetRef, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    payload_schema_version: int
    payload_checksum: str
    updated_by_user_id: str
    updated_at: datetime


@dataclass(frozen=True)
class ProjectAgentCreateResult:
    asset: AgentAssetView
    definition: AgentDefinitionView


AgentDefinitionResult = ProjectAgentCreateResult


class AgentService:
    """Own Project Agent definition validation and atomic replacement."""

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
    ) -> ProjectAgentCreateResult:
        if not isinstance(actor, ProjectContext) or not isinstance(session, AsyncSession) or not session.in_transaction():
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        return await self._create_project_definition(AgentRepository(session), actor, command, payload)

    async def _create_project_definition(
        self,
        repository: AgentRepository,
        actor: ProjectContext | SystemAssetGovernanceContext,
        command: CreateAgent,
        payload: AgentPayload,
    ) -> ProjectAgentCreateResult:
        command = self._validate_create(actor, command)
        payload = self._validate_payload(actor, payload)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        if not isinstance(actor, ProjectContext) and actor.project_id is None:
            raise AssetForbidden(actor.request_id)
        await require_agent_catalog_validation(
            self._catalog_validator,
            repository.session,
            request_id=actor.request_id,
            model_ref=payload.model_ref,
            tool_groups=payload.tool_groups,
        )
        await self._validate_dependency_closure(repository, actor, payload.skill_refs, payload.mcp_version_ids)
        definition_id = uuid.uuid4()
        checksum = agent_payload_checksum(payload, payload_schema_version=4)
        record = (
            await repository.create_project_asset(actor, command, payload, definition_id=definition_id, payload_checksum=checksum)
            if isinstance(actor, ProjectContext)
            else await repository.create_override_asset(actor, command, payload, definition_id=definition_id, payload_checksum=checksum)
        )
        await self._record_governance(repository.session, actor, record.row.id, "agent.create")
        return self._result(record)

    async def create_project(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        command: CreateAgent,
        payload: AgentPayload,
    ) -> ProjectAgentCreateResult:
        if not isinstance(actor, (ProjectContext, SystemAssetGovernanceContext)) or (isinstance(actor, SystemAssetGovernanceContext) and actor.project_id is None):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))

        async def operation(repository: AgentRepository) -> ProjectAgentCreateResult:
            return await self._create_project_definition(repository, actor, command, payload)

        return await self._execute(actor, operation)

    async def replace_definition(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        payload: AgentPayload,
        *,
        expected_asset_version: int,
    ) -> AgentDefinitionResult:
        payload = self._validate_payload(actor, payload)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentDefinitionResult:
            asset = await self._get_mutable_project_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            await self._validate_dependency_closure(repository, actor, payload.skill_refs, payload.mcp_version_ids)
            await require_agent_catalog_validation(
                self._catalog_validator,
                repository.session,
                request_id=actor.request_id,
                model_ref=payload.model_ref,
                tool_groups=payload.tool_groups,
            )
            record = await repository.replace_definition(
                asset,
                payload,
                definition_id=uuid.uuid4(),
                payload_checksum=agent_payload_checksum(payload, payload_schema_version=4),
                updated_by_user_id=str(actor.user_id),
            )
            return self._result(record)

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, result.asset.id, "agent.definition.update"),
        )

    async def update_instructions(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        instructions: AgentInstructions,
        *,
        expected_asset_version: int,
    ) -> AgentDefinitionResult:
        instructions = self._validate_instructions(actor, instructions)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentDefinitionResult:
            asset = await self._get_mutable_project_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            current = await repository.get_definition(asset, for_update=True)
            payload = replace(
                self._payload_from_record(current, actor.request_id),
                agents_instructions=instructions.agents_instructions,
                soul=instructions.soul,
                identity=instructions.identity,
                user_context=instructions.user_context,
            )
            payload = self._validate_payload(actor, payload)
            await self._validate_dependency_closure(repository, actor, payload.skill_refs, payload.mcp_version_ids)
            await require_agent_catalog_validation(
                self._catalog_validator,
                repository.session,
                request_id=actor.request_id,
                model_ref=payload.model_ref,
                tool_groups=payload.tool_groups,
            )
            return self._result(
                await repository.replace_definition(
                    asset,
                    payload,
                    definition_id=uuid.uuid4(),
                    payload_checksum=agent_payload_checksum(payload, payload_schema_version=4),
                    updated_by_user_id=str(actor.user_id),
                )
            )

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, result.asset.id, "agent.instructions.update"),
        )

    async def update_capability_bindings(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        bindings: AgentCapabilityBindings,
        *,
        expected_asset_version: int,
    ) -> AgentDefinitionResult:
        bindings = self._validate_capability_bindings(actor, bindings)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentDefinitionResult:
            asset = await self._get_mutable_project_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            current = await repository.get_definition(asset, for_update=True)
            payload = replace(
                self._payload_from_record(current, actor.request_id),
                skill_refs=bindings.skill_refs,
                mcp_version_ids=bindings.mcp_version_ids,
            )
            payload = self._validate_payload(actor, payload)
            await self._validate_dependency_closure(repository, actor, payload.skill_refs, payload.mcp_version_ids)
            await require_agent_catalog_validation(
                self._catalog_validator,
                repository.session,
                request_id=actor.request_id,
                model_ref=payload.model_ref,
                tool_groups=payload.tool_groups,
            )
            return self._result(
                await repository.replace_definition(
                    asset,
                    payload,
                    definition_id=uuid.uuid4(),
                    payload_checksum=agent_payload_checksum(payload, payload_schema_version=4),
                    updated_by_user_id=str(actor.user_id),
                )
            )

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(session, actor, result.asset.id, "agent.capability_bindings.update"),
        )

    async def remove_project_skill_from_definitions_in_session(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> tuple[AgentAssetView, ...]:
        """Remove one Project Skill from all Project Agent definitions atomically.

        The caller owns the surrounding deletion transaction and the shared
        Project governance fence. Agent lifecycle status is deliberately kept.
        """

        if not isinstance(session, AsyncSession) or not session.in_transaction() or not isinstance(actor, ProjectContext) or not isinstance(skill_id, uuid.UUID):
            raise AssetValidationFailed(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        repository = AgentRepository(session)
        records = await repository.lock_project_agents_referencing_skill(actor, skill_id)
        affected: list[AgentAssetView] = []
        for record in records:
            payload = self._payload_from_record(record, actor.request_id)
            updated = replace(
                payload,
                skill_refs=tuple(ref for ref in payload.skill_refs if not (ref.scope is AssetScope.PROJECT and ref.asset_id == skill_id)),
            )
            if updated.skill_refs == payload.skill_refs:
                continue
            changed = await repository.replace_definition(
                record.row,
                updated,
                definition_id=uuid.uuid4(),
                payload_checksum=agent_payload_checksum(updated, payload_schema_version=4),
                updated_by_user_id=str(actor.user_id),
            )
            affected.append(self._asset_view(changed.row))
        return tuple(affected)

    async def delete(self, actor: ProjectContext, asset_id: uuid.UUID, *, expected_asset_version: int) -> None:
        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(getattr(actor, "request_id", "unknown"))
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> bool:
            cleared_default = await repository.clear_current_project_default(actor, asset_id)
            asset = await repository.get_project_asset(actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            await repository.archive_project_asset(actor, asset)
            return cleared_default

        async def governance(session: AsyncSession, cleared_default: bool) -> None:
            if cleared_default:
                await self._record_governance(session, actor, asset_id, "agent.default.clear")
            await self._record_governance(session, actor, asset_id, "agent.delete")

        await self._execute(actor, operation, governance=governance)

    async def suspend(self, actor: ProjectContext | SystemAssetGovernanceContext, asset_id: uuid.UUID, *, expected_asset_version: int) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        return await self._change_status(actor, asset_id, expected_asset_version=expected_asset_version, source="active", target="suspended", action="agent.suspend")

    async def enable(self, actor: ProjectContext | SystemAssetGovernanceContext, asset_id: uuid.UUID, *, expected_asset_version: int) -> AgentAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: AgentRepository) -> AgentAssetView:
            asset = await self._get_mutable_project_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "suspended":
                raise AssetConflict(actor.request_id)
            record = await repository.get_definition(asset, for_update=True)
            payload = self._payload_from_record(record, actor.request_id)
            await self._validate_dependency_closure(repository, actor, payload.skill_refs, payload.mcp_version_ids, require_runnable=True)
            await require_agent_catalog_validation(self._catalog_validator, repository.session, request_id=actor.request_id, model_ref=payload.model_ref, tool_groups=payload.tool_groups)
            asset.status = "active"
            asset.revision += 1
            asset.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(actor, operation, governance=lambda session, result: self._record_governance(session, actor, result.id, "agent.enable"))

    async def _change_status(
        self,
        actor: ProjectContext | SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
        source: str,
        target: str,
        action: str,
    ) -> AgentAssetView:
        async def operation(repository: AgentRepository) -> AgentAssetView:
            await repository.ensure_not_current_project_default(actor, asset_id)
            asset = await self._get_mutable_project_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != source:
                raise AssetConflict(actor.request_id)
            asset.status = target
            asset.revision += 1
            asset.updated_by_user_id = str(actor.user_id)
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(actor, operation, governance=lambda session, result: self._record_governance(session, actor, result.id, action))

    async def get(self, actor: _Actor, asset_id: uuid.UUID) -> AgentDefinitionResult:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: AgentRepository) -> AgentDefinitionResult:
            if isinstance(actor, ProjectContext):
                asset = await repository.get_project_visible_asset(actor, asset_id)
            elif actor.project_id is not None:
                asset = await repository.get_override_asset(actor, asset_id)
            else:
                asset = await repository.get_system_asset(actor, asset_id)
            return self._result(await repository.get_definition(asset))

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
            return tuple(self._asset_view(row, description=row.description) for row in rows)

        return await self._execute(actor, operation)

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
    async def _get_mutable_project_asset(repository: AgentRepository, actor: ProjectContext | SystemAssetGovernanceContext, asset_id: uuid.UUID, *, for_update: bool) -> AgentRow:
        asset = await repository.get_project_asset(actor, asset_id, for_update=for_update) if isinstance(actor, ProjectContext) else await repository.get_override_asset(actor, asset_id, for_update=for_update)
        if asset.scope != AssetScope.PROJECT.value or asset.status not in {"active", "suspended"}:
            raise AssetConflict(actor.request_id)
        return asset

    @staticmethod
    def _validate_create(actor: _Actor, command: CreateAgent) -> CreateAgent:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateAgent):
            raise AssetValidationFailed(request_id)
        slug = command.slug.strip()
        display_name = command.display_name.strip()
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(request_id)
        return CreateAgent(slug, display_name)

    @classmethod
    def _validate_payload(cls, actor: _Actor, payload: AgentPayload) -> AgentPayload:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(payload, AgentPayload):
            raise AssetValidationFailed(request_id)
        try:
            model_settings = AgentModelSettings.model_validate(payload.model_settings)
            normalized = replace(
                payload,
                tool_groups=tuple(payload.tool_groups),
                skill_refs=tuple(payload.skill_refs),
                mcp_version_ids=tuple(payload.mcp_version_ids),
                payload_schema_version=4,
                model_settings=model_settings,
            )
        except (TypeError, ValidationError, ValueError):
            raise AssetValidationFailed(request_id) from None
        if not all(isinstance(value, str) for value in (normalized.description, normalized.agents_instructions, normalized.soul, normalized.identity, normalized.user_context, normalized.model_ref)):
            raise AssetValidationFailed(request_id)
        cls._validate_instruction_sizes(actor, AgentInstructions(normalized.agents_instructions, normalized.soul, normalized.identity, normalized.user_context))
        if normalized.model_ref != DEFAULT_MODEL_REF and exact_model_ref(normalized.model_ref) is None:
            raise AssetValidationFailed(request_id)
        if any(not isinstance(group, str) or not group.strip() for group in normalized.tool_groups) or len(set(normalized.tool_groups)) != len(normalized.tool_groups):
            raise AssetValidationFailed(request_id)
        if any(not isinstance(value, SkillAssetRef) or not isinstance(value.scope, AssetScope) or not isinstance(value.asset_id, uuid.UUID) for value in normalized.skill_refs) or len(set(normalized.skill_refs)) != len(
            normalized.skill_refs
        ):
            raise AssetValidationFailed(request_id)
        if any(not isinstance(value, uuid.UUID) for value in normalized.mcp_version_ids) or len(set(normalized.mcp_version_ids)) != len(normalized.mcp_version_ids):
            raise AssetValidationFailed(request_id)
        return normalized

    @staticmethod
    def _validate_instructions(actor: _Actor, instructions: AgentInstructions) -> AgentInstructions:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(instructions, AgentInstructions) or not all(isinstance(value, str) for value in (instructions.agents_instructions, instructions.soul, instructions.identity, instructions.user_context)):
            raise AssetValidationFailed(request_id)
        AgentService._validate_instruction_sizes(actor, instructions)
        return instructions

    @staticmethod
    def _validate_capability_bindings(actor: _Actor, bindings: AgentCapabilityBindings) -> AgentCapabilityBindings:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(bindings, AgentCapabilityBindings):
            raise AssetValidationFailed(request_id)
        try:
            skill_refs = tuple(bindings.skill_refs)
            mcp_ids = tuple(bindings.mcp_version_ids)
        except TypeError:
            raise AssetValidationFailed(request_id) from None
        if (
            any(not isinstance(value, SkillAssetRef) or not isinstance(value.scope, AssetScope) or not isinstance(value.asset_id, uuid.UUID) for value in skill_refs)
            or len(set(skill_refs)) != len(skill_refs)
            or any(not isinstance(value, uuid.UUID) for value in mcp_ids)
            or len(set(mcp_ids)) != len(mcp_ids)
        ):
            raise AssetValidationFailed(request_id)
        return AgentCapabilityBindings(skill_refs, mcp_ids)

    @staticmethod
    def _validate_instruction_sizes(actor: _Actor, instructions: AgentInstructions) -> None:
        sizes = tuple(len(value.encode("utf-8")) for value in (instructions.agents_instructions, instructions.soul, instructions.identity, instructions.user_context))
        if any(size > MAX_AGENT_INSTRUCTION_FIELD_BYTES for size in sizes) or sum(sizes) > MAX_AGENT_INSTRUCTIONS_TOTAL_BYTES:
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
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _require_expected_version(actor: _Actor, asset: AgentRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or asset.revision != expected:
            raise AssetConflict(actor.request_id)

    async def _validate_dependency_closure(
        self,
        repository: AgentRepository,
        actor: ProjectContext | SystemAssetGovernanceContext,
        skill_refs: Sequence[SkillAssetRef],
        mcp_version_ids: Sequence[uuid.UUID],
        *,
        require_runnable: bool = False,
    ) -> None:
        if isinstance(actor, ProjectContext):
            resolved_skill_refs = await repository.resolve_project_skill_refs(actor, skill_refs, require_runnable=require_runnable)
            resolved_mcp_ids = await repository.resolve_project_mcp_versions(actor, mcp_version_ids)
        elif actor.project_id is not None:
            resolved_skill_refs = await repository.resolve_override_skill_refs(actor, skill_refs, require_runnable=require_runnable)
            resolved_mcp_ids = await repository.resolve_override_mcp_versions(actor, mcp_version_ids)
        else:
            resolved_skill_refs = await repository.resolve_system_skill_refs(actor, skill_refs, require_runnable=require_runnable)
            resolved_mcp_ids = await repository.resolve_system_mcp_versions(actor, mcp_version_ids)
        if set(resolved_skill_refs) != set(skill_refs) or set(resolved_mcp_ids) != set(mcp_version_ids):
            raise AssetValidationFailed(actor.request_id)
        slugs = await repository.lock_skill_asset_slugs(skill_refs)
        if len(slugs) != len(skill_refs) or len({slug.casefold() for slug in slugs}) != len(slugs):
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _payload_from_record(record: AgentDefinitionRecord, request_id: str) -> AgentPayload:
        row = record.row
        try:
            payload = AgentPayload(
                description=row.description,
                agents_instructions=row.agents_instructions,
                soul=row.soul,
                identity=row.identity,
                user_context=row.user_context,
                model_ref=row.model_ref,
                model_settings=AgentModelSettings.model_validate(row.model_settings),
                tool_groups=tuple(row.tool_groups),
                skill_refs=record.skill_refs,
                mcp_version_ids=record.mcp_version_ids,
                payload_schema_version=4,
            )
        except (TypeError, ValidationError, ValueError):
            raise AssetValidationFailed(request_id) from None
        if not agent_payload_checksum_matches(payload, row.payload_checksum, payload_schema_version=4):
            raise AssetValidationFailed(request_id)
        return payload

    @staticmethod
    def _asset_view(row: AgentRow, *, description: str | None = None) -> AgentAssetView:
        return AgentAssetView(
            id=row.id,
            scope=AssetScope(row.scope),
            project_id=row.project_id,
            slug=row.slug,
            display_name=row.display_name,
            status=row.status,
            definition_id=row.definition_id,
            revision=row.revision,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            description=row.description if description is None else description,
        )

    @staticmethod
    def _definition_view(record: AgentDefinitionRecord) -> AgentDefinitionView:
        row = record.row
        return AgentDefinitionView(
            definition_id=row.definition_id,
            agent_id=row.id,
            description=row.description,
            agents_instructions=row.agents_instructions,
            soul=row.soul,
            identity=row.identity,
            user_context=row.user_context,
            model_ref=row.model_ref,
            model_settings=AgentModelSettings.model_validate(row.model_settings),
            tool_groups=tuple(row.tool_groups),
            skill_refs=record.skill_refs,
            mcp_version_ids=record.mcp_version_ids,
            payload_schema_version=row.payload_schema_version,
            payload_checksum=row.payload_checksum,
            updated_by_user_id=row.updated_by_user_id,
            updated_at=row.updated_at,
        )

    @classmethod
    def _result(cls, record: AgentDefinitionRecord) -> AgentDefinitionResult:
        return AgentDefinitionResult(asset=cls._asset_view(record.row), definition=cls._definition_view(record))

    async def _record_governance(self, session: AsyncSession, actor: ProjectContext | SystemAssetGovernanceContext, asset_id: uuid.UUID, action: str) -> None:
        if isinstance(actor, ProjectContext):
            await self._governance_sink.append_project(session, actor=actor.user_id, project_id=actor.project_id, asset_id=asset_id, version_id=None, action=action, request_id=actor.request_id, asset_kind="agent")
        elif isinstance(actor, SystemAssetGovernanceContext):
            await self._governance_sink.append_override(session, actor=actor.user_id, project_id=actor.project_id, asset_id=asset_id, version_id=None, action=action, request_id=actor.request_id, asset_kind="agent")


__all__ = [
    "AGENT_INSTRUCTION_FIELDS",
    "AgentAssetView",
    "AgentCapabilityBindings",
    "AgentDefinitionResult",
    "AgentDefinitionView",
    "AgentInstructions",
    "AgentService",
    "CreateAgent",
    "ProjectAgentCreateResult",
]
