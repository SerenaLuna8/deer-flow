"""Validation helpers for project-scoped Skill Builder sessions."""

from __future__ import annotations

import re
import uuid

from pydantic import ValidationError

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_profile import agent_design_mode_matches_profile
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetValidationFailed,
    SkillDesignTargetDeleted,
)
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_design_codec import _clarification_from_json, _jsonable
from app.shared_assets.skill_design_contracts import (
    CancelSkillDesignSession,
    CommitSkillDesignSession,
    CreateSkillDesignRevisionSession,
    CreateSkillDesignSession,
    SetSkillDesignExecutionPreference,
    SkillDesignClarificationResponse,
    SkillDesignClarificationTurn,
    SkillDesignDraftUpdateTurn,
    SkillDesignMessageTurn,
    SkillDesignStatus,
    SkillDesignTurn,
    SkillDesignTurnAttachment,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
from app.shared_assets.skill_design_generation import (
    MAX_SKILL_DESIGN_ATTACHMENTS,
    MAX_SKILL_DESIGN_ATTACHMENTS_TOTAL_BYTES,
    SKILL_DESIGN_REASONING_EFFORTS,
    CandidateResult,
    SkillDesignAttachment,
    contains_secret_like_material,
)
from app.shared_assets.skill_package_integrity import SkillArchivePreview
from app.system_settings.model_refs import exact_model_ref
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDEMPOTENCY_KEY_CHARS = 255
_MAX_MESSAGE_CHARS = 8_000
_MAX_SESSION_MESSAGES = 128
_MAX_DISPLAY_NAME_CHARS = 120
_MAX_BUILDER_FILES = 128
_MAX_BUILDER_FILE_BYTES = 512 * 1024
_MAX_BUILDER_TOTAL_BYTES = 2 * 1024 * 1024


def _validate_create(
    context: ProjectContext,
    command: CreateSkillDesignSession,
) -> CreateSkillDesignSession:
    _require_context(context)
    if not isinstance(command, CreateSkillDesignSession):
        raise AssetValidationFailed(context.request_id)
    slug = command.slug.strip() if isinstance(command.slug, str) else ""
    display_name = command.display_name.strip() if isinstance(command.display_name, str) else ""
    if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > _MAX_DISPLAY_NAME_CHARS or contains_secret_like_material(display_name):
        raise AssetValidationFailed(context.request_id)
    idempotency_key = _validate_idempotency_key(
        context,
        command.idempotency_key,
    )
    return CreateSkillDesignSession(
        slug=slug,
        display_name=display_name,
        idempotency_key=idempotency_key,
    )


def _validate_create_revision(
    context: ProjectContext,
    command: CreateSkillDesignRevisionSession,
) -> CreateSkillDesignRevisionSession:
    _require_context(context)
    if not isinstance(command, CreateSkillDesignRevisionSession):
        raise AssetValidationFailed(context.request_id)
    return CreateSkillDesignRevisionSession(
        skill_id=_validate_uuid(context, command.skill_id),
        idempotency_key=_validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
    )


def _validate_turn(
    context: ProjectContext,
    command: SubmitSkillDesignTurn,
) -> SubmitSkillDesignTurn:
    _require_context(context)
    if not isinstance(command, SubmitSkillDesignTurn) or not _valid_revision(command.expected_revision):
        raise AssetValidationFailed(context.request_id)
    key = _validate_idempotency_key(
        context,
        command.idempotency_key,
    )
    turn = command.input
    if isinstance(turn, SkillDesignMessageTurn):
        if turn.kind != "message":
            raise AssetValidationFailed(context.request_id)
        message = _bounded_text(
            context,
            turn.message,
            max_chars=_MAX_MESSAGE_CHARS,
        )
        if contains_secret_like_material(message):
            raise AssetValidationFailed(context.request_id)
        normalized: SkillDesignTurn = SkillDesignMessageTurn(
            kind="message",
            message=message,
            model_name=_validate_turn_model_name(
                context,
                turn.model_name,
            ),
            mode=turn.mode,
            thinking_enabled=turn.thinking_enabled,
            reasoning_effort=_validate_turn_reasoning_effort(
                context,
                turn.reasoning_effort,
            ),
            attachments=_validate_turn_attachments(
                context,
                turn.attachments,
            ),
        )
    elif isinstance(turn, SkillDesignClarificationTurn):
        response = turn.response
        if turn.kind != "clarification" or not isinstance(response, SkillDesignClarificationResponse):
            raise AssetValidationFailed(context.request_id)
        value = _bounded_text(
            context,
            response.value,
            max_chars=_MAX_MESSAGE_CHARS,
        )
        if contains_secret_like_material(value):
            raise AssetValidationFailed(context.request_id)
        normalized = SkillDesignClarificationTurn(
            kind="clarification",
            response=SkillDesignClarificationResponse(
                version=response.version,
                kind=response.kind,
                source=response.source,
                request_id=response.request_id,
                response_kind=response.response_kind,
                value=value,
                option_id=response.option_id,
            ),
            model_name=_validate_turn_model_name(
                context,
                turn.model_name,
            ),
            mode=turn.mode,
            thinking_enabled=turn.thinking_enabled,
            reasoning_effort=_validate_turn_reasoning_effort(
                context,
                turn.reasoning_effort,
            ),
        )
    elif isinstance(turn, SkillDesignDraftUpdateTurn):
        if turn.kind != "draft_update" or _CHECKSUM_PATTERN.fullmatch(turn.expected_draft_checksum) is None:
            raise AssetValidationFailed(context.request_id)
        try:
            changes = tuple(turn.changes)
        except TypeError:
            raise AssetValidationFailed(context.request_id) from None
        if not changes:
            raise AssetValidationFailed(context.request_id)
        if contains_secret_like_material(_jsonable(changes)):
            raise AssetValidationFailed(context.request_id)
        normalized = SkillDesignDraftUpdateTurn(
            kind="draft_update",
            expected_draft_checksum=turn.expected_draft_checksum,
            changes=changes,
        )
    else:
        raise AssetValidationFailed(context.request_id)
    if isinstance(
        normalized,
        SkillDesignMessageTurn | SkillDesignClarificationTurn,
    ) and (normalized.mode is not None or normalized.thinking_enabled is not None):
        if normalized.model_name is None or normalized.mode is None:
            raise AssetValidationFailed(context.request_id)
        _validate_execution_preference(
            context,
            SetSkillDesignExecutionPreference(
                model_name=normalized.model_name,
                mode=normalized.mode,
                thinking_enabled=normalized.thinking_enabled,
                reasoning_effort=normalized.reasoning_effort,
            ),
        )
    return SubmitSkillDesignTurn(
        input=normalized,
        expected_revision=command.expected_revision,
        idempotency_key=key,
    )


def _validate_execution_preference(
    context: ProjectContext,
    command: SetSkillDesignExecutionPreference,
) -> SetSkillDesignExecutionPreference:
    _require_context(context)
    if not isinstance(command, SetSkillDesignExecutionPreference):
        raise AssetValidationFailed(context.request_id)
    model_name = _validate_turn_model_name(
        context,
        command.model_name,
    )
    if model_name is None:
        raise AssetValidationFailed(context.request_id)
    if command.mode not in {"flash", "thinking", "pro", "ultra"}:
        raise AssetValidationFailed(context.request_id)
    if type(command.thinking_enabled) is not bool:
        raise AssetValidationFailed(context.request_id)
    effort = _validate_turn_reasoning_effort(
        context,
        command.reasoning_effort,
    )
    if not agent_design_mode_matches_profile(
        command.mode,
        thinking_enabled=command.thinking_enabled,
        reasoning_effort=effort,
    ):
        raise AssetValidationFailed(context.request_id)
    return SetSkillDesignExecutionPreference(
        model_name=model_name,
        mode=command.mode,
        thinking_enabled=command.thinking_enabled,
        reasoning_effort=effort,
    )


def _validate_turn_model_name(
    context: ProjectContext,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    if exact_model_ref(value) is None:
        raise AssetValidationFailed(context.request_id)
    return value


def _validate_turn_reasoning_effort(
    context: ProjectContext,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SKILL_DESIGN_REASONING_EFFORTS:
        raise AssetValidationFailed(context.request_id)
    return value


def _validate_turn_attachments(
    context: ProjectContext,
    attachments: object,
) -> tuple[SkillDesignTurnAttachment, ...]:
    try:
        snapshot = tuple(attachments)  # type: ignore[call-overload]
    except TypeError:
        raise AssetValidationFailed(context.request_id) from None
    if not snapshot:
        return ()
    if len(snapshot) > MAX_SKILL_DESIGN_ATTACHMENTS:
        raise AssetValidationFailed(context.request_id)
    normalized: list[SkillDesignTurnAttachment] = []
    names: set[str] = set()
    total = 0
    for item in snapshot:
        if not isinstance(item, SkillDesignTurnAttachment):
            raise AssetValidationFailed(context.request_id)
        try:
            # The generation contract model owns name/content shape rules.
            checked = SkillDesignAttachment(
                name=item.name,
                content=item.content,
            )
        except ValidationError:
            raise AssetValidationFailed(context.request_id) from None
        if checked.name in names:
            raise AssetValidationFailed(context.request_id)
        names.add(checked.name)
        total += len(checked.content.encode("utf-8"))
        if total > MAX_SKILL_DESIGN_ATTACHMENTS_TOTAL_BYTES:
            raise AssetValidationFailed(context.request_id)
        if contains_secret_like_material(checked.name) or contains_secret_like_material(checked.content):
            raise AssetValidationFailed(context.request_id)
        normalized.append(
            SkillDesignTurnAttachment(
                name=checked.name,
                content=checked.content,
            )
        )
    return tuple(normalized)


def _validate_validation(
    context: ProjectContext,
    command: ValidateSkillDesignSession,
) -> ValidateSkillDesignSession:
    if not isinstance(command, ValidateSkillDesignSession) or not _valid_revision(command.expected_revision) or not isinstance(command.expected_draft_checksum, str) or _CHECKSUM_PATTERN.fullmatch(command.expected_draft_checksum) is None:
        raise AssetValidationFailed(context.request_id)
    return ValidateSkillDesignSession(
        expected_revision=command.expected_revision,
        expected_draft_checksum=command.expected_draft_checksum,
        idempotency_key=_validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
    )


def _validate_commit(
    context: ProjectContext,
    command: CommitSkillDesignSession,
) -> CommitSkillDesignSession:
    if not isinstance(command, CommitSkillDesignSession) or not _valid_revision(command.expected_revision) or not isinstance(command.expected_draft_checksum, str) or _CHECKSUM_PATTERN.fullmatch(command.expected_draft_checksum) is None:
        raise AssetValidationFailed(context.request_id)
    return CommitSkillDesignSession(
        expected_revision=command.expected_revision,
        expected_draft_checksum=command.expected_draft_checksum,
        idempotency_key=_validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
    )


def _validate_cancel(
    context: ProjectContext,
    command: CancelSkillDesignSession,
) -> CancelSkillDesignSession:
    if not isinstance(command, CancelSkillDesignSession) or not _valid_revision(command.expected_revision):
        raise AssetValidationFailed(context.request_id)
    return CancelSkillDesignSession(
        expected_revision=command.expected_revision,
        idempotency_key=_validate_idempotency_key(
            context,
            command.idempotency_key,
        ),
    )


def _require_context(context: ProjectContext) -> None:
    if not isinstance(context, ProjectContext):
        raise AssetForbidden(getattr(context, "request_id", "unknown"))


def _require_capability(
    context: ProjectContext,
    capability: Capability,
) -> None:
    _require_context(context)
    if capability not in context.capabilities:
        raise AssetForbidden(context.request_id)


def _require_nonterminal(
    context: ProjectContext,
    row: SkillDesignSessionRow,
) -> None:
    if row.status in {
        SkillDesignStatus.COMPLETED.value,
        SkillDesignStatus.CANCELLED.value,
        SkillDesignStatus.COMMITTING.value,
    }:
        raise AssetConflict(context.request_id)


def _require_revise_target_live(
    context: ProjectContext,
    row: SkillDesignSessionRow,
) -> None:
    """A revise session whose target Skill was deleted is terminally dead."""

    if row.session_kind == "revise" and (row.target_skill_deleted or row.target_skill_id is None):
        raise SkillDesignTargetDeleted(context.request_id)


def _require_expected_revision(
    context: ProjectContext,
    row: SkillDesignSessionRow,
    expected: int,
) -> None:
    if row.revision != expected:
        raise AssetConflict(context.request_id)


def _require_matching_operation(
    context: ProjectContext,
    operation: SkillDesignOperationRow,
    *,
    session_id: uuid.UUID,
    request_checksum: str,
) -> None:
    if operation.session_id != session_id or operation.request_checksum != request_checksum:
        raise AssetConflict(context.request_id)


def _require_message_capacity(
    context: ProjectContext,
    row: SkillDesignSessionRow,
    *,
    additional: int,
) -> None:
    messages = row.messages_json
    if not isinstance(messages, list) or not isinstance(additional, int) or isinstance(additional, bool) or additional < 1 or len(messages) + additional > _MAX_SESSION_MESSAGES:
        raise AssetValidationFailed(context.request_id)


def _require_matching_clarification_response(
    context: ProjectContext,
    row: SkillDesignSessionRow,
    response: SkillDesignClarificationResponse,
) -> None:
    if row.status != SkillDesignStatus.AWAITING_CLARIFICATION.value or row.active_clarification_json is None:
        raise AssetConflict(context.request_id)
    request = _clarification_from_json(
        context,
        row.active_clarification_json,
    )
    if response.version != request.version or response.kind != "human_input_response" or response.source != request.source or response.request_id != request.request_id or response.response_kind not in {"option", "text"}:
        raise AssetConflict(context.request_id)
    if response.response_kind == "option":
        selected = next(
            (item for item in request.options if item.id == response.option_id),
            None,
        )
        if selected is None or selected.value != response.value:
            raise AssetConflict(context.request_id)
    elif response.option_id is not None:
        raise AssetConflict(context.request_id)


def _candidate_files(
    context: ProjectContext,
    result: CandidateResult,
) -> tuple[SkillArchiveFile, ...]:
    if contains_secret_like_material(result.model_dump(mode="json")):
        raise AssetValidationFailed(context.request_id)
    files = tuple(
        SkillArchiveFile(
            path=item.path,
            content=item.content.encode("utf-8"),
            media_type=item.media_type,
        )
        for item in result.files
    )
    return _validate_builder_files(context, files)


def _validate_builder_files(
    context: ProjectContext,
    files: tuple[SkillArchiveFile, ...],
    *,
    allow_empty: bool = False,
    require_skill_md: bool = True,
) -> tuple[SkillArchiveFile, ...]:
    try:
        snapshot = tuple(files)
    except TypeError:
        raise AssetValidationFailed(context.request_id) from None
    if not snapshot:
        if allow_empty:
            return ()
        raise AssetValidationFailed(context.request_id)
    if len(snapshot) > _MAX_BUILDER_FILES:
        raise AssetValidationFailed(context.request_id)
    total = 0
    paths: set[str] = set()
    for item in snapshot:
        if not isinstance(item, SkillArchiveFile):
            raise AssetValidationFailed(context.request_id)
        if item.path in paths or len(item.content) > _MAX_BUILDER_FILE_BYTES:
            raise AssetValidationFailed(context.request_id)
        paths.add(item.path)
        total += len(item.content)
        if total > _MAX_BUILDER_TOTAL_BYTES:
            raise AssetValidationFailed(context.request_id)
        try:
            decoded = item.content.decode("utf-8")
        except UnicodeDecodeError:
            raise AssetValidationFailed(context.request_id) from None
        if "\x00" in decoded or contains_secret_like_material(decoded):
            raise AssetValidationFailed(context.request_id)
    if require_skill_md and "SKILL.md" not in paths:
        raise AssetValidationFailed(context.request_id)
    return tuple(sorted(snapshot, key=lambda item: item.path))


def _validate_partial_builder_files(
    context: ProjectContext,
    files: tuple[SkillArchiveFile, ...],
) -> tuple[SkillArchiveFile, ...]:
    """Validate a persisted in-progress draft without requiring completion."""

    return _validate_builder_files(
        context,
        files,
        allow_empty=True,
        require_skill_md=False,
    )


def _require_preview_name(
    context: ProjectContext,
    preview: SkillArchivePreview,
    expected_slug: str,
) -> None:
    name = preview.frontmatter.get("name")
    if not isinstance(name, str) or name != expected_slug:
        raise AssetValidationFailed(context.request_id)


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
    if not normalized or len(normalized) > _MAX_IDEMPOTENCY_KEY_CHARS or "\x00" in normalized:
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
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise AssetValidationFailed(context.request_id)
    return normalized
