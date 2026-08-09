"""Project-scoped conversational Agent design orchestration.

The service deliberately separates each model-backed turn into two database
transactions.  The first transaction durably records the user input and marks
the session as generating.  The model call happens without an open database
transaction, and the second transaction applies only the validated result.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ValidationError
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_generation import (
    DEFAULT_GENERATION_TIMEOUT_SECONDS,
    REQUIRED_INTERVIEW_QUESTIONS,
    AgentDesignDraft,
    AgentDesignGenerationContext,
    AgentDesignGenerationError,
    AgentDesignGenerationRequest,
    AgentDesignGenerationResult,
    AgentDesignGenerationService,
    AgentDesignInterviewAnswer,
    CandidateResult,
    ClarificationQuestion,
    NeedsClarificationResult,
)
from app.shared_assets.agent_design_repository import AgentDesignRepository
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.agent_service import (
    AgentAssetView,
    AgentService,
    AgentVersionView,
    CreateAgent,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.models import AgentModelSettings, AgentPayload
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_GENERATION_MODEL_REF_PATTERN = re.compile(
    r"(?:default|[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?)\Z",
)
_PUBLIC_ERROR_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_IDEMPOTENCY_KEY_CHARS = 255
_MAX_MESSAGE_CHARS = 4_000
_MAX_DESCRIPTION_CHARS = 4_000
_MAX_MODEL_REF_CHARS = 255
_MAX_TOOL_GROUPS = 50
_CLARIFICATION_SET_KIND = "agent_design_clarification_set"
_DEFAULT_STALE_GENERATING_SECONDS = DEFAULT_GENERATION_TIMEOUT_SECONDS + 60.0
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_agent_design_operations_idempotency",
        "uq_agent_design_sessions_create_idempotency",
        "uq_agents_project_slug",
        "uq_agent_versions_asset_number",
    }
)

DEFAULT_AGENT_MODEL_REF = "default"
DEFAULT_AGENT_TOOL_GROUPS: tuple[str, ...] = (
    "web",
    "file:read",
    "file:write",
    "bash",
    "task",
)


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
    skill_version_ids: tuple[uuid.UUID, ...]
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


@dataclass(frozen=True, slots=True)
class CommitAgentDesignSession:
    expected_revision: int
    expected_blueprint_checksum: str
    idempotency_key: str


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
    messages: tuple[AgentDesignMessage, ...]
    active_clarification: AgentDesignClarificationRequest | None
    active_clarifications: tuple[AgentDesignClarificationRequest, ...]
    progress: tuple[AgentDesignProgressItem, ...]
    error_code: str | None
    error_message: str | None
    created_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentDesignSessionSummary:
    id: uuid.UUID
    slug: str
    display_name: str
    status: AgentDesignStatus
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentDesignCommitResult:
    session: AgentDesignSessionView
    agent: AgentAssetView
    version: AgentVersionView


class _RepositoryFactory(Protocol):
    def __call__(self, session: AsyncSession) -> AgentDesignRepository: ...


class AgentDesignService:
    """Coordinate owner-scoped Agent Builder sessions."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        generator: AgentDesignGenerationService | None = None,
        agent_service: AgentService | None = None,
        repository_factory: _RepositoryFactory = AgentDesignRepository,
        default_tool_groups_provider: Callable[[], tuple[str, ...]] | None = None,
        stale_generating_seconds: float = _DEFAULT_STALE_GENERATING_SECONDS,
    ) -> None:
        if not isinstance(stale_generating_seconds, int | float) or isinstance(stale_generating_seconds, bool) or stale_generating_seconds <= 0:
            raise ValueError("stale_generating_seconds must be positive")
        self._session_factory = session_factory
        self._generator = generator or AgentDesignGenerationService()
        self._agent_service = agent_service or AgentService(session_factory)
        self._repository_factory = repository_factory
        self._default_tool_groups_provider = default_tool_groups_provider or (lambda: DEFAULT_AGENT_TOOL_GROUPS)
        self._stale_after = timedelta(seconds=float(stale_generating_seconds))

    async def create(
        self,
        context: ProjectContext,
        command: CreateAgentDesignSession,
    ) -> AgentDesignSessionView:
        command = self._validate_create(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        idempotency_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "slug": command.slug,
                "display_name": command.display_name,
            }
        )
        now = self._now()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    existing = await repository.get_by_create_idempotency(
                        context,
                        idempotency_hash,
                        for_update=True,
                    )
                    if existing is not None:
                        if existing.create_request_checksum != request_checksum:
                            raise AssetConflict(context.request_id)
                        await self._recover_stale_generating(
                            repository,
                            context,
                            existing,
                            now=now,
                        )
                        return self._session_view(existing)
                    row = AgentDesignSessionRow(
                        id=uuid.uuid4(),
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=uuid.uuid4(),
                        slug=command.slug,
                        display_name=command.display_name,
                        status=AgentDesignStatus.INTERVIEWING.value,
                        revision=1,
                        messages_json=[
                            self._message_json(
                                "assistant",
                                "请描述你想创建的 Agent，包括它的用途、工作方式和期望输出。",
                                now=now,
                            )
                        ],
                        progress_json=self._progress_json(AgentDesignProgressStatus.PENDING),
                        active_clarification_json=None,
                        blueprint_json=None,
                        blueprint_checksum=None,
                        error_code=None,
                        error_message=None,
                        created_agent_id=None,
                        created_agent_version_id=None,
                        create_idempotency_key_hash=idempotency_hash,
                        create_request_checksum=request_checksum,
                        created_at=now,
                        updated_at=now,
                    )
                    await repository.create(context, row)
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
    ) -> tuple[AgentDesignSessionSummary, ...]:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AssetValidationFailed(context.request_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    rows = await repository.list_incomplete(context, limit=limit)
                    now = self._now()
                    resolved: list[AgentDesignSessionSummary] = []
                    for listed in rows:
                        row = listed
                        if self._is_stale_generating(row, now=now):
                            row = await repository.get(
                                context,
                                row.id,
                                for_update=True,
                            )
                            await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=now,
                            )
                        resolved.append(self._session_summary(row))
                    return tuple(resolved)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def get(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> AgentDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    await self._recover_stale_generating(
                        repository,
                        context,
                        row,
                        now=self._now(),
                    )
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def submit_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SubmitAgentDesignTurn,
    ) -> AgentDesignSessionView:
        command = self._validate_turn(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "input": command.input,
                "generation_model_ref": command.generation_model_ref,
            }
        )

        prepared = await self._prepare_turn(
            context,
            session_id,
            command,
            operation_hash=operation_hash,
            request_checksum=request_checksum,
        )
        if isinstance(prepared, AgentDesignSessionView):
            return prepared
        generation_revision, request, generation_context = prepared
        try:
            result = await self._generator.generate(
                request,
                context=generation_context,
                model_ref=command.generation_model_ref,
            )
        except AgentDesignGenerationError as exc:
            code = exc.code if isinstance(exc.code, str) and _PUBLIC_ERROR_PATTERN.fullmatch(exc.code) else AgentDesignServiceErrorCode.GENERATION_UNAVAILABLE.value
            return await self._finish_generation_failure(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                error_code=code,
                error_message=self._stable_generation_error_message(code),
            )
        except Exception:
            return await self._finish_generation_failure(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                error_code=AgentDesignServiceErrorCode.GENERATION_UNAVAILABLE.value,
                error_message="Agent 设定生成暂时不可用，请稍后重试。",
            )
        return await self._finish_generation_success(
            context,
            session_id,
            operation_hash=operation_hash,
            generation_revision=generation_revision,
            result=result,
        )

    async def commit(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: CommitAgentDesignSession,
    ) -> AgentDesignCommitResult:
        command = self._validate_commit(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "expected_blueprint_checksum": command.expected_blueprint_checksum,
            }
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="commit",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed" and row.status == AgentDesignStatus.COMPLETED.value:
                            return await self._committed_result(
                                session,
                                context,
                                row,
                            )
                        if operation.status == "in_progress":
                            raise AssetConflict(context.request_id)
                    if row.status == AgentDesignStatus.COMPLETED.value:
                        if row.blueprint_checksum != command.expected_blueprint_checksum:
                            raise AssetConflict(context.request_id)
                        return await self._committed_result(
                            session,
                            context,
                            row,
                        )
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    if row.status != AgentDesignStatus.PROPOSAL_READY.value or row.blueprint_json is None or row.blueprint_checksum != command.expected_blueprint_checksum:
                        raise AssetConflict(context.request_id)
                    if operation is None:
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="commit",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)
                    else:
                        self._reset_operation(operation)
                    blueprint = self._blueprint_from_json(row.blueprint_json)
                    row.status = AgentDesignStatus.COMMITTING.value
                    row.error_code = None
                    row.error_message = None
                    await session.flush()
                    created = await self._agent_service.create_project_from_design_in_session(
                        session,
                        context,
                        CreateAgent(
                            slug=row.slug,
                            display_name=row.display_name,
                        ),
                        self._agent_payload(blueprint),
                    )
                    row.status = AgentDesignStatus.COMPLETED.value
                    row.created_agent_id = created.asset.id
                    row.created_agent_version_id = created.version.id
                    row.revision += 1
                    row.progress_json = self._progress_json(AgentDesignProgressStatus.COMPLETED)
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await session.flush()
                    return AgentDesignCommitResult(
                        session=self._session_view(row),
                        agent=created.asset,
                        version=created.version,
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def cancel(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: CancelAgentDesignSession,
    ) -> AgentDesignSessionView:
        command = self._validate_cancel(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
            }
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="cancel",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            return self._session_view(row)
                        if operation.status == "in_progress":
                            raise AssetConflict(context.request_id)
                    if row.status == AgentDesignStatus.CANCELLED.value:
                        return self._session_view(row)
                    if row.status == AgentDesignStatus.COMPLETED.value:
                        raise AssetConflict(context.request_id)
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    if operation is None:
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="cancel",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)
                    else:
                        self._reset_operation(operation)
                    row.status = AgentDesignStatus.CANCELLED.value
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.revision += 1
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await session.flush()
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    def blueprint_checksum(blueprint: AgentDesignBlueprint) -> str:
        if not isinstance(blueprint, AgentDesignBlueprint):
            raise TypeError("blueprint must be AgentDesignBlueprint")
        canonical = json.dumps(
            AgentDesignService._blueprint_json(blueprint),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _prepare_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SubmitAgentDesignTurn,
        *,
        operation_hash: str,
        request_checksum: str,
    ) -> (
        AgentDesignSessionView
        | tuple[
            int,
            AgentDesignGenerationRequest,
            AgentDesignGenerationContext,
        ]
    ):
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    retry_existing = False
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            return self._session_view(row)
                        if operation.status == "in_progress":
                            if not self._is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=self._now(),
                            )
                            operation.status = "failed"
                            operation.result_revision = row.revision
                            operation.public_error_code = AgentDesignServiceErrorCode.GENERATION_INTERRUPTED.value
                        if operation.status != "failed" or row.status != AgentDesignStatus.FAILED.value or operation.result_revision != row.revision:
                            raise AssetConflict(context.request_id)
                        retry_existing = True
                    else:
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        if row.status == AgentDesignStatus.GENERATING.value:
                            if not self._is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=self._now(),
                            )
                        self._require_nonterminal(context, row)
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="turn",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)

                    if isinstance(command.input, AgentDesignBlueprintTurn):
                        if retry_existing and operation.status == "completed":
                            return self._session_view(row)
                        blueprint = self._validate_blueprint(
                            context,
                            command.input.blueprint,
                        )
                        row.blueprint_json = self._blueprint_json(blueprint)
                        row.blueprint_checksum = self.blueprint_checksum(blueprint)
                        row.status = AgentDesignStatus.PROPOSAL_READY.value
                        row.active_clarification_json = None
                        row.error_code = None
                        row.error_message = None
                        row.progress_json = self._progress_json(AgentDesignProgressStatus.COMPLETED)
                        if not retry_existing:
                            row.messages_json = [
                                *row.messages_json,
                                self._message_json(
                                    "user",
                                    "已手动更新 Agent 设定。",
                                    now=self._now(),
                                ),
                            ]
                        row.revision += 1
                        operation.status = "completed"
                        operation.result_revision = row.revision
                        operation.public_error_code = None
                        await session.flush()
                        return self._session_view(row)

                    if not retry_existing:
                        ready_to_generate = self._append_turn_input(
                            context,
                            row,
                            command.input,
                        )
                        if not ready_to_generate:
                            row.status = AgentDesignStatus.AWAITING_CLARIFICATION.value
                            row.error_code = None
                            row.error_message = None
                            row.progress_json = self._progress_json(
                                AgentDesignProgressStatus.PENDING,
                            )
                            row.revision += 1
                            operation.status = "completed"
                            operation.result_revision = row.revision
                            operation.public_error_code = None
                            await session.flush()
                            return self._session_view(row)
                    blueprint = self._blueprint_from_json(row.blueprint_json) if row.blueprint_json is not None else self._default_blueprint(self._first_user_message(row))
                    generation_request = self._generation_request(
                        row,
                        blueprint,
                        command.input,
                    )
                    generation_context = AgentDesignGenerationContext(
                        allowed_assets=(),
                        allowed_capabilities=blueprint.tool_groups,
                    )
                    row.status = AgentDesignStatus.GENERATING.value
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.progress_json = self._progress_json(AgentDesignProgressStatus.RUNNING)
                    row.revision += 1
                    self._reset_operation(operation)
                    await session.flush()
                    return (
                        row.revision,
                        generation_request,
                        generation_context,
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _finish_generation_success(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        result: AgentDesignGenerationResult,
    ) -> AgentDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is None or operation.status != "in_progress" or row.status != AgentDesignStatus.GENERATING.value or row.revision != generation_revision:
                        raise AssetConflict(context.request_id)
                    if isinstance(result, NeedsClarificationResult):
                        if len(result.questions) != 1:
                            raise AssetValidationFailed(context.request_id)
                        question_number = len(self._clarification_history(row)) + 1
                        if question_number > REQUIRED_INTERVIEW_QUESTIONS:
                            raise AssetValidationFailed(context.request_id)
                        clarification = self._clarification_request(
                            result.questions[0],
                            index=question_number,
                            total=REQUIRED_INTERVIEW_QUESTIONS,
                        )
                        row.status = AgentDesignStatus.AWAITING_CLARIFICATION.value
                        row.active_clarification_json = self._clarification_json(clarification)
                        row.progress_json = self._progress_json(AgentDesignProgressStatus.PENDING)
                    elif isinstance(result, CandidateResult):
                        current = (
                            self._blueprint_from_json(row.blueprint_json)
                            if row.blueprint_json is not None
                            else await self._default_blueprint_with_system_dependencies(
                                session,
                                context,
                                self._first_user_message(row),
                            )
                        )
                        blueprint = self._candidate_blueprint(
                            context,
                            current,
                            result,
                        )
                        row.blueprint_json = self._blueprint_json(blueprint)
                        row.blueprint_checksum = self.blueprint_checksum(blueprint)
                        row.status = AgentDesignStatus.PROPOSAL_READY.value
                        row.active_clarification_json = None
                        row.progress_json = self._progress_json(AgentDesignProgressStatus.COMPLETED)
                        summary = "已生成 AGENTS.md、SOUL.md、IDENTITY.md 和 USER.md 的完整设定，请确认或继续调整。"
                        if result.conflicts:
                            summary = f"{summary} 另有 {len(result.conflicts)} 项设定冲突需要你在预览中确认。"
                        row.messages_json = [
                            *row.messages_json,
                            self._message_json(
                                "assistant",
                                summary,
                                now=self._now(),
                            ),
                        ]
                    else:
                        raise AssetValidationFailed(context.request_id)
                    row.error_code = None
                    row.error_message = None
                    row.revision += 1
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await session.flush()
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _finish_generation_failure(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        error_code: str,
        error_message: str,
    ) -> AgentDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is None or operation.status != "in_progress" or row.status != AgentDesignStatus.GENERATING.value or row.revision != generation_revision:
                        return self._session_view(row)
                    row.status = AgentDesignStatus.FAILED.value
                    row.error_code = error_code
                    row.error_message = error_message
                    row.progress_json = self._progress_json(AgentDesignProgressStatus.FAILED)
                    row.messages_json = [
                        *row.messages_json,
                        self._message_json(
                            "assistant",
                            error_message,
                            now=self._now(),
                        ),
                    ]
                    row.revision += 1
                    operation.status = "failed"
                    operation.result_revision = row.revision
                    operation.public_error_code = error_code
                    await session.flush()
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _recover_stale_generating(
        self,
        repository: AgentDesignRepository,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        *,
        now: datetime,
    ) -> bool:
        if not self._is_stale_generating(row, now=now):
            return False
        code = AgentDesignServiceErrorCode.GENERATION_INTERRUPTED.value
        row.status = AgentDesignStatus.FAILED.value
        row.error_code = code
        row.error_message = "上一次生成已中断，请重新发送你的要求。"
        row.progress_json = self._progress_json(AgentDesignProgressStatus.FAILED)
        row.active_clarification_json = None
        row.revision += 1
        await repository.session.execute(
            update(AgentDesignOperationRow)
            .where(
                AgentDesignOperationRow.project_id == context.project_id,
                AgentDesignOperationRow.owner_user_id == str(context.user_id),
                AgentDesignOperationRow.session_id == row.id,
                AgentDesignOperationRow.status == "in_progress",
            )
            .values(
                status="failed",
                result_revision=row.revision,
                public_error_code=code,
            )
        )
        await repository.session.flush()
        return True

    def _is_stale_generating(
        self,
        row: AgentDesignSessionRow,
        *,
        now: datetime,
    ) -> bool:
        if row.status != AgentDesignStatus.GENERATING.value:
            return False
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return updated_at <= now - self._stale_after

    async def _committed_result(
        self,
        session: AsyncSession,
        context: ProjectContext,
        row: AgentDesignSessionRow,
    ) -> AgentDesignCommitResult:
        if row.created_agent_id is None or row.created_agent_version_id is None:
            raise AssetConflict(context.request_id)
        repository = AgentRepository(session)
        asset = await repository.get_project_asset(
            context,
            row.created_agent_id,
        )
        version = await repository.get_project_version(
            context,
            row.created_agent_id,
            row.created_agent_version_id,
        )
        return AgentDesignCommitResult(
            session=self._session_view(row),
            agent=AgentService._asset_view(asset),
            version=AgentService._version_view(version),
        )

    def _append_turn_input(
        self,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        turn: AgentDesignMessageTurn | AgentDesignClarificationTurn,
    ) -> bool:
        if isinstance(turn, AgentDesignMessageTurn):
            if row.active_clarification_json is not None:
                raise AssetConflict(context.request_id)
            content = turn.message
            messages = [self._message_json("user", content, now=self._now())]
            ready_to_generate = True
        elif isinstance(turn, AgentDesignClarificationTurn):
            active = row.active_clarification_json
            if row.status != AgentDesignStatus.AWAITING_CLARIFICATION.value or not isinstance(active, Mapping):
                raise AssetConflict(context.request_id)
            clarifications = self._clarifications_from_json(active)
            answered = self._clarification_answers(row)
            pending = tuple(request for request in clarifications if request.request_id not in answered)
            matching = pending[0] if pending else None
            if matching is None or matching.source != turn.response.source or matching.request_id != turn.response.request_id:
                raise AssetConflict(context.request_id)
            matching_json = self._clarification_json(matching)
            self._require_matching_clarification_response(
                context,
                matching_json,
                turn.response,
            )
            content = turn.response.value
            messages = [
                self._message_json(
                    "assistant",
                    matching.question,
                    now=self._now(),
                ),
                self._message_json(
                    "user",
                    content,
                    now=self._now(),
                    clarification_request_id=turn.response.request_id,
                    clarification_question=matching.question,
                ),
            ]
            answered[turn.response.request_id] = content
            ready_to_generate = True
            row.active_clarification_json = None
        else:
            raise AssetValidationFailed(context.request_id)
        row.messages_json = [
            *row.messages_json,
            *messages,
        ]
        if isinstance(turn, AgentDesignMessageTurn):
            row.active_clarification_json = None
        return ready_to_generate

    @staticmethod
    def _require_matching_clarification_response(
        context: ProjectContext,
        active: Mapping[str, object],
        response: AgentDesignClarificationResponse,
    ) -> None:
        input_mode = active.get("input_mode")
        if response.response_kind == "text":
            if response.option_id is not None or input_mode not in {
                "free_text",
                "choice_with_other",
            }:
                raise AssetConflict(context.request_id)
            return
        if input_mode not in {"single_choice", "choice_with_other"}:
            raise AssetConflict(context.request_id)
        options = active.get("options")
        if not isinstance(options, list):
            raise AssetConflict(context.request_id)
        selected = next(
            (option for option in options if isinstance(option, Mapping) and option.get("id") == response.option_id),
            None,
        )
        if selected is None or selected.get("value") != response.value:
            raise AssetConflict(context.request_id)

    @classmethod
    def _generation_request(
        cls,
        row: AgentDesignSessionRow,
        blueprint: AgentDesignBlueprint,
        turn: AgentDesignMessageTurn | AgentDesignClarificationTurn,
    ) -> AgentDesignGenerationRequest:
        current = AgentDesignDraft(
            agents_instructions=blueprint.agents_instructions,
            soul=blueprint.soul,
            identity=blueprint.identity,
            user_context=blueprint.user_context,
        )
        answers = cls._clarification_answers(row)
        interview_history = cls._clarification_history(row)
        phase = "composition" if len(interview_history) >= REQUIRED_INTERVIEW_QUESTIONS else "discovery"
        if isinstance(turn, AgentDesignClarificationTurn):
            if not answers:
                raise AssetValidationFailed("unknown")
        elif any(
            (
                current.agents_instructions,
                current.soul,
                current.identity,
                current.user_context,
            )
        ):
            answers["revision_request"] = turn.message
        elif answers:
            answers["retry_request"] = turn.message
        else:
            answers = {}
        return AgentDesignGenerationRequest(
            agent_name=row.display_name,
            brief=blueprint.description,
            answers=answers,
            interview_history=interview_history,
            current_draft=current,
            mode=("revise" if any(current.model_dump().values()) else "initial"),
            phase=phase,
        )

    def _default_blueprint(self, description: str) -> AgentDesignBlueprint:
        return AgentDesignBlueprint(
            description=description,
            model_ref=DEFAULT_AGENT_MODEL_REF,
            tool_groups=tuple(dict.fromkeys(self._default_tool_groups_provider())),
            skill_version_ids=(),
            mcp_version_ids=(),
            agents_instructions="",
            soul="",
            identity="",
            user_context="",
            model_settings=AgentModelSettings(),
        )

    async def _default_blueprint_with_system_dependencies(
        self,
        session: AsyncSession,
        context: ProjectContext,
        description: str,
    ) -> AgentDesignBlueprint:
        skill_version_ids, mcp_version_ids = await AgentRepository(session).list_enabled_system_dependency_versions(context)
        return replace(
            self._default_blueprint(description),
            skill_version_ids=skill_version_ids,
            mcp_version_ids=mcp_version_ids,
        )

    @staticmethod
    def _first_user_message(row: AgentDesignSessionRow) -> str:
        for message in row.messages_json:
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        raise AssetValidationFailed("unknown")

    @classmethod
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
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(context.request_id)
        return CreateAgentDesignSession(
            slug=slug,
            display_name=display_name,
            idempotency_key=idempotency_key,
        )

    @classmethod
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
        if generation_model_ref is not None and (not isinstance(generation_model_ref, str) or _GENERATION_MODEL_REF_PATTERN.fullmatch(generation_model_ref) is None):
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
        )

    @classmethod
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
        return CommitAgentDesignSession(
            expected_revision=command.expected_revision,
            expected_blueprint_checksum=command.expected_blueprint_checksum,
            idempotency_key=cls._validate_idempotency_key(
                context,
                command.idempotency_key,
            ),
        )

    @classmethod
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

    @staticmethod
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
            skill_version_ids = tuple(blueprint.skill_version_ids)
            mcp_version_ids = tuple(blueprint.mcp_version_ids)
        except TypeError:
            raise AssetValidationFailed(context.request_id) from None
        if (
            not description
            or len(description) > _MAX_DESCRIPTION_CHARS
            or not model_ref
            or len(model_ref) > _MAX_MODEL_REF_CHARS
            or not tool_groups
            or len(tool_groups) > _MAX_TOOL_GROUPS
            or any(not isinstance(group, str) or _CAPABILITY_PATTERN.fullmatch(group) is None for group in tool_groups)
            or len(set(tool_groups)) != len(tool_groups)
        ):
            raise AssetValidationFailed(context.request_id)
        for values in (skill_version_ids, mcp_version_ids):
            if any(not isinstance(value, uuid.UUID) for value in values) or len(set(values)) != len(values):
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
            skill_version_ids=skill_version_ids,
            mcp_version_ids=mcp_version_ids,
            agents_instructions=documents.agents_instructions,
            soul=documents.soul,
            identity=documents.identity,
            user_context=documents.user_context,
            model_settings=model_settings,
        )

    @classmethod
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
                skill_version_ids=current.skill_version_ids,
                mcp_version_ids=current.mcp_version_ids,
                agents_instructions=result.documents.agents_instructions,
                soul=result.documents.soul,
                identity=result.documents.identity,
                user_context=result.documents.user_context,
                model_settings=current.model_settings,
            ),
        )

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @classmethod
    def _require_capability(
        cls,
        context: ProjectContext,
        capability: Capability,
    ) -> None:
        cls._require_context(context)
        if capability not in context.capabilities:
            raise AssetForbidden(context.request_id)

    @staticmethod
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

    @staticmethod
    def _require_expected_revision(
        context: ProjectContext,
        row: AgentDesignSessionRow,
        expected: int,
    ) -> None:
        if row.revision != expected:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _require_matching_operation(
        context: ProjectContext,
        operation: AgentDesignOperationRow,
        *,
        session_id: uuid.UUID,
        request_checksum: str,
    ) -> None:
        if operation.session_id != session_id or operation.request_checksum != request_checksum:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _new_operation(
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        kind: str,
        idempotency_hash: str,
        request_checksum: str,
    ) -> AgentDesignOperationRow:
        now = AgentDesignService._now()
        return AgentDesignOperationRow(
            id=uuid.uuid4(),
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            session_id=session_id,
            operation_kind=kind,
            idempotency_key_hash=idempotency_hash,
            request_checksum=request_checksum,
            status="in_progress",
            result_revision=None,
            public_error_code=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _reset_operation(operation: AgentDesignOperationRow) -> None:
        operation.status = "in_progress"
        operation.result_revision = None
        operation.public_error_code = None

    @staticmethod
    def _agent_payload(blueprint: AgentDesignBlueprint) -> AgentPayload:
        return AgentPayload(
            description=blueprint.description,
            model_ref=blueprint.model_ref,
            tool_groups=blueprint.tool_groups,
            skill_version_ids=blueprint.skill_version_ids,
            mcp_version_ids=blueprint.mcp_version_ids,
            agents_instructions=blueprint.agents_instructions,
            soul=blueprint.soul,
            identity=blueprint.identity,
            user_context=blueprint.user_context,
            payload_schema_version=(3 if not blueprint.model_settings.is_empty else 2),
            model_settings=blueprint.model_settings,
        )

    @staticmethod
    def _valid_revision(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    @staticmethod
    def _validate_uuid(
        context: ProjectContext,
        value: object,
    ) -> uuid.UUID:
        if not isinstance(value, uuid.UUID):
            raise AssetValidationFailed(context.request_id)
        return value

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def _idempotency_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_checksum(value: object) -> str:
        canonical = json.dumps(
            AgentDesignService._jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _jsonable(value: object) -> object:
        if isinstance(value, BaseModel):
            return AgentDesignService._jsonable(value.model_dump(mode="json"))
        if is_dataclass(value):
            document: dict[str, object] = {}
            for field in fields(value):
                item = getattr(value, field.name)
                # Empty model settings did not exist in the v2 Builder request
                # contract. Omitting them preserves idempotent retries across
                # an in-place checkout upgrade.
                if isinstance(item, AgentModelSettings) and item.is_empty:
                    continue
                document[field.name] = AgentDesignService._jsonable(item)
            return document
        if isinstance(value, Mapping):
            return {str(key): AgentDesignService._jsonable(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [AgentDesignService._jsonable(item) for item in value]
        if isinstance(value, (uuid.UUID, datetime, StrEnum)):
            return str(value)
        return value

    @staticmethod
    def _message_json(
        role: str,
        content: str,
        *,
        now: datetime,
        clarification_request_id: str | None = None,
        clarification_question: str | None = None,
    ) -> dict[str, object]:
        message: dict[str, object] = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "created_at": now.isoformat(),
        }
        if clarification_request_id is not None:
            message["clarification_request_id"] = clarification_request_id
        if clarification_question is not None:
            message["clarification_question"] = clarification_question
        return message

    @staticmethod
    def _progress_json(
        status: AgentDesignProgressStatus,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": field,
                "label": label,
                "status": status.value,
            }
            for field, label in (
                ("agents_instructions", "AGENTS.md"),
                ("soul", "SOUL.md"),
                ("identity", "IDENTITY.md"),
                ("user_context", "USER.md"),
            )
        ]

    @staticmethod
    def _blueprint_json(
        blueprint: AgentDesignBlueprint,
    ) -> dict[str, object]:
        document: dict[str, object] = {
            "description": blueprint.description,
            "model_ref": blueprint.model_ref,
            "tool_groups": list(blueprint.tool_groups),
            "skill_version_ids": [str(value) for value in blueprint.skill_version_ids],
            "mcp_version_ids": [str(value) for value in blueprint.mcp_version_ids],
            "agents_instructions": blueprint.agents_instructions,
            "soul": blueprint.soul,
            "identity": blueprint.identity,
            "user_context": blueprint.user_context,
        }
        if not blueprint.model_settings.is_empty:
            document["model_settings"] = blueprint.model_settings.model_dump(exclude_none=True)
        return document

    @staticmethod
    def _blueprint_from_json(
        raw: Mapping[str, object] | None,
    ) -> AgentDesignBlueprint:
        if raw is None:
            raise AssetValidationFailed("unknown")
        try:
            return AgentDesignBlueprint(
                description=str(raw["description"]),
                model_ref=str(raw["model_ref"]),
                tool_groups=tuple(str(item) for item in raw["tool_groups"]),
                skill_version_ids=tuple(uuid.UUID(str(item)) for item in raw["skill_version_ids"]),
                mcp_version_ids=tuple(uuid.UUID(str(item)) for item in raw["mcp_version_ids"]),
                agents_instructions=str(raw["agents_instructions"]),
                soul=str(raw["soul"]),
                identity=str(raw["identity"]),
                user_context=str(raw["user_context"]),
                model_settings=AgentModelSettings.model_validate(raw.get("model_settings", {})),
            )
        except (KeyError, TypeError, ValueError):
            raise AssetValidationFailed("unknown") from None

    @staticmethod
    def _clarification_request(
        question: ClarificationQuestion,
        *,
        index: int = 1,
        total: int = 1,
    ) -> AgentDesignClarificationRequest:
        if question.kind == "free_text":
            input_mode = "free_text"
        elif question.kind == "single_select":
            input_mode = "choice_with_other"
        else:
            input_mode = "choice_with_other"
        options = tuple(
            AgentDesignClarificationOption(
                id=f"{question.id}-{index + 1}",
                label=option,
                value=option,
            )
            for index, option in enumerate(question.options)
        )
        return AgentDesignClarificationRequest(
            version=1,
            kind="human_input_request",
            source="agent_builder",
            request_id=question.id,
            clarification_type="agent_design",
            title=f"问题 {index}/{total}",
            question=question.prompt,
            context=question.reason,
            input_mode=input_mode,
            options=options,
        )

    @staticmethod
    def _clarification_json(
        request: AgentDesignClarificationRequest,
    ) -> dict[str, object]:
        return {
            "version": request.version,
            "kind": request.kind,
            "source": request.source,
            "request_id": request.request_id,
            "clarification_type": request.clarification_type,
            "title": request.title,
            "question": request.question,
            "context": request.context,
            "input_mode": request.input_mode,
            "options": [
                {
                    "id": option.id,
                    "label": option.label,
                    "value": option.value,
                }
                for option in request.options
            ],
        }

    @classmethod
    def _clarification_set_json(
        cls,
        requests: tuple[AgentDesignClarificationRequest, ...],
    ) -> dict[str, object]:
        if len(requests) != 3:
            raise AssetValidationFailed("unknown")
        return {
            "version": 1,
            "kind": _CLARIFICATION_SET_KIND,
            "questions": [cls._clarification_json(request) for request in requests],
        }

    @classmethod
    def _clarifications_from_json(
        cls,
        raw: Mapping[str, object],
    ) -> tuple[AgentDesignClarificationRequest, ...]:
        if raw.get("kind") == _CLARIFICATION_SET_KIND:
            questions = raw.get("questions")
            if not isinstance(questions, list) or len(questions) != 3:
                raise AssetValidationFailed("unknown")
            values = tuple(cls._clarification_from_json(question) for question in questions if isinstance(question, Mapping))
            if len(values) != 3:
                raise AssetValidationFailed("unknown")
            return values
        return (cls._clarification_from_json(raw),)

    @staticmethod
    def _clarification_from_json(
        raw: Mapping[str, object],
    ) -> AgentDesignClarificationRequest:
        try:
            return AgentDesignClarificationRequest(
                version=int(raw["version"]),
                kind=str(raw["kind"]),
                source=str(raw["source"]),
                request_id=str(raw["request_id"]),
                clarification_type=str(raw["clarification_type"]),
                title=str(raw["title"]),
                question=str(raw["question"]),
                context=str(raw["context"]),
                input_mode=str(raw["input_mode"]),
                options=tuple(
                    AgentDesignClarificationOption(
                        id=str(option["id"]),
                        label=str(option["label"]),
                        value=str(option["value"]),
                    )
                    for option in raw.get("options", ())
                    if isinstance(option, Mapping)
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise AssetValidationFailed("unknown") from None

    @staticmethod
    def _clarification_answers(
        row: AgentDesignSessionRow,
    ) -> dict[str, str]:
        answers: dict[str, str] = {}
        for message in row.messages_json:
            request_id = message.get("clarification_request_id")
            content = message.get("content")
            if isinstance(request_id, str) and isinstance(content, str):
                answers[request_id] = content
        return answers

    @staticmethod
    def _clarification_history(
        row: AgentDesignSessionRow,
    ) -> tuple[AgentDesignInterviewAnswer, ...]:
        history: list[AgentDesignInterviewAnswer] = []
        for message in row.messages_json:
            request_id = message.get("clarification_request_id")
            content = message.get("content")
            question = message.get("clarification_question")
            if isinstance(request_id, str) and isinstance(content, str):
                history.append(
                    AgentDesignInterviewAnswer(
                        id=request_id,
                        question=(question if isinstance(question, str) else request_id),
                        answer=content,
                    )
                )
        return tuple(history)

    @staticmethod
    def _session_view(
        row: AgentDesignSessionRow,
    ) -> AgentDesignSessionView:
        blueprint = AgentDesignService._blueprint_from_json(row.blueprint_json) if row.blueprint_json is not None else None
        messages = tuple(
            AgentDesignMessage(
                id=str(item["id"]),
                role=str(item["role"]),
                content=str(item["content"]),
                created_at=datetime.fromisoformat(str(item["created_at"])),
            )
            for item in row.messages_json
        )
        progress = tuple(
            AgentDesignProgressItem(
                id=str(item["id"]),
                label=str(item["label"]),
                status=AgentDesignProgressStatus(str(item["status"])),
            )
            for item in row.progress_json
        )
        active_raw = row.active_clarification_json
        active_clarifications: tuple[AgentDesignClarificationRequest, ...] = ()
        if active_raw is not None:
            answered = AgentDesignService._clarification_answers(row)
            active_clarifications = tuple(request for request in AgentDesignService._clarifications_from_json(active_raw) if request.request_id not in answered)
        active = active_clarifications[0] if active_clarifications else None
        return AgentDesignSessionView(
            id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            slug=row.slug,
            display_name=row.display_name,
            status=AgentDesignStatus(row.status),
            revision=row.revision,
            blueprint=blueprint,
            blueprint_checksum=row.blueprint_checksum,
            messages=messages,
            active_clarification=active,
            active_clarifications=active_clarifications,
            progress=progress,
            error_code=row.error_code,
            error_message=row.error_message,
            created_agent_id=row.created_agent_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _session_summary(
        row: AgentDesignSessionRow,
    ) -> AgentDesignSessionSummary:
        return AgentDesignSessionSummary(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            status=AgentDesignStatus(row.status),
            revision=row.revision,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _stable_generation_error_message(code: str) -> str:
        if code == "AGENT_DESIGN_GENERATION_TIMEOUT":
            return "Agent 设定生成超时，请稍后重试。"
        if code in {
            "AGENT_DESIGN_INVALID_MODEL_OUTPUT",
            "AGENT_DESIGN_UNSUPPORTED_CAPABILITY",
            "AGENT_DESIGN_UNDECLARED_CAPABILITY",
        }:
            return "模型返回的 Agent 设定无效，请调整描述后重试。"
        if code in {
            "AGENT_DESIGN_SECRET_DETECTED",
            "AGENT_DESIGN_UNSAFE_MODEL_OUTPUT",
        }:
            return "Agent 设定包含不安全或敏感内容，请修改后重试。"
        return "Agent 设定生成暂时不可用，请稍后重试。"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


__all__ = [
    "AgentDesignBlueprint",
    "AgentDesignBlueprintTurn",
    "AgentDesignClarificationOption",
    "AgentDesignClarificationRequest",
    "AgentDesignClarificationResponse",
    "AgentDesignClarificationTurn",
    "AgentDesignCommitResult",
    "AgentDesignMessage",
    "AgentDesignMessageTurn",
    "AgentDesignProgressItem",
    "AgentDesignProgressStatus",
    "AgentDesignService",
    "AgentDesignServiceErrorCode",
    "AgentDesignSessionSummary",
    "AgentDesignSessionView",
    "AgentDesignStatus",
    "CancelAgentDesignSession",
    "CommitAgentDesignSession",
    "CreateAgentDesignSession",
    "DEFAULT_AGENT_MODEL_REF",
    "DEFAULT_AGENT_TOOL_GROUPS",
    "SubmitAgentDesignTurn",
]
