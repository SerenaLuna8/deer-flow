"""Project-scoped conversational Agent design orchestration.

The service deliberately separates each model-backed turn into two database
transactions.  The first transaction durably records the user input and marks
the session as generating.  The model call happens without an open database
transaction, and the second transaction applies only the validated result.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_design_activity import (
    AgentDesignActivity,
    AgentDesignActivityKind,
    AgentDesignActivityLimitExceeded,
    AgentDesignActivityRepository,
    activity_view,
)
from app.shared_assets.agent_design_codec import (
    _agent_payload as _agent_payload_impl,
)
from app.shared_assets.agent_design_codec import (
    _blueprint_from_json as _blueprint_from_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _blueprint_json as _blueprint_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _candidate_metadata_from_json as _candidate_metadata_from_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_answers as _clarification_answers_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_from_json as _clarification_from_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_history as _clarification_history_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_json as _clarification_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_request as _clarification_request_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarification_set_json as _clarification_set_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _clarifications_from_json as _clarifications_from_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _decode_session_cursor as _decode_session_cursor_impl,
)
from app.shared_assets.agent_design_codec import (
    _encode_session_cursor as _encode_session_cursor_impl,
)
from app.shared_assets.agent_design_codec import (
    _has_blocking_conflicts as _has_blocking_conflicts_impl,
)
from app.shared_assets.agent_design_codec import _jsonable as _jsonable_impl
from app.shared_assets.agent_design_codec import (
    _message_json as _message_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _progress_json as _progress_json_impl,
)
from app.shared_assets.agent_design_codec import (
    _remaining_conflicts_after_blueprint_update as _remaining_conflicts_after_blueprint_update_impl,
)
from app.shared_assets.agent_design_codec import (
    _request_checksum as _request_checksum_impl,
)
from app.shared_assets.agent_design_codec import (
    _session_summary as _session_summary_impl,
)
from app.shared_assets.agent_design_codec import (
    _session_view as _session_view_impl,
)
from app.shared_assets.agent_design_codec import (
    _stable_generation_error_message as _stable_generation_error_message_impl,
)
from app.shared_assets.agent_design_codec import (
    blueprint_checksum as blueprint_checksum_impl,
)
from app.shared_assets.agent_design_contracts import (
    AgentDesignBlueprint,
    AgentDesignBlueprintTurn,
    AgentDesignClarificationOption,
    AgentDesignClarificationRequest,
    AgentDesignClarificationResponse,
    AgentDesignClarificationTurn,
    AgentDesignCommitResult,
    AgentDesignMessage,
    AgentDesignMessageTurn,
    AgentDesignProgressItem,
    AgentDesignProgressStatus,
    AgentDesignServiceErrorCode,
    AgentDesignSessionPage,
    AgentDesignSessionSummary,
    AgentDesignSessionView,
    AgentDesignStatus,
    AgentDesignTurn,  # noqa: F401
    CancelAgentDesignSession,
    CommitAgentDesignSession,
    CreateAgentDesignSession,
    SetAgentDesignGenerationPreference,
    SubmitAgentDesignTurn,
)
from app.shared_assets.agent_design_control import AgentDesignGenerationControl
from app.shared_assets.agent_design_generation import (
    DEFAULT_GENERATION_TIMEOUT_SECONDS,
    MAX_AGENT_DESIGN_CONTEXT_ASSETS,
    REQUIRED_INTERVIEW_QUESTIONS,
    AgentDesignDraft,
    AgentDesignGenerationContext,
    AgentDesignGenerationError,
    AgentDesignGenerationRequest,
    AgentDesignGenerationResult,
    AgentDesignGenerationService,
    AllowedProjectAssetMetadata,
    CandidateResult,
    NeedsClarificationResult,
)
from app.shared_assets.agent_design_profile import (
    AgentDesignGenerationProfile,
    AgentDesignGenerationProfileUnsupported,
    agent_design_mode_profile,
    resolve_agent_design_generation_profile,
)
from app.shared_assets.agent_design_repository import (
    AgentDesignAllowedAssetRecord,
    AgentDesignRepository,
)
from app.shared_assets.agent_design_validation import (
    AGENT_DESIGN_SLUG_MAX_LENGTH,
    AGENT_DESIGN_SLUG_MIN_LENGTH,
    AGENT_DESIGN_SLUG_PATTERN,
    _valid_agent_design_slug,
)
from app.shared_assets.agent_design_validation import (
    _bounded_text as _bounded_text_impl,
)
from app.shared_assets.agent_design_validation import (
    _candidate_blueprint as _candidate_blueprint_impl,
)
from app.shared_assets.agent_design_validation import (
    _require_capability as _require_capability_impl,
)
from app.shared_assets.agent_design_validation import (
    _require_context as _require_context_impl,
)
from app.shared_assets.agent_design_validation import (
    _require_expected_revision as _require_expected_revision_impl,
)
from app.shared_assets.agent_design_validation import (
    _require_matching_operation as _require_matching_operation_impl,
)
from app.shared_assets.agent_design_validation import (
    _require_nonterminal as _require_nonterminal_impl,
)
from app.shared_assets.agent_design_validation import (
    _valid_revision as _valid_revision_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_blueprint as _validate_blueprint_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_cancel as _validate_cancel_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_commit as _validate_commit_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_create as _validate_create_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_generation_preference as _validate_generation_preference_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_idempotency_key as _validate_idempotency_key_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_turn as _validate_turn_impl,
)
from app.shared_assets.agent_design_validation import (
    _validate_uuid as _validate_uuid_impl,
)
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.errors import (
    AgentDesignConflictUnresolved,
    AgentDesignGenerationProfileStale,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AssetConflict,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.models import AgentModelSettings
from app.system_settings.execution_payload import freeze_system_model_material
from app.system_settings.model_refs import DEFAULT_MODEL_REF
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from deerflow.config.model_execution import FrozenSystemModelExecution
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)

MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT = 8
_PUBLIC_ERROR_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_DEFAULT_STALE_GENERATING_SECONDS = DEFAULT_GENERATION_TIMEOUT_SECONDS + 60.0
_GENERATION_STOP_POLL_SECONDS = 0.1
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_agent_design_operations_idempotency",
        "uq_agent_design_sessions_create_idempotency",
        "uq_agents_project_slug",
        "uq_agents_definition_id",
    }
)

DEFAULT_AGENT_MODEL_REF = DEFAULT_MODEL_REF
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


class _RepositoryFactory(Protocol):
    def __call__(self, session: AsyncSession) -> AgentDesignRepository: ...


class AgentDesignService:
    """Coordinate owner-scoped Agent Builder sessions."""

    blueprint_checksum = staticmethod(blueprint_checksum_impl)
    _agent_payload = staticmethod(_agent_payload_impl)
    _encode_session_cursor = staticmethod(_encode_session_cursor_impl)
    _decode_session_cursor = staticmethod(_decode_session_cursor_impl)
    _request_checksum = staticmethod(_request_checksum_impl)
    _jsonable = staticmethod(_jsonable_impl)
    _message_json = staticmethod(_message_json_impl)
    _progress_json = staticmethod(_progress_json_impl)
    _blueprint_json = staticmethod(_blueprint_json_impl)
    _candidate_metadata_from_json = staticmethod(_candidate_metadata_from_json_impl)
    _remaining_conflicts_after_blueprint_update = staticmethod(_remaining_conflicts_after_blueprint_update_impl)
    _blueprint_from_json = staticmethod(_blueprint_from_json_impl)
    _clarification_request = staticmethod(_clarification_request_impl)
    _clarification_json = staticmethod(_clarification_json_impl)
    _clarification_from_json = staticmethod(_clarification_from_json_impl)
    _clarification_answers = staticmethod(_clarification_answers_impl)
    _clarification_history = staticmethod(_clarification_history_impl)
    _session_view = staticmethod(_session_view_impl)
    _session_summary = staticmethod(_session_summary_impl)
    _stable_generation_error_message = staticmethod(_stable_generation_error_message_impl)
    _has_blocking_conflicts = classmethod(_has_blocking_conflicts_impl)
    _clarification_set_json = classmethod(_clarification_set_json_impl)
    _clarifications_from_json = classmethod(_clarifications_from_json_impl)
    _validate_create = classmethod(_validate_create_impl)
    _validate_turn = classmethod(_validate_turn_impl)
    _validate_generation_preference = classmethod(_validate_generation_preference_impl)
    _validate_commit = classmethod(_validate_commit_impl)
    _validate_cancel = classmethod(_validate_cancel_impl)
    _candidate_blueprint = classmethod(_candidate_blueprint_impl)
    _require_capability = classmethod(_require_capability_impl)
    _validate_blueprint = staticmethod(_validate_blueprint_impl)
    _require_context = staticmethod(_require_context_impl)
    _require_nonterminal = staticmethod(_require_nonterminal_impl)
    _require_expected_revision = staticmethod(_require_expected_revision_impl)
    _require_matching_operation = staticmethod(_require_matching_operation_impl)
    _valid_revision = staticmethod(_valid_revision_impl)
    _validate_uuid = staticmethod(_validate_uuid_impl)
    _validate_idempotency_key = staticmethod(_validate_idempotency_key_impl)
    _bounded_text = staticmethod(_bounded_text_impl)

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        generator: AgentDesignGenerationService | None = None,
        agent_service: AgentService | None = None,
        repository_factory: _RepositoryFactory = AgentDesignRepository,
        default_tool_groups_provider: Callable[[], tuple[str, ...]] | None = None,
        stale_generating_seconds: float = _DEFAULT_STALE_GENERATING_SECONDS,
        generation_control: AgentDesignGenerationControl | None = None,
    ) -> None:
        if not isinstance(stale_generating_seconds, int | float) or isinstance(stale_generating_seconds, bool) or stale_generating_seconds <= 0:
            raise ValueError("stale_generating_seconds must be positive")
        self._session_factory = session_factory
        self._generator = generator or AgentDesignGenerationService()
        self._agent_service = agent_service or AgentService(session_factory)
        self._repository_factory = repository_factory
        self._default_tool_groups_provider = default_tool_groups_provider or (lambda: DEFAULT_AGENT_TOOL_GROUPS)
        self._stale_after = timedelta(seconds=float(stale_generating_seconds))
        self._generation_control = generation_control or AgentDesignGenerationControl()

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
                    await repository.lock_session_create_scope(context)
                    existing = await repository.get_by_create_idempotency(
                        context,
                        idempotency_hash,
                        for_update=False,
                    )
                    if existing is not None:
                        if existing.create_request_checksum != request_checksum:
                            raise AssetConflict(context.request_id)
                        active_operations: tuple[AgentDesignOperationRow, ...] = ()
                        if self._is_stale_generating(existing, now=now):
                            active_operations = await repository.lock_in_progress_turn_operations(
                                context,
                                existing.id,
                            )
                            existing = await repository.get_by_create_idempotency(
                                context,
                                idempotency_hash,
                                for_update=True,
                            )
                            if existing is None or existing.create_request_checksum != request_checksum:
                                raise AssetConflict(context.request_id)
                        await self._recover_stale_generating(
                            repository,
                            context,
                            existing,
                            now=now,
                            active_operations=active_operations,
                        )
                        return self._session_view(existing)
                    if await repository.count_incomplete(context) >= MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT:
                        raise AgentDesignSessionLimitExceeded(context.request_id)
                    if await repository.project_agent_slug_exists(
                        context,
                        command.slug,
                        for_update=True,
                    ):
                        raise AgentDesignSlugConflict(context.request_id)
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
            constraint = _constraint_name(exc)
            if constraint == "uq_agents_project_slug":
                raise AgentDesignSlugConflict(context.request_id) from None
            if constraint in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AgentDesignSessionPage:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AssetValidationFailed(context.request_id)
        before_created_at, before_id = self._decode_session_cursor(
            context,
            cursor,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    rows = await repository.list_incomplete(
                        context,
                        limit=limit + 1,
                        before_created_at=before_created_at,
                        before_id=before_id,
                    )
                    selected = rows[:limit]
                    next_position = (selected[-1].created_at, selected[-1].id) if len(rows) > limit and selected else None
                    now = self._now()
                    can_recover = Capability.SHARED_ASSETS_EDIT in context.capabilities
                    resolved: list[AgentDesignSessionSummary] = []
                    for listed in selected:
                        row = listed
                        if can_recover and self._is_stale_generating(row, now=now):
                            active_operations = await repository.lock_in_progress_turn_operations(
                                context,
                                row.id,
                            )
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
                                active_operations=active_operations,
                            )
                        if row.status in (
                            AgentDesignStatus.COMPLETED.value,
                            AgentDesignStatus.CANCELLED.value,
                        ):
                            continue
                        resolved.append(self._session_summary(row))
                    next_cursor = None
                    if next_position is not None:
                        next_cursor = self._encode_session_cursor(*next_position)
                    return AgentDesignSessionPage(
                        items=tuple(resolved),
                        next_cursor=next_cursor,
                    )
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
                        for_update=False,
                    )
                    now = self._now()
                    can_recover = Capability.SHARED_ASSETS_EDIT in context.capabilities
                    active_operations: tuple[AgentDesignOperationRow, ...] = ()
                    if can_recover and self._is_stale_generating(row, now=now):
                        active_operations = await repository.lock_in_progress_turn_operations(
                            context,
                            session_id,
                        )
                        row = await repository.get(
                            context,
                            session_id,
                            for_update=True,
                        )
                    if can_recover:
                        await self._recover_stale_generating(
                            repository,
                            context,
                            row,
                            now=now,
                            active_operations=active_operations,
                        )
                    return self._session_view(row)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def get_by_created_agent(
        self,
        context: ProjectContext,
        agent_id: uuid.UUID,
    ) -> AgentDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        agent_id = self._validate_uuid(context, agent_id)
        try:
            async with self._session_factory() as session, session.begin():
                row = await self._repository_factory(session).get_by_created_agent(
                    context,
                    agent_id,
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
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "input": command.input,
                "generation_model_ref": command.generation_model_ref,
                "generation_mode": command.generation_mode,
                "thinking_enabled": command.thinking_enabled,
                "reasoning_effort": command.reasoning_effort,
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
        generation_revision, request, generation_context, operation_id, generation_profile = prepared
        started_at = time.monotonic()
        await self._append_activity(
            context,
            session_id=session_id,
            operation_id=operation_id,
            kind=AgentDesignActivityKind.TURN_ACCEPTED,
        )
        control_key = self._generation_control.key(
            context.project_id,
            str(context.user_id),
            session_id,
            operation_id,
        )
        abort_event = await self._generation_control.register(control_key)
        stop_monitor: asyncio.Task[None] | None = None
        try:
            if not abort_event.is_set() and await self._generation_should_stop(
                context,
                session_id,
                operation_id,
            ):
                abort_event.set()
            if abort_event.is_set():
                return await self._finish_generation_stopped(
                    context,
                    session_id,
                    operation_hash=operation_hash,
                    generation_revision=generation_revision,
                    duration_ms=max(
                        0,
                        round((time.monotonic() - started_at) * 1000),
                    ),
                )
            stop_monitor = asyncio.create_task(
                self._monitor_generation_stop(
                    context,
                    session_id,
                    operation_id,
                    abort_event,
                )
            )

            async def record_generation_activity(
                kind: str,
                attempt: int | None,
                payload: dict[str, object],
            ) -> None:
                try:
                    activity_kind = AgentDesignActivityKind(kind)
                    await self._append_activity(
                        context,
                        session_id=session_id,
                        operation_id=operation_id,
                        kind=activity_kind,
                        payload=payload,
                        attempt=attempt,
                    )
                except AgentDesignActivityLimitExceeded:
                    raise AgentDesignGenerationError(
                        "AGENT_DESIGN_ACTIVITY_LIMIT_EXCEEDED",
                        "Agent design activity exceeded the safety limit.",
                    ) from None

            generation_kwargs: dict[str, object] = {
                "context": generation_context,
                "activity_callback": record_generation_activity,
                "abort_event": abort_event,
            }
            if generation_profile is not None:
                generation_kwargs.update(
                    model_ref=generation_profile.model_ref,
                    model_execution=generation_profile.model_execution,
                    thinking_enabled=generation_profile.thinking_enabled,
                    reasoning_effort=generation_profile.reasoning_effort,
                )
            elif command.generation_model_ref is not None:
                generation_kwargs["model_ref"] = command.generation_model_ref
            result = await self._generator.generate(request, **generation_kwargs)
        except asyncio.CancelledError:
            if not abort_event.is_set():
                raise
            return await self._finish_generation_stopped(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
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
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
            )
        except Exception:
            return await self._finish_generation_failure(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                error_code=AgentDesignServiceErrorCode.GENERATION_UNAVAILABLE.value,
                error_message="Agent 设定生成暂时不可用，请稍后重试。",
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
            )
        else:
            return await self._finish_generation_success(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                result=result,
                duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
            )
        finally:
            if stop_monitor is not None:
                stop_monitor.cancel()
                try:
                    await stop_monitor
                except asyncio.CancelledError:
                    pass
            await self._generation_control.complete(control_key)

    async def stop_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> AgentDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
                row = await repository.get(context, session_id, for_update=True)
                if not active or row.status != AgentDesignStatus.GENERATING.value:
                    return self._session_view(row)
                operation = active[0]
                operation.stop_requested_at = self._now()
                operation_id = uuid.UUID(str(operation.id))
                await session.flush()
            control_key = self._generation_control.key(
                context.project_id,
                str(context.user_id),
                session_id,
                operation_id,
            )
            done = await self._generation_control.request_stop(control_key)
            if done is None:
                return await self._wait_for_generation_completion(
                    context,
                    session_id,
                    operation_id,
                )
            await done.wait()
            return await self.get(context, session_id)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def set_generation_preference(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SetAgentDesignGenerationPreference,
    ) -> AgentDesignSessionView:
        command = self._validate_generation_preference(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active_turns = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
                await self._require_no_cancel_in_progress(
                    repository,
                    context,
                    session_id,
                )
                row = await repository.get(context, session_id, for_update=True)
                if active_turns or row.status in {
                    AgentDesignStatus.GENERATING.value,
                    AgentDesignStatus.COMMITTING.value,
                    AgentDesignStatus.COMPLETED.value,
                    AgentDesignStatus.CANCELLED.value,
                }:
                    raise AssetConflict(context.request_id)
                profile = await self._resolve_generation_profile_values(
                    session,
                    context,
                    requested_model_ref=command.generation_model_ref,
                    requested_mode=command.generation_mode,
                    thinking_enabled=command.thinking_enabled,
                    reasoning_effort=command.reasoning_effort,
                )
                row.generation_model_ref = command.generation_model_ref
                row.generation_mode = profile.mode
                await session.flush()
                return self._session_view(row)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def list_activities(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> tuple[AgentDesignActivity, ...]:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        session_id = self._validate_uuid(context, session_id)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2_000:
            raise AssetValidationFailed(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                rows = await AgentDesignActivityRepository(session).list_after(
                    context,
                    session_id=session_id,
                    after_seq=after_seq,
                    limit=limit,
                )
                return tuple(activity_view(row) for row in rows)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

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
        checksum_payload: dict[str, object] = {
            "session_id": session_id,
            "expected_revision": command.expected_revision,
            "expected_blueprint_checksum": command.expected_blueprint_checksum,
        }
        if command.slug is not None:
            checksum_payload["slug"] = command.slug
        request_checksum = self._request_checksum(checksum_payload)
        prepared_operation_id: uuid.UUID | None = None
        try:
            prepared = await self._prepare_commit(
                context,
                session_id,
                command,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
            )
            if not isinstance(prepared, uuid.UUID):
                return prepared
            prepared_operation_id = prepared
            for kind in (
                AgentDesignActivityKind.COMMIT_ACCEPTED,
                AgentDesignActivityKind.COMMIT_VALIDATION_STARTED,
                AgentDesignActivityKind.COMMIT_VALIDATION_PASSED,
                AgentDesignActivityKind.COMMIT_PERSISTENCE_STARTED,
            ):
                await self._append_activity(
                    context,
                    session_id=session_id,
                    operation_id=prepared_operation_id,
                    kind=kind,
                )
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    active_turn_operations = await repository.lock_in_progress_turn_operations(
                        context,
                        session_id,
                    )
                    if active_turn_operations:
                        raise AssetConflict(context.request_id)
                    await self._require_no_cancel_in_progress(
                        repository,
                        context,
                        session_id,
                    )
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
                    if operation is None:
                        raise AssetConflict(context.request_id)
                    self._require_matching_operation(
                        context,
                        operation,
                        session_id=session_id,
                        request_checksum=request_checksum,
                    )
                    if operation.status == "completed" and row.status == AgentDesignStatus.COMPLETED.value:
                        return await self._committed_result(session, context, row)
                    if (
                        operation.status != "in_progress"
                        or operation.id != prepared_operation_id
                        or row.status != AgentDesignStatus.COMMITTING.value
                        or row.revision != command.expected_revision
                        or row.blueprint_json is None
                        or row.blueprint_checksum != command.expected_blueprint_checksum
                    ):
                        raise AssetConflict(context.request_id)
                    if self._has_blocking_conflicts(row.blueprint_json):
                        raise AgentDesignConflictUnresolved(context.request_id)
                    effective_slug = command.slug or row.slug
                    if not _valid_agent_design_slug(effective_slug):
                        raise AssetValidationFailed(context.request_id)
                    blueprint = self._blueprint_from_json(row.blueprint_json)
                    if await repository.project_agent_slug_exists(
                        context,
                        effective_slug,
                        for_update=True,
                    ):
                        raise AgentDesignSlugConflict(context.request_id)
                    activity_repository = AgentDesignActivityRepository(session)
                    created = await self._agent_service.create_project_from_design_in_session(
                        session,
                        context,
                        CreateAgent(
                            slug=effective_slug,
                            display_name=effective_slug,
                        ),
                        self._agent_payload(blueprint),
                    )
                    row.status = AgentDesignStatus.COMPLETED.value
                    row.slug = created.asset.slug
                    row.display_name = created.asset.display_name
                    row.created_agent_id = created.asset.id
                    row.revision += 1
                    row.progress_json = self._progress_json(AgentDesignProgressStatus.COMPLETED)
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await activity_repository.append(
                        context,
                        session_id=session_id,
                        operation_id=operation.id,
                        kind=AgentDesignActivityKind.COMMIT_PERSISTENCE_COMPLETED,
                    )
                    await activity_repository.append(
                        context,
                        session_id=session_id,
                        operation_id=operation.id,
                        kind=AgentDesignActivityKind.COMMIT_TERMINAL,
                        payload={"status": "completed"},
                    )
                    await session.flush()
                    return AgentDesignCommitResult(
                        session=self._session_view(row),
                        agent=created.asset,
                        definition=created.definition,
                    )
        except SharedAssetError:
            await self._record_commit_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                operation_id=prepared_operation_id,
            )
            raise
        except IntegrityError as exc:
            constraint = _constraint_name(exc)
            if constraint == "uq_agents_project_slug":
                public_error: SharedAssetError = AgentDesignSlugConflict(context.request_id)
            elif constraint in _CONFLICT_CONSTRAINTS:
                public_error = AssetConflict(context.request_id)
            else:
                public_error = AssetStorageUnavailable(context.request_id)
            await self._record_commit_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                operation_id=prepared_operation_id,
            )
            raise public_error from None
        except DBAPIError:
            await self._record_commit_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                operation_id=prepared_operation_id,
            )
            raise AssetStorageUnavailable(context.request_id) from None

    async def _prepare_commit(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: CommitAgentDesignSession,
        *,
        operation_hash: str,
        request_checksum: str,
    ) -> uuid.UUID | AgentDesignCommitResult:
        async with self._session_factory() as session, session.begin():
            repository = self._repository_factory(session)
            if await repository.lock_in_progress_turn_operations(
                context,
                session_id,
            ):
                raise AssetConflict(context.request_id)
            await self._require_no_cancel_in_progress(
                repository,
                context,
                session_id,
            )
            operation = await repository.get_operation(
                context,
                operation_kind="commit",
                idempotency_key_hash=operation_hash,
                for_update=True,
            )
            row = await repository.get(context, session_id, for_update=True)
            if operation is not None:
                self._require_matching_operation(
                    context,
                    operation,
                    session_id=session_id,
                    request_checksum=request_checksum,
                )
                if operation.status == "completed" and row.status == AgentDesignStatus.COMPLETED.value:
                    return await self._committed_result(session, context, row)
                raise AssetConflict(context.request_id)
            if row.status == AgentDesignStatus.COMPLETED.value:
                if row.blueprint_checksum != command.expected_blueprint_checksum or (command.slug is not None and command.slug != row.slug):
                    raise AssetConflict(context.request_id)
                return await self._committed_result(session, context, row)
            self._require_expected_revision(context, row, command.expected_revision)
            if row.status != AgentDesignStatus.PROPOSAL_READY.value or row.blueprint_json is None or row.blueprint_checksum != command.expected_blueprint_checksum:
                raise AssetConflict(context.request_id)
            if self._has_blocking_conflicts(row.blueprint_json):
                raise AgentDesignConflictUnresolved(context.request_id)
            effective_slug = command.slug or row.slug
            if not _valid_agent_design_slug(effective_slug):
                raise AssetValidationFailed(context.request_id)
            if await repository.project_agent_slug_exists(
                context,
                effective_slug,
                for_update=True,
            ):
                raise AgentDesignSlugConflict(context.request_id)
            operation = self._new_operation(
                context,
                session_id,
                kind="commit",
                idempotency_hash=operation_hash,
                request_checksum=request_checksum,
            )
            await repository.create_operation(context, operation)
            row.status = AgentDesignStatus.COMMITTING.value
            row.error_code = None
            row.error_message = None
            await session.flush()
            return uuid.UUID(str(operation.id))

    async def _record_commit_failure(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_hash: str,
        request_checksum: str,
        operation_id: uuid.UUID | None,
    ) -> None:
        """Persist a public failure terminal after the atomic commit rolls back."""

        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                operation = await repository.get_operation(
                    context,
                    operation_kind="commit",
                    idempotency_key_hash=operation_hash,
                    for_update=True,
                )
                row = await repository.get(context, session_id, for_update=True)
                if operation is not None:
                    if operation_id is None or operation.id != operation_id or operation.status != "in_progress":
                        return
                else:
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="commit",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    await repository.create_operation(context, operation)
                activity_repository = AgentDesignActivityRepository(session)
                if operation_id is None:
                    await activity_repository.append(
                        context,
                        session_id=session_id,
                        operation_id=operation.id,
                        kind=AgentDesignActivityKind.COMMIT_ACCEPTED,
                    )
                    await activity_repository.append(
                        context,
                        session_id=session_id,
                        operation_id=operation.id,
                        kind=AgentDesignActivityKind.COMMIT_VALIDATION_STARTED,
                    )
                if row.status == AgentDesignStatus.COMMITTING.value:
                    row.status = AgentDesignStatus.PROPOSAL_READY.value
                    row.error_code = AgentDesignServiceErrorCode.COMMIT_FAILED.value
                    row.error_message = "Agent 创建失败，请重试。"
                operation.status = "failed"
                operation.result_revision = row.revision
                operation.public_error_code = AgentDesignServiceErrorCode.COMMIT_FAILED.value
                await activity_repository.append(
                    context,
                    session_id=session_id,
                    operation_id=operation.id,
                    kind=AgentDesignActivityKind.COMMIT_TERMINAL,
                    payload={
                        "status": "failed",
                        "error_code": AgentDesignServiceErrorCode.COMMIT_FAILED.value,
                    },
                )
                await session.flush()
        except Exception:  # noqa: BLE001 - preserve the primary commit failure
            return

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
            active_operation_id: uuid.UUID | None = None
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active_turn_operations = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
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
                if row.status in {
                    AgentDesignStatus.CANCELLED.value,
                    AgentDesignStatus.COMPLETED.value,
                }:
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
                if active_turn_operations:
                    active_turn = active_turn_operations[0]
                    active_turn.stop_requested_at = self._now()
                    active_operation_id = uuid.UUID(str(active_turn.id))
                await session.flush()

            if active_operation_id is not None:
                control_key = self._generation_control.key(
                    context.project_id,
                    str(context.user_id),
                    session_id,
                    active_operation_id,
                )
                done = await self._generation_control.request_stop(control_key)
                if done is None:
                    await self._wait_for_generation_completion(
                        context,
                        session_id,
                        active_operation_id,
                    )
                else:
                    await done.wait()

            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active_turn_operations = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
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
                if operation is None:
                    raise AssetConflict(context.request_id)
                self._require_matching_operation(
                    context,
                    operation,
                    session_id=session_id,
                    request_checksum=request_checksum,
                )
                if operation.status == "completed":
                    return self._session_view(row)
                if operation.status != "in_progress":
                    raise AssetConflict(context.request_id)
                self._clear_cancelled_session(row)
                row.revision += 1
                self._terminalize_cancelled_turn_operations(
                    active_turn_operations,
                    result_revision=row.revision,
                )
                await repository.clear_turn_generation_profiles(
                    context,
                    session_id,
                )
                await AgentDesignActivityRepository(session).clear_session(
                    context,
                    session_id=session_id,
                )
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
    def _clear_cancelled_session(row: AgentDesignSessionRow) -> None:
        """Retain an idempotency tombstone while deleting private draft content."""

        row.slug = f"deleted-{row.id.hex}"
        row.display_name = "Deleted Agent design"
        row.status = AgentDesignStatus.CANCELLED.value
        row.messages_json = []
        row.progress_json = []
        row.active_clarification_json = None
        row.blueprint_json = None
        row.blueprint_checksum = None
        row.error_code = None
        row.error_message = None
        row.generation_model_ref = None
        row.generation_mode = None

    @staticmethod
    def _terminalize_cancelled_turn_operations(
        operations: tuple[AgentDesignOperationRow, ...],
        *,
        result_revision: int,
    ) -> None:
        for operation in operations:
            operation.status = "failed"
            operation.result_revision = result_revision
            operation.public_error_code = AgentDesignServiceErrorCode.SESSION_CANCELLED.value
            operation.requested_generation_profile_json = None
            operation.effective_generation_profile_json = None

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
            uuid.UUID,
            AgentDesignGenerationProfile | None,
        ]
    ):
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    active_operations = await repository.lock_in_progress_turn_operations(
                        context,
                        session_id,
                    )
                    await self._require_no_cancel_in_progress(
                        repository,
                        context,
                        session_id,
                    )
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
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status in {"completed", "failed", "stopped"}:
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
                                active_operations=active_operations,
                            )
                            return self._session_view(row)
                        raise AssetConflict(context.request_id)
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
                                active_operations=active_operations,
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
                        current_blueprint = self._blueprint_from_json(row.blueprint_json) if row.blueprint_json is not None else None
                        blueprint = self._validate_blueprint(
                            context,
                            command.input.blueprint,
                        )
                        assumptions, conflicts = self._candidate_metadata_from_json(
                            row.blueprint_json,
                        )
                        row.blueprint_json = self._blueprint_json(
                            blueprint,
                            assumptions=assumptions,
                            conflicts=self._remaining_conflicts_after_blueprint_update(
                                current_blueprint,
                                blueprint,
                                conflicts,
                            ),
                        )
                        row.blueprint_checksum = self.blueprint_checksum(blueprint)
                        row.status = AgentDesignStatus.PROPOSAL_READY.value
                        row.active_clarification_json = None
                        row.error_code = None
                        row.error_message = None
                        row.progress_json = self._progress_json(AgentDesignProgressStatus.COMPLETED)
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

                    ready_to_generate = self._append_turn_input(
                        context,
                        row,
                        command.input,
                        operation_id=operation.id,
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
                    # Keep the ORM row valid before the catalog SELECT below.
                    # SQLAlchemy may autoflush pending clarification changes
                    # before executing that query.
                    row.status = AgentDesignStatus.GENERATING.value
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.progress_json = self._progress_json(AgentDesignProgressStatus.RUNNING)
                    row.revision += 1
                    self._reset_operation(operation)
                    allowed_assets = await self._generation_allowed_assets(
                        repository,
                        context,
                    )
                    generation_context = AgentDesignGenerationContext(
                        allowed_assets=allowed_assets,
                        allowed_capabilities=blueprint.tool_groups,
                    )
                    generation_profile = await self._resolve_generation_profile(
                        session,
                        context,
                        row,
                        command,
                    )
                    if generation_profile is not None:
                        requested_model_ref = command.generation_model_ref or row.generation_model_ref
                        requested_mode = command.generation_mode or row.generation_mode
                        if requested_model_ref is None or requested_mode is None:
                            raise AssetStorageUnavailable(context.request_id)
                        requested_thinking, requested_effort = agent_design_mode_profile(
                            requested_mode,
                            supports_thinking=generation_profile.thinking_enabled,
                            supports_reasoning_effort=(generation_profile.reasoning_effort is not None),
                        )
                        requested_profile = {
                            "model_ref": requested_model_ref,
                            "mode": requested_mode,
                            "thinking_enabled": requested_thinking,
                            "reasoning_effort": requested_effort,
                        }
                        operation.requested_generation_profile_json = requested_profile
                        operation.effective_generation_profile_json = generation_profile.as_dict()
                        row.generation_model_ref = requested_model_ref
                        row.generation_mode = requested_mode
                    await session.flush()
                    return (
                        row.revision,
                        generation_request,
                        generation_context,
                        operation.id,
                        generation_profile,
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(context.request_id) from None
            raise AssetStorageUnavailable(context.request_id) from None
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    async def _require_no_cancel_in_progress(
        repository: AgentDesignRepository,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> None:
        if await repository.lock_in_progress_cancel_operations(
            context,
            session_id,
        ):
            raise AssetConflict(context.request_id)

    async def _append_activity(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
        kind: AgentDesignActivityKind,
        payload: dict[str, object] | None = None,
        attempt: int | None = None,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await AgentDesignActivityRepository(session).append(
                    context,
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=kind,
                    payload=payload,
                    attempt=attempt,
                )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    async def _resolve_generation_profile(
        session: AsyncSession,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        command: SubmitAgentDesignTurn,
    ) -> AgentDesignGenerationProfile | None:
        requested_model_ref = command.generation_model_ref or row.generation_model_ref
        requested_mode = command.generation_mode or row.generation_mode
        if requested_model_ref is None or requested_mode is None:
            return None
        if command.generation_mode is None:
            thinking_enabled = None
            reasoning_effort = None
        else:
            thinking_enabled = command.thinking_enabled
            reasoning_effort = command.reasoning_effort
        return await AgentDesignService._resolve_generation_profile_values(
            session,
            context,
            requested_model_ref=requested_model_ref,
            requested_mode=requested_mode,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    async def _resolve_generation_profile_values(
        session: AsyncSession,
        context: ProjectContext,
        *,
        requested_model_ref: str,
        requested_mode: str,
        thinking_enabled: bool | None,
        reasoning_effort: str | None,
    ) -> AgentDesignGenerationProfile:
        try:
            material = await SystemModelRepository(session).resolve_active_model(
                requested_model_ref,
                load_secret=True,
            )
        except SystemModelRepositoryInvariant:
            raise AssetStorageUnavailable(context.request_id) from None
        if material is None:
            raise AgentDesignGenerationProfileStale(context.request_id)
        if thinking_enabled is None:
            try:
                thinking_enabled, reasoning_effort = agent_design_mode_profile(
                    requested_mode,
                    supports_thinking=material.model.supports_thinking,
                    supports_reasoning_effort=(material.model.supports_reasoning_effort),
                )
            except AgentDesignGenerationProfileUnsupported:
                raise AgentDesignGenerationProfileStale(context.request_id) from None
        try:
            profile = resolve_agent_design_generation_profile(
                requested_model_ref=requested_model_ref,
                effective_model_ref=str(uuid.UUID(str(material.model.id))),
                mode=requested_mode,
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
                supports_thinking=material.model.supports_thinking,
                supports_reasoning_effort=(material.model.supports_reasoning_effort),
            )
            return replace(
                profile,
                model_execution=freeze_system_model_material(material),
            )
        except AgentDesignGenerationProfileUnsupported:
            raise AgentDesignGenerationProfileStale(context.request_id) from None

    @staticmethod
    async def _generation_allowed_assets(
        repository: AgentDesignRepository,
        context: ProjectContext,
    ) -> tuple[AllowedProjectAssetMetadata, ...]:
        records = await repository.list_allowed_assets(
            context,
            limit=MAX_AGENT_DESIGN_CONTEXT_ASSETS,
        )
        if any(not isinstance(record, AgentDesignAllowedAssetRecord) for record in records):
            raise AssetStorageUnavailable(context.request_id)
        try:
            ordered = sorted(
                records,
                key=lambda record: (
                    0 if record.kind == "skill" else 1,
                    0 if record.scope == "project" else 1,
                    record.slug.casefold(),
                    record.asset_id.hex,
                    record.version_id.hex,
                ),
            )[:MAX_AGENT_DESIGN_CONTEXT_ASSETS]
            return tuple(
                AllowedProjectAssetMetadata(
                    kind=record.kind,
                    scope=record.scope,
                    asset_id=record.asset_id,
                    version_id=record.version_id,
                    name=record.name,
                    slug=record.slug,
                    description=record.description[:2_000],
                    capabilities=(),
                    enabled=True,
                )
                for record in ordered
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise AssetStorageUnavailable(context.request_id) from None

    async def _finish_generation_success(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        result: AgentDesignGenerationResult,
        duration_ms: int = 0,
    ) -> AgentDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    await repository.lock_in_progress_turn_operations(
                        context,
                        session_id,
                    )
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
                    if operation.stop_requested_at is not None:
                        return await self._settle_generation_stopped(
                            session,
                            context,
                            row,
                            operation,
                            duration_ms=duration_ms,
                        )
                    if not await self._generation_profile_is_valid(
                        session,
                        operation,
                    ):
                        code = AgentDesignGenerationProfileStale.code
                        return await self._settle_generation_failed(
                            session,
                            context,
                            row,
                            operation,
                            error_code=code,
                            error_message=self._stable_generation_error_message(code),
                            duration_ms=duration_ms,
                        )
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
                        row.blueprint_json = self._blueprint_json(
                            blueprint,
                            assumptions=result.assumptions,
                            conflicts=result.conflicts,
                        )
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
                                operation_id=operation.id,
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
                    await AgentDesignActivityRepository(session).append(
                        context,
                        session_id=session_id,
                        operation_id=operation.id,
                        kind=AgentDesignActivityKind.TURN_TERMINAL,
                        payload={
                            "status": "completed",
                            "duration_ms": duration_ms,
                        },
                    )
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
        duration_ms: int = 0,
    ) -> AgentDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    await repository.lock_in_progress_turn_operations(
                        context,
                        session_id,
                    )
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
                    if operation.stop_requested_at is not None:
                        return await self._settle_generation_stopped(
                            session,
                            context,
                            row,
                            operation,
                            duration_ms=duration_ms,
                        )
                    return await self._settle_generation_failed(
                        session,
                        context,
                        row,
                        operation,
                        error_code=error_code,
                        error_message=error_message,
                        duration_ms=duration_ms,
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    async def _generation_profile_is_valid(
        session: AsyncSession,
        operation: AgentDesignOperationRow,
    ) -> bool:
        del session
        requested = operation.requested_generation_profile_json
        effective = operation.effective_generation_profile_json
        if requested is None and effective is None:
            return True
        if not isinstance(requested, dict) or not isinstance(effective, dict):
            return False
        try:
            requested_model_ref = str(requested["model_ref"])
            execution_raw = effective["model_execution"]
            if not isinstance(execution_raw, dict):
                return False
            generation_raw = execution_raw["secret_generation_id"]
            execution = FrozenSystemModelExecution(
                model_config_id=uuid.UUID(str(execution_raw["model_config_id"])),
                provider_payload=execution_raw["provider_payload"],
                payload_checksum=str(execution_raw["payload_checksum"]),
                secret_generation_id=(uuid.UUID(str(generation_raw)) if generation_raw is not None else None),
                secret_envelope_digest=(str(execution_raw["secret_envelope_digest"]) if execution_raw["secret_envelope_digest"] is not None else None),
            )
            provider_payload = execution.provider_payload
            resolved = resolve_agent_design_generation_profile(
                requested_model_ref=requested_model_ref,
                effective_model_ref=str(execution.model_config_id),
                mode=requested.get("mode"),
                thinking_enabled=requested.get("thinking_enabled"),
                reasoning_effort=requested.get("reasoning_effort"),
                supports_thinking=provider_payload["supports_thinking"],
                supports_reasoning_effort=provider_payload["supports_reasoning_effort"],
            )
            resolved = replace(
                resolved,
                model_execution=execution,
            )
        except (
            AgentDesignGenerationProfileUnsupported,
            KeyError,
            TypeError,
            ValueError,
        ):
            return False
        return resolved.as_dict() == effective

    async def _settle_generation_failed(
        self,
        session: AsyncSession,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        operation: AgentDesignOperationRow,
        *,
        error_code: str,
        error_message: str,
        duration_ms: int,
    ) -> AgentDesignSessionView:
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
                operation_id=operation.id,
            ),
        ]
        row.revision += 1
        operation.status = "failed"
        operation.result_revision = row.revision
        operation.public_error_code = error_code
        await AgentDesignActivityRepository(session).append(
            context,
            session_id=uuid.UUID(str(row.id)),
            operation_id=uuid.UUID(str(operation.id)),
            kind=AgentDesignActivityKind.TURN_TERMINAL,
            payload={
                "status": "failed",
                "duration_ms": duration_ms,
                "error_code": error_code,
            },
        )
        await session.flush()
        return self._session_view(row)

    async def _finish_generation_stopped(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        duration_ms: int = 0,
    ) -> AgentDesignSessionView:
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
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
                return await self._settle_generation_stopped(
                    session,
                    context,
                    row,
                    operation,
                    duration_ms=duration_ms,
                )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _generation_should_stop(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> bool:
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
                operation = next(
                    (item for item in active if uuid.UUID(str(item.id)) == operation_id),
                    None,
                )
                return operation is None or operation.stop_requested_at is not None
        except (SharedAssetError, DBAPIError):
            return True

    async def _monitor_generation_stop(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
        abort_event: asyncio.Event,
    ) -> None:
        while not abort_event.is_set():
            await asyncio.sleep(_GENERATION_STOP_POLL_SECONDS)
            if await self._generation_should_stop(
                context,
                session_id,
                operation_id,
            ):
                abort_event.set()
                return

    async def _wait_for_generation_completion(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> AgentDesignSessionView:
        while True:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
                row = await repository.get(context, session_id, for_update=True)
                if all(uuid.UUID(str(item.id)) != operation_id for item in active):
                    return self._session_view(row)
            await asyncio.sleep(_GENERATION_STOP_POLL_SECONDS)

    async def _settle_generation_stopped(
        self,
        session: AsyncSession,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        operation: AgentDesignOperationRow,
        *,
        duration_ms: int,
    ) -> AgentDesignSessionView:
        row.status = AgentDesignStatus.PROPOSAL_READY.value if row.blueprint_json is not None else AgentDesignStatus.INTERVIEWING.value
        row.active_clarification_json = None
        row.error_code = None
        row.error_message = None
        row.progress_json = self._progress_json(
            AgentDesignProgressStatus.COMPLETED if row.blueprint_json is not None else AgentDesignProgressStatus.PENDING,
        )
        row.revision += 1
        operation.status = "stopped"
        operation.result_revision = row.revision
        operation.public_error_code = None
        await AgentDesignActivityRepository(session).append(
            context,
            session_id=uuid.UUID(str(row.id)),
            operation_id=uuid.UUID(str(operation.id)),
            kind=AgentDesignActivityKind.TURN_TERMINAL,
            payload={
                "status": "stopped",
                "duration_ms": duration_ms,
            },
        )
        await session.flush()
        return self._session_view(row)

    async def _recover_stale_generating(
        self,
        repository: AgentDesignRepository,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        *,
        now: datetime,
        active_operations: tuple[AgentDesignOperationRow, ...],
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
        activity_repository = AgentDesignActivityRepository(repository.session)
        for operation in active_operations:
            operation.status = "failed"
            operation.result_revision = row.revision
            operation.public_error_code = code
            await activity_repository.append(
                context,
                session_id=uuid.UUID(str(row.id)),
                operation_id=uuid.UUID(str(operation.id)),
                kind=AgentDesignActivityKind.TURN_TERMINAL,
                payload={"status": "failed", "error_code": code},
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
        if row.created_agent_id is None:
            raise AssetConflict(context.request_id)
        repository = AgentRepository(session)
        asset = await repository.get_project_asset(
            context,
            row.created_agent_id,
        )
        definition = await repository.get_definition(asset)
        return AgentDesignCommitResult(
            session=self._session_view(row),
            agent=AgentService._asset_view(asset),
            definition=AgentService._definition_view(definition),
        )

    def _append_turn_input(
        self,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        turn: AgentDesignMessageTurn | AgentDesignClarificationTurn,
        *,
        operation_id: uuid.UUID | None = None,
    ) -> bool:
        if isinstance(turn, AgentDesignMessageTurn):
            if row.active_clarification_json is not None:
                raise AssetConflict(context.request_id)
            content = turn.message
            messages = [
                self._message_json(
                    "user",
                    content,
                    now=self._now(),
                    authoritative_brief=(row.blueprint_json is None and not self._clarification_answers(row)),
                    operation_id=operation_id,
                )
            ]
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
                    operation_id=operation_id,
                ),
                self._message_json(
                    "user",
                    content,
                    now=self._now(),
                    clarification_request_id=turn.response.request_id,
                    clarification_question=matching.question,
                    operation_id=operation_id,
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
        brief = blueprint.description
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
            brief = turn.message
        return AgentDesignGenerationRequest(
            agent_name=row.display_name,
            brief=brief,
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
            skill_refs=(),
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
        skill_refs, mcp_version_ids = await AgentRepository(session).list_enabled_system_dependencies(context)
        return replace(
            self._default_blueprint(description),
            skill_refs=skill_refs,
            mcp_version_ids=mcp_version_ids,
        )

    @staticmethod
    def _first_user_message(row: AgentDesignSessionRow) -> str:
        for message in reversed(row.messages_json):
            if message.get("role") == "user" and message.get("authoritative_brief") is True:
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        for message in reversed(row.messages_json):
            if message.get("role") == "user" and "clarification_request_id" not in message:
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        raise AssetValidationFailed("unknown")

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
    def _idempotency_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

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
    "AgentDesignSessionPage",
    "AgentDesignSessionSummary",
    "AgentDesignSessionView",
    "AgentDesignStatus",
    "AGENT_DESIGN_SLUG_MAX_LENGTH",
    "AGENT_DESIGN_SLUG_MIN_LENGTH",
    "AGENT_DESIGN_SLUG_PATTERN",
    "MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT",
    "CancelAgentDesignSession",
    "CommitAgentDesignSession",
    "CreateAgentDesignSession",
    "DEFAULT_AGENT_MODEL_REF",
    "DEFAULT_AGENT_TOOL_GROUPS",
    "SetAgentDesignGenerationPreference",
    "SubmitAgentDesignTurn",
]
