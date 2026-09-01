"""Validation helpers for project-scoped Agent Builder sessions."""

from __future__ import annotations

import re
import uuid

from pydantic import ValidationError

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_contracts import (
    AgentDesignBlueprint,
    AgentDesignBlueprintTurn,
    AgentDesignClarificationResponse,
    AgentDesignClarificationTurn,
    AgentDesignMessageTurn,
    AgentDesignStatus,
    AgentDesignTurn,
    CancelAgentDesignSession,
    CommitAgentDesignSession,
    CreateAgentDesignSession,
    SetAgentDesignGenerationPreference,
    SubmitAgentDesignTurn,
)
from app.shared_assets.agent_design_generation import AgentDesignDraft, CandidateResult
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetValidationFailed,
)
from app.shared_assets.models import AgentModelSettings, AssetScope, SkillAssetRef
from app.system_settings.model_refs import (
    DEFAULT_MODEL_REF as DEFAULT_AGENT_MODEL_REF,
)
from app.system_settings.model_refs import exact_model_ref
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)

AGENT_DESIGN_SLUG_MIN_LENGTH = 3
AGENT_DESIGN_SLUG_MAX_LENGTH = 63
AGENT_DESIGN_SLUG_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_SLUG_PATTERN = re.compile(AGENT_DESIGN_SLUG_PATTERN)
_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_IDEMPOTENCY_KEY_CHARS = 255
_MAX_MESSAGE_CHARS = 4_000
_MAX_DESCRIPTION_CHARS = 4_000
_MAX_TOOL_GROUPS = 50


def _valid_agent_design_slug(value: str) -> bool:
    return AGENT_DESIGN_SLUG_MIN_LENGTH <= len(value) <= AGENT_DESIGN_SLUG_MAX_LENGTH and _SLUG_PATTERN.fullmatch(value) is not None


def _validate_create(
    cls,
    context: ProjectContext,
    command: CreateAgentDesignSession,
) -> CreateAgentDesignSession:
    cls._require_context(context)
    if not isinstance(command, CreateAgentDesignSession):
        raise AssetValidationFailed(context.request_id)
    if not all(
        isinstance(item, str)
        for item in (
            command.slug,
            command.display_name,
            command.idempotency_key,
        )
    ):
        raise AssetValidationFailed(context.request_id)
    slug = command.slug.strip()
    display_name = command.display_name.strip()
    idempotency_key = cls._validate_idempotency_key(
        context,
        command.idempotency_key,
    )
    if not _valid_agent_design_slug(slug) or not display_name or len(display_name) > 120:
        raise AssetValidationFailed(context.request_id)
    return CreateAgentDesignSession(
        slug=slug,
        display_name=display_name,
        idempotency_key=idempotency_key,
    )


def _validate_turn(
    cls,
    context: ProjectContext,
    command: SubmitAgentDesignTurn,
) -> SubmitAgentDesignTurn:
    cls._require_context(context)
    if not isinstance(command, SubmitAgentDesignTurn) or not cls._valid_revision(command.expected_revision):
        raise AssetValidationFailed(context.request_id)
    idempotency_key = cls._validate_idempotency_key(
        context,
        command.idempotency_key,
    )
    generation_model_ref = command.generation_model_ref
    if generation_model_ref is not None and generation_model_ref != DEFAULT_AGENT_MODEL_REF and exact_model_ref(generation_model_ref) is None:
        raise AssetValidationFailed(context.request_id)
    generation_mode = command.generation_mode
    thinking_enabled = command.thinking_enabled
    reasoning_effort = command.reasoning_effort
    profile_values = (
        generation_mode,
        thinking_enabled,
        reasoning_effort,
    )
    if any(value is not None for value in profile_values):
        if generation_model_ref is None or generation_mode not in {"flash", "thinking", "pro", "ultra"} or type(thinking_enabled) is not bool or reasoning_effort not in {None, "none", "low", "medium", "high"}:
            raise AssetValidationFailed(context.request_id)
    turn = command.input
    if isinstance(turn, AgentDesignMessageTurn):
        if turn.kind != "message":
            raise AssetValidationFailed(context.request_id)
        message = cls._bounded_text(
            context,
            turn.message,
            max_chars=_MAX_MESSAGE_CHARS,
        )
        normalized: AgentDesignTurn = AgentDesignMessageTurn(
            kind="message",
            message=message,
        )
    elif isinstance(turn, AgentDesignClarificationTurn):
        response = turn.response
        if turn.kind != "clarification" or not isinstance(response, AgentDesignClarificationResponse) or response.version != 1 or response.kind != "human_input_response" or response.response_kind not in {"option", "text"}:
            raise AssetValidationFailed(context.request_id)
        source = cls._bounded_text(context, response.source, max_chars=64)
        request_id = cls._bounded_text(
            context,
            response.request_id,
            max_chars=128,
        )
        value = cls._bounded_text(
            context,
            response.value,
            max_chars=2_000,
        )
        option_id = response.option_id
        if option_id is not None:
            option_id = cls._bounded_text(
                context,
                option_id,
                max_chars=128,
            )
        if response.response_kind == "option" and option_id is None:
            raise AssetValidationFailed(context.request_id)
        normalized = AgentDesignClarificationTurn(
            kind="clarification",
            response=AgentDesignClarificationResponse(
                version=1,
                kind="human_input_response",
                source=source,
                request_id=request_id,
                response_kind=response.response_kind,
                value=value,
                option_id=option_id,
            ),
        )
    elif isinstance(turn, AgentDesignBlueprintTurn):
        if turn.kind != "blueprint_update":
            raise AssetValidationFailed(context.request_id)
        normalized = AgentDesignBlueprintTurn(
            kind="blueprint_update",
            blueprint=cls._validate_blueprint(
                context,
                turn.blueprint,
            ),
        )
    else:
        raise AssetValidationFailed(context.request_id)
    return SubmitAgentDesignTurn(
        input=normalized,
        expected_revision=command.expected_revision,
        idempotency_key=idempotency_key,
        generation_model_ref=generation_model_ref,
        generation_mode=generation_mode,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
    )


