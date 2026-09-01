"""Immutable public contracts for the Project Skill Builder design flow."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.shared_assets.skill_builder_admission_contract import SkillBuilderRunAdmission
from app.shared_assets.skill_design_generation import SkillBuilderDependencySnapshot
from app.shared_assets.skill_package_integrity import SkillFileChange
from app.shared_assets.skill_service import SkillAssetView, SkillVersionView


class SkillDesignStatus(StrEnum):
    INTERVIEWING = "interviewing"
    GENERATING = "generating"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    DRAFT_READY = "draft_ready"
    VALIDATED = "validated"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SkillDesignProgressStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SkillDesignServiceErrorCode(StrEnum):
    GENERATION_INTERRUPTED = "SKILL_DESIGN_GENERATION_INTERRUPTED"
    GENERATION_UNAVAILABLE = "SKILL_DESIGN_GENERATION_UNAVAILABLE"
    INVALID_MODEL_OUTPUT = "SKILL_DESIGN_INVALID_MODEL_OUTPUT"
    COMMIT_INTERRUPTED = "SKILL_DESIGN_COMMIT_INTERRUPTED"


@dataclass(frozen=True, slots=True)
class CreateSkillDesignSession:
    slug: str
    display_name: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateSkillDesignRevisionSession:
    """Open a Builder session seeded from an existing Skill's latest head."""

    skill_id: uuid.UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SkillDesignMessage:
    id: str
    role: str
    content: str
    created_at: datetime
    operation_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignProgressItem:
    id: str
    label: str
    status: SkillDesignProgressStatus


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationOption:
    id: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationRequest:
    version: int
    kind: str
    source: str
    request_id: str
    clarification_type: str
    title: str
    question: str
    context: str
    input_mode: str
    options: tuple[SkillDesignClarificationOption, ...]


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationResponse:
    version: int
    kind: str
    source: str
    request_id: str
    response_kind: str
    value: str
    option_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignTurnAttachment:
    """User-uploaded UTF-8 reference file scoped to one message turn."""

    name: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillDesignMessageTurn:
    kind: str
    message: str
    model_name: str | None = None
    mode: str | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
    attachments: tuple[SkillDesignTurnAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationTurn:
    kind: str
    response: SkillDesignClarificationResponse
    model_name: str | None = None
    mode: str | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignDraftUpdateTurn:
    kind: str
    expected_draft_checksum: str
    changes: tuple[SkillFileChange, ...]


SkillDesignTurn = SkillDesignMessageTurn | SkillDesignClarificationTurn | SkillDesignDraftUpdateTurn


@dataclass(frozen=True, slots=True)
class SubmitSkillDesignTurn:
    input: SkillDesignTurn
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ValidateSkillDesignSession:
    expected_revision: int
    expected_draft_checksum: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CommitSkillDesignSession:
    expected_revision: int
    expected_draft_checksum: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelSkillDesignSession:
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SetSkillDesignExecutionPreference:
    model_name: str
    mode: str
    thinking_enabled: bool
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class SkillDesignExecutionPreference:
    model_name: str
    mode: str
    thinking_enabled: bool
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class SkillDesignFileView:
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    encoding: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillDesignBaseFile:
    """Pinned base-version file identity used for revision diff rendering."""

    path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillDesignSecretRequirement:
    name: str
    target_env: str
    optional: bool


@dataclass(frozen=True, slots=True)
class SkillDesignValidation:
    draft_checksum: str
    validated_at: datetime
    description: str
    frontmatter: Mapping[str, object]
    compatibility: str | None
    secret_requirements: tuple[SkillDesignSecretRequirement, ...]


@dataclass(frozen=True, slots=True)
class SkillDesignSessionView:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: uuid.UUID
    slug: str
    display_name: str
    status: SkillDesignStatus
    revision: int
    messages: tuple[SkillDesignMessage, ...]
    active_clarification: SkillDesignClarificationRequest | None
    progress: tuple[SkillDesignProgressItem, ...]
    files: tuple[SkillDesignFileView, ...]
    draft_checksum: str | None
    validation: SkillDesignValidation | None
    error_code: str | None
    error_message: str | None
    created_skill_id: uuid.UUID | None
    created_skill_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    active_run: SkillBuilderRunAdmission | None = None
    authoring_dependencies: SkillBuilderDependencySnapshot | None = None
    session_kind: str = "create"
    target_skill_id: uuid.UUID | None = None
    base_version_id: uuid.UUID | None = None
    base_version_number: int | None = None
    base_payload_checksum: str | None = None
    target_skill_deleted: bool = False
    base_files: tuple[SkillDesignBaseFile, ...] = ()
    execution_preference: SkillDesignExecutionPreference | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignSessionSummary:
    id: uuid.UUID
    slug: str
    display_name: str
    status: SkillDesignStatus
    revision: int
    updated_at: datetime
    session_kind: str = "create"


@dataclass(frozen=True, slots=True)
class SkillDesignCommitResult:
    session: SkillDesignSessionView
    skill: SkillAssetView
    version: SkillVersionView | None = None


__all__ = (
    "SkillDesignStatus",
    "SkillDesignProgressStatus",
    "SkillDesignServiceErrorCode",
    "CreateSkillDesignSession",
    "CreateSkillDesignRevisionSession",
    "SkillDesignMessage",
    "SkillDesignProgressItem",
    "SkillDesignClarificationOption",
    "SkillDesignClarificationRequest",
    "SkillDesignClarificationResponse",
    "SkillDesignTurnAttachment",
    "SkillDesignMessageTurn",
    "SkillDesignClarificationTurn",
    "SkillDesignDraftUpdateTurn",
    "SkillDesignTurn",
    "SubmitSkillDesignTurn",
    "ValidateSkillDesignSession",
    "CommitSkillDesignSession",
    "CancelSkillDesignSession",
    "SetSkillDesignExecutionPreference",
    "SkillDesignExecutionPreference",
    "SkillDesignFileView",
    "SkillDesignBaseFile",
    "SkillDesignSecretRequirement",
    "SkillDesignValidation",
    "SkillDesignSessionView",
    "SkillDesignSessionSummary",
    "SkillDesignCommitResult",
)
