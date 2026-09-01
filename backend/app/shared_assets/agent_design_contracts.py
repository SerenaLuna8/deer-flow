"""Immutable public contracts for Agent Builder design sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.shared_assets.agent_design_generation import AgentDesignConflict
from app.shared_assets.agent_service import AgentAssetView, AgentDefinitionView
from app.shared_assets.models import AgentModelSettings, SkillAssetRef


class AgentDesignStatus(StrEnum):
    INTERVIEWING = "interviewing"
    GENERATING = "generating"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    PROPOSAL_READY = "proposal_ready"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentDesignProgressStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentDesignServiceErrorCode(StrEnum):
    """Stable content-free codes persisted on failed sessions."""

    GENERATION_INTERRUPTED = "AGENT_DESIGN_GENERATION_INTERRUPTED"
    GENERATION_UNAVAILABLE = "AGENT_DESIGN_GENERATION_UNAVAILABLE"
    INVALID_MODEL_OUTPUT = "AGENT_DESIGN_INVALID_MODEL_OUTPUT"
    SESSION_CANCELLED = "AGENT_DESIGN_SESSION_CANCELLED"
    COMMIT_FAILED = "AGENT_DESIGN_COMMIT_FAILED"


@dataclass(frozen=True, slots=True)
class CreateAgentDesignSession:
    slug: str
    display_name: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AgentDesignBlueprint:
    description: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_refs: tuple[SkillAssetRef, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_settings: AgentModelSettings = AgentModelSettings()


@dataclass(frozen=True, slots=True)
class AgentDesignMessage:
    id: str
    role: str
    content: str
    created_at: datetime
    operation_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AgentDesignProgressItem:
    id: str
    label: str
    status: AgentDesignProgressStatus


@dataclass(frozen=True, slots=True)
class AgentDesignClarificationOption:
    id: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class AgentDesignClarificationRequest:
    version: int
    kind: str
    source: str
    request_id: str
    clarification_type: str
    title: str
    question: str
    context: str
    input_mode: str
    options: tuple[AgentDesignClarificationOption, ...]


@dataclass(frozen=True, slots=True)
class AgentDesignClarificationResponse:
    version: int
    kind: str
    source: str
    request_id: str
    response_kind: str
    value: str
    option_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentDesignMessageTurn:
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class AgentDesignClarificationTurn:
    kind: str
    response: AgentDesignClarificationResponse


@dataclass(frozen=True, slots=True)
class AgentDesignBlueprintTurn:
    kind: str
    blueprint: AgentDesignBlueprint


AgentDesignTurn = AgentDesignMessageTurn | AgentDesignClarificationTurn | AgentDesignBlueprintTurn


@dataclass(frozen=True, slots=True)
class SubmitAgentDesignTurn:
    input: AgentDesignTurn
    expected_revision: int
    idempotency_key: str
    generation_model_ref: str | None = None
    generation_mode: str | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class SetAgentDesignGenerationPreference:
    generation_model_ref: str
    generation_mode: str
    thinking_enabled: bool
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class CommitAgentDesignSession:
    expected_revision: int
    expected_blueprint_checksum: str
    idempotency_key: str
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class CancelAgentDesignSession:
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AgentDesignSessionView:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: uuid.UUID
    slug: str
    display_name: str
    status: AgentDesignStatus
    revision: int
    blueprint: AgentDesignBlueprint | None
    blueprint_checksum: str | None
    assumptions: tuple[str, ...]
    conflicts: tuple[AgentDesignConflict, ...]
    messages: tuple[AgentDesignMessage, ...]
    active_clarification: AgentDesignClarificationRequest | None
    active_clarifications: tuple[AgentDesignClarificationRequest, ...]
    progress: tuple[AgentDesignProgressItem, ...]
    error_code: str | None
    error_message: str | None
    created_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    generation_preference: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class AgentDesignSessionSummary:
    id: uuid.UUID
    slug: str
    display_name: str
    status: AgentDesignStatus
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentDesignSessionPage:
    items: tuple[AgentDesignSessionSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class AgentDesignCommitResult:
    session: AgentDesignSessionView
    agent: AgentAssetView
    definition: AgentDefinitionView


__all__ = [
    "AgentDesignStatus",
    "AgentDesignProgressStatus",
    "AgentDesignServiceErrorCode",
    "CreateAgentDesignSession",
    "AgentDesignBlueprint",
    "AgentDesignMessage",
    "AgentDesignProgressItem",
    "AgentDesignClarificationOption",
    "AgentDesignClarificationRequest",
    "AgentDesignClarificationResponse",
    "AgentDesignMessageTurn",
    "AgentDesignClarificationTurn",
    "AgentDesignBlueprintTurn",
    "AgentDesignTurn",
    "SubmitAgentDesignTurn",
    "SetAgentDesignGenerationPreference",
    "CommitAgentDesignSession",
    "CancelAgentDesignSession",
    "AgentDesignSessionView",
    "AgentDesignSessionSummary",
    "AgentDesignSessionPage",
    "AgentDesignCommitResult",
]