def _validate_generation_preference(
    cls,
    context: ProjectContext,
    command: SetAgentDesignGenerationPreference,
) -> SetAgentDesignGenerationPreference:
    cls._require_context(context)
    if not isinstance(command, SetAgentDesignGenerationPreference):
        raise AssetValidationFailed(context.request_id)
    model_ref = command.generation_model_ref
    mode = command.generation_mode
    thinking_enabled = command.thinking_enabled
    reasoning_effort = command.reasoning_effort
    if (
        not isinstance(model_ref, str)
        or (model_ref != DEFAULT_AGENT_MODEL_REF and exact_model_ref(model_ref) is None)
        or mode not in {"flash", "thinking", "pro", "ultra"}
        or type(thinking_enabled) is not bool
        or reasoning_effort not in {None, "none", "low", "medium", "high"}
    ):
        raise AssetValidationFailed(context.request_id)
    return SetAgentDesignGenerationPreference(
        generation_model_ref=model_ref,
        generation_mode=mode,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
    )


def _validate_commit(
    cls,
    context: ProjectContext,
    command: CommitAgentDesignSession,
) -> CommitAgentDesignSession:
    cls._require_context(context)
    if (
        not isinstance(command, CommitAgentDesignSession)
        or not cls._valid_revision(command.expected_revision)
        or not isinstance(command.expected_blueprint_checksum, str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            command.expected_blueprint_checksum,
        )
        is None
    ):
        raise AssetValidationFailed(context.request_id)
    slug = command.slug
    if slug is not None:
        if not isinstance(slug, str):
            raise AssetValidationFailed(context.request_id)
        slug = slug.strip()
        if not _valid_agent_design_slug(slug):
            raise AssetValidationFailed(context.request_id)
    return CommitAgentDesignSession(
        expected_revision=command.expected_revision,
        expected_blueprint_checksum=command.expected_blueprint_checksum,
        idempotency_key=cls._validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
        slug=slug,
    )


def _validate_cancel(
    cls,
    context: ProjectContext,
    command: CancelAgentDesignSession,
) -> CancelAgentDesignSession:
    cls._require_context(context)
    if not isinstance(command, CancelAgentDesignSession) or not cls._valid_revision(command.expected_revision):
        raise AssetValidationFailed(context.request_id)
    return CancelAgentDesignSession(
        expected_revision=command.expected_revision,
        idempotency_key=cls._validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
    )


