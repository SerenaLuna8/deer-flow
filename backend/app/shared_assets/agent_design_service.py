"""Project-scoped conversational Agent design orchestration.

The service deliberately separates each model-backed turn into two database
transactions.  The first transaction durably records the user input and marks
the session as generating.  The model call happens without an open database
transaction, and the second transaction applies only the validated result.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

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
    AgentDesignGenerationContext,
    AgentDesignGenerationError,
    AgentDesignGenerationRequest,
    AgentDesignGenerationService,
)
from app.shared_assets.agent_design_generation_lifecycle import (
    _CONFLICT_CONSTRAINTS,
    AgentDesignGenerationLifecycle,
    _constraint_name,
)
from app.shared_assets.agent_design_generation_lifecycle import (
    _reset_operation as _reset_operation_impl,
)
from app.shared_assets.agent_design_profile import (
    AgentDesignGenerationProfile,
)
from app.shared_assets.agent_design_repository import (
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
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AssetConflict,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.system_settings.model_refs import DEFAULT_MODEL_REF
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)

MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT = 8
_DEFAULT_STALE_GENERATING_SECONDS = DEFAULT_GENERATION_TIMEOUT_SECONDS + 60.0
DEFAULT_AGENT_MODEL_REF = DEFAULT_MODEL_REF
DEFAULT_AGENT_TOOL_GROUPS: tuple[str, ...] = (
    "web",
    "file:read",
    "file:write",
    "bash",
    "task",
)


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
    _generation_request = classmethod(AgentDesignGenerationLifecycle.__dict__["_generation_request"].__func__)
    _generation_profile_is_valid = staticmethod(AgentDesignGenerationLifecycle.__dict__["_generation_profile_is_valid"].__func__)
    _first_user_message = staticmethod(AgentDesignGenerationLifecycle.__dict__["_first_user_message"].__func__)
    _require_matching_clarification_response = staticmethod(AgentDesignGenerationLifecycle.__dict__["_require_matching_clarification_response"].__func__)
    _reset_operation = staticmethod(_reset_operation_impl)

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
        self._generation_lifecycle = AgentDesignGenerationLifecycle(
            self._session_factory,
            generator=self._generator,
            repository_factory=self._repository_factory,
            default_tool_groups_provider=self._default_tool_groups_provider,
            stale_after=self._stale_after,
            generation_control=self._generation_control,
            clock=lambda: self._now(),
        )

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
                        if self._generation_lifecycle.is_stale_generating(existing, now=now):
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
                        await self._generation_lifecycle.recover_stale_generating(
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
                        if can_recover and self._generation_lifecycle.is_stale_generating(row, now=now):
                            active_operations = await repository.lock_in_progress_turn_operations(
                                context,
                                row.id,
                            )
                            row = await repository.get(
                                context,
                                row.id,
                                for_update=True,
                            )
                            await self._generation_lifecycle.recover_stale_generating(
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
                    if can_recover and self._generation_lifecycle.is_stale_generating(row, now=now):
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
                        await self._generation_lifecycle.recover_stale_generating(
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

        return await self._generation_lifecycle.run_prepared_turn(
            context,
            session_id,
            operation_hash=operation_hash,
            generation_revision=generation_revision,
            request=request,
            generation_context=generation_context,
            operation_id=operation_id,
            generation_profile=generation_profile,
            requested_model_ref=command.generation_model_ref,
            started_at=started_at,
            activity_callback=record_generation_activity,
        )

    async def stop_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> AgentDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        return await self._generation_lifecycle.stop_turn(
            context,
            session_id,
            refresh=self.get,
        )

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
                profile = await self._generation_lifecycle.resolve_generation_profile_values(
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
                await self._generation_lifecycle.request_stop_and_wait(
                    context,
                    session_id,
                    active_operation_id,
                )

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
                            if not self._generation_lifecycle.is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            await self._generation_lifecycle.recover_stale_generating(
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
                            if not self._generation_lifecycle.is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            await self._generation_lifecycle.recover_stale_generating(
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

                    return await self._generation_lifecycle.prepare_generation_in_transaction(
                        session,
                        repository,
                        context,
                        row,
                        operation,
                        command,
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
        return self._generation_lifecycle._append_turn_input(
            context,
            row,
            turn,
            operation_id=operation_id,
        )

    def _default_blueprint(self, description: str) -> AgentDesignBlueprint:
        return self._generation_lifecycle._default_blueprint(description)

    async def _default_blueprint_with_system_dependencies(
        self,
        session: AsyncSession,
        context: ProjectContext,
        description: str,
    ) -> AgentDesignBlueprint:
        return await self._generation_lifecycle._default_blueprint_with_system_dependencies(
            session,
            context,
            description,
        )

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