def _validate_blueprint(
    context: ProjectContext,
    blueprint: AgentDesignBlueprint,
) -> AgentDesignBlueprint:
    if not isinstance(blueprint, AgentDesignBlueprint):
        raise AssetValidationFailed(context.request_id)
    if not isinstance(blueprint.model_settings, AgentModelSettings):
        raise AssetValidationFailed(context.request_id)
    try:
        model_settings = AgentModelSettings.model_validate(
            blueprint.model_settings,
        )
    except ValidationError:
        raise AssetValidationFailed(context.request_id) from None
    if not all(
        isinstance(item, str)
        for item in (
            blueprint.description,
            blueprint.model_ref,
            blueprint.agents_instructions,
            blueprint.soul,
            blueprint.identity,
            blueprint.user_context,
        )
    ):
        raise AssetValidationFailed(context.request_id)
    description = blueprint.description.strip()
    model_ref = blueprint.model_ref.strip()
    try:
        tool_groups = tuple(blueprint.tool_groups)
        skill_refs = tuple(blueprint.skill_refs)
        mcp_version_ids = tuple(blueprint.mcp_version_ids)
    except TypeError:
        raise AssetValidationFailed(context.request_id) from None
    if (
        not description
        or len(description) > _MAX_DESCRIPTION_CHARS
        or not model_ref
        or (model_ref != DEFAULT_AGENT_MODEL_REF and exact_model_ref(model_ref) is None)
        or not tool_groups
        or len(tool_groups) > _MAX_TOOL_GROUPS
        or any(not isinstance(group, str) or _CAPABILITY_PATTERN.fullmatch(group) is None for group in tool_groups)
        or len(set(tool_groups)) != len(tool_groups)
    ):
        raise AssetValidationFailed(context.request_id)
    if (
        any(not isinstance(value, SkillAssetRef) or not isinstance(value.scope, AssetScope) or not isinstance(value.asset_id, uuid.UUID) for value in skill_refs)
        or len(set(skill_refs)) != len(skill_refs)
        or any(not isinstance(value, uuid.UUID) for value in mcp_version_ids)
        or len(set(mcp_version_ids)) != len(mcp_version_ids)
    ):
        raise AssetValidationFailed(context.request_id)
    try:
        documents = AgentDesignDraft(
            agents_instructions=blueprint.agents_instructions,
            soul=blueprint.soul,
            identity=blueprint.identity,
            user_context=blueprint.user_context,
        )
    except Exception:
        raise AssetValidationFailed(context.request_id) from None
    if any(
        not getattr(documents, field).strip()
        for field in (
            "agents_instructions",
            "soul",
            "identity",
            "user_context",
        )
    ):
        raise AssetValidationFailed(context.request_id)
    return AgentDesignBlueprint(
        description=description,
        model_ref=model_ref,
        tool_groups=tool_groups,
        skill_refs=skill_refs,
        mcp_version_ids=mcp_version_ids,
        agents_instructions=documents.agents_instructions,
        soul=documents.soul,
        identity=documents.identity,
        user_context=documents.user_context,
        model_settings=model_settings,
    )


def _candidate_blueprint(
    cls,
    context: ProjectContext,
    current: AgentDesignBlueprint,
    result: CandidateResult,
) -> AgentDesignBlueprint:
    return cls._validate_blueprint(
        context,
        AgentDesignBlueprint(
            description=result.description,
            model_ref=current.model_ref,
            tool_groups=current.tool_groups,
            skill_refs=current.skill_refs,
            mcp_version_ids=current.mcp_version_ids,
            agents_instructions=result.documents.agents_instructions,
            soul=result.documents.soul,
            identity=result.documents.identity,
            user_context=result.documents.user_context,
            model_settings=current.model_settings,
        ),
    )


def _require_context(context: ProjectContext) -> None:
    if not isinstance(context, ProjectContext):
        raise AssetForbidden(getattr(context, "request_id", "unknown"))


def _require_capability(
    cls,
    context: ProjectContext,
    capability: Capability,
) -> None:
    cls._require_context(context)
    if capability not in context.capabilities:
        raise AssetForbidden(context.request_id)


def _require_nonterminal(
    context: ProjectContext,
    row: AgentDesignSessionRow,
) -> None:
    if row.status in {
        AgentDesignStatus.COMMITTING.value,
        AgentDesignStatus.COMPLETED.value,
        AgentDesignStatus.CANCELLED.value,
    }:
        raise AssetConflict(context.request_id)


def _require_expected_revision(
    context: ProjectContext,
    row: AgentDesignSessionRow,
    expected: int,
) -> None:
    if row.revision != expected:
        raise AssetConflict(context.request_id)


def _require_matching_operation(
    context: ProjectContext,
    operation: AgentDesignOperationRow,
    *,
    session_id: uuid.UUID,
    request_checksum: str,
) -> None:
    if operation.session_id != session_id or operation.request_checksum != request_checksum:
        raise AssetConflict(context.request_id)


def _valid_revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _validate_uuid(
    context: ProjectContext,
    value: object,
) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise AssetValidationFailed(context.request_id)
    return value


def _validate_idempotency_key(
    context: ProjectContext,
    value: object,
) -> str:
    if not isinstance(value, str):
        raise AssetValidationFailed(context.request_id)
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise AssetValidationFailed(context.request_id)
    return normalized


def _bounded_text(
    context: ProjectContext,
    value: object,
    *,
    max_chars: int,
) -> str:
    if not isinstance(value, str):
        raise AssetValidationFailed(context.request_id)
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        raise AssetValidationFailed(context.request_id)
    return normalized
