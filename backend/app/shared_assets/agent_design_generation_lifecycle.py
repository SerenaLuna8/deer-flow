"""Agent Builder model-turn execution, stop coordination, and stale recovery.

``AgentDesignService`` composes exactly one instance of this collaborator.  It
owns the generation tail of a turn: preparing generated-default state inside
the caller's transaction, running the model call without an open transaction,
settling success/failure/stopped terminals atomically, polling for stop
requests, and recovering stale generating sessions.  Session admission, manual
Blueprint updates, Commit, and Cancel remain on the Service.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.agent_design_activity import (
    AgentDesignActivityKind,
    AgentDesignActivityRepository,
)
from app.shared_assets.agent_design_codec import (
    _blueprint_from_json,
    _blueprint_json,
    _clarification_answers,
    _clarification_from_json,
    _clarification_history,
    _clarification_json,
    _clarification_request,
    _clarifications_from_json,
    _message_json,
    _progress_json,
    _session_view,
    _stable_generation_error_message,
    blueprint_checksum,
)
from app.shared_assets.agent_design_contracts import (
    AgentDesignBlueprint,
    AgentDesignClarificationResponse,
    AgentDesignClarificationTurn,
    AgentDesignMessageTurn,
    AgentDesignProgressStatus,
    AgentDesignServiceErrorCode,
    AgentDesignSessionView,
    AgentDesignStatus,
    SubmitAgentDesignTurn,
)
from app.shared_assets.agent_design_control import AgentDesignGenerationControl
from app.shared_assets.agent_design_generation import (
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
    _candidate_blueprint,
    _validate_blueprint,
)
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.errors import (
    AgentDesignGenerationProfileStale,
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

ActivityCallback = Callable[[str, int | None, dict[str, object]], Awaitable[None]]

_PUBLIC_ERROR_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_GENERATION_STOP_POLL_SECONDS = 0.1
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_agent_design_operations_idempotency",
        "uq_agent_design_sessions_create_idempotency",
        "uq_agents_project_slug",
        "uq_agents_definition_id",
    }
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


def _reset_operation(operation: AgentDesignOperationRow) -> None:
    operation.status = "in_progress"
    operation.result_revision = None
    operation.public_error_code = None


class AgentDesignGenerationLifecycle:
    """One concrete generation collaborator; composed by ``AgentDesignService``."""

    # ``cls``-style codec/validation helpers resolve their siblings through the
    # collaborator exactly as they do through the Service.
    _clarification_from_json = staticmethod(_clarification_from_json)
    _clarifications_from_json = classmethod(_clarifications_from_json)
    _validate_blueprint = staticmethod(_validate_blueprint)
    _candidate_blueprint = classmethod(_candidate_blueprint)

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        generator: AgentDesignGenerationService,
        repository_factory: Callable[[AsyncSession], AgentDesignRepository],
        default_tool_groups_provider: Callable[[], tuple[str, ...]],
        stale_after: timedelta,
        generation_control: AgentDesignGenerationControl,
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._generator = generator
        self._repository_factory = repository_factory
        self._default_tool_groups_provider = default_tool_groups_provider
        self._stale_after = stale_after
        self._generation_control = generation_control
        self._clock = clock

    async def prepare_generation_in_transaction(
        self,
        session: AsyncSession,
        repository: AgentDesignRepository,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        operation: AgentDesignOperationRow,
        command: SubmitAgentDesignTurn,
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
        """Record the generation input on an already-open transaction.

        Precondition: ``command.input`` is not an ``AgentDesignBlueprintTurn``.
        The caller owns ``session``; this method never begins or commits.
        """

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
            row.progress_json = _progress_json(
                AgentDesignProgressStatus.PENDING,
            )
            row.revision += 1
            operation.status = "completed"
            operation.result_revision = row.revision
            operation.public_error_code = None
            await session.flush()
            return _session_view(row)
        blueprint = _blueprint_from_json(row.blueprint_json) if row.blueprint_json is not None else self._default_blueprint(self._first_user_message(row))
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
        row.progress_json = _progress_json(AgentDesignProgressStatus.RUNNING)
        row.revision += 1
        _reset_operation(operation)
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

    async def run_prepared_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        request: AgentDesignGenerationRequest,
        generation_context: AgentDesignGenerationContext,
        operation_id: uuid.UUID,
        generation_profile: AgentDesignGenerationProfile | None,
        requested_model_ref: str | None,
        started_at: float,
        activity_callback: ActivityCallback,
    ) -> AgentDesignSessionView:
        """Execute one accepted model turn and settle its terminal atomically."""

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

            generation_kwargs: dict[str, object] = {
                "context": generation_context,
                "activity_callback": activity_callback,
                "abort_event": abort_event,
            }
            if generation_profile is not None:
                generation_kwargs.update(
                    model_ref=generation_profile.model_ref,
                    model_execution=generation_profile.model_execution,
                    thinking_enabled=generation_profile.thinking_enabled,
                    reasoning_effort=generation_profile.reasoning_effort,
                )
            elif requested_model_ref is not None:
                generation_kwargs["model_ref"] = requested_model_ref
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
                error_message=_stable_generation_error_message(code),
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
        *,
        refresh: Callable[[ProjectContext, uuid.UUID], Awaitable[AgentDesignSessionView]],
    ) -> AgentDesignSessionView:
        """Request a stop for the active turn and return the settled view."""

        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                active = await repository.lock_in_progress_turn_operations(
                    context,
                    session_id,
                )
                row = await repository.get(context, session_id, for_update=True)
                if not active or row.status != AgentDesignStatus.GENERATING.value:
                    return _session_view(row)
                operation = active[0]
                operation.stop_requested_at = self._clock()
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
            return await refresh(context, session_id)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def request_stop_and_wait(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> None:
        """Stop an in-flight generation and wait until it has settled."""

        control_key = self._generation_control.key(
            context.project_id,
            str(context.user_id),
            session_id,
            operation_id,
        )
        done = await self._generation_control.request_stop(control_key)
        if done is None:
            await self._wait_for_generation_completion(
                context,
                session_id,
                operation_id,
            )
        else:
            await done.wait()

    async def resolve_generation_profile_values(
        self,
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

    async def recover_stale_generating(
        self,
        repository: AgentDesignRepository,
        context: ProjectContext,
        row: AgentDesignSessionRow,
        *,
        now: datetime,
        active_operations: tuple[AgentDesignOperationRow, ...],
    ) -> bool:
        if not self.is_stale_generating(row, now=now):
            return False
        code = AgentDesignServiceErrorCode.GENERATION_INTERRUPTED.value
        row.status = AgentDesignStatus.FAILED.value
        row.error_code = code
        row.error_message = "上一次生成已中断，请重新发送你的要求。"
        row.progress_json = _progress_json(AgentDesignProgressStatus.FAILED)
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

    def is_stale_generating(
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

    async def _resolve_generation_profile(
        self,
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
        return await self.resolve_generation_profile_values(
            session,
            context,
            requested_model_ref=requested_model_ref,
            requested_mode=requested_mode,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )

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
                            error_message=_stable_generation_error_message(code),
                            duration_ms=duration_ms,
                        )
                    if isinstance(result, NeedsClarificationResult):
                        if len(result.questions) != 1:
                            raise AssetValidationFailed(context.request_id)
                        question_number = len(_clarification_history(row)) + 1
                        if question_number > REQUIRED_INTERVIEW_QUESTIONS:
                            raise AssetValidationFailed(context.request_id)
                        clarification = _clarification_request(
                            result.questions[0],
                            index=question_number,
                            total=REQUIRED_INTERVIEW_QUESTIONS,
                        )
                        row.status = AgentDesignStatus.AWAITING_CLARIFICATION.value
                        row.active_clarification_json = _clarification_json(clarification)
                        row.progress_json = _progress_json(AgentDesignProgressStatus.PENDING)
                    elif isinstance(result, CandidateResult):
                        current = (
                            _blueprint_from_json(row.blueprint_json)
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
                        row.blueprint_json = _blueprint_json(
                            blueprint,
                            assumptions=result.assumptions,
                            conflicts=result.conflicts,
                        )
                        row.blueprint_checksum = blueprint_checksum(blueprint)
                        row.status = AgentDesignStatus.PROPOSAL_READY.value
                        row.active_clarification_json = None
                        row.progress_json = _progress_json(AgentDesignProgressStatus.COMPLETED)
                        summary = "已生成 AGENTS.md、SOUL.md、IDENTITY.md 和 USER.md 的完整设定，请确认或继续调整。"
                        if result.conflicts:
                            summary = f"{summary} 另有 {len(result.conflicts)} 项设定冲突需要你在预览中确认。"
                        row.messages_json = [
                            *row.messages_json,
                            _message_json(
                                "assistant",
                                summary,
                                now=self._clock(),
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
                    return _session_view(row)
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
                        return _session_view(row)
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
        row.progress_json = _progress_json(AgentDesignProgressStatus.FAILED)
        row.messages_json = [
            *row.messages_json,
            _message_json(
                "assistant",
                error_message,
                now=self._clock(),
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
        return _session_view(row)

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
                    return _session_view(row)
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
                    return _session_view(row)
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
        row.progress_json = _progress_json(
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
        return _session_view(row)

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
                _message_json(
                    "user",
                    content,
                    now=self._clock(),
                    authoritative_brief=(row.blueprint_json is None and not _clarification_answers(row)),
                    operation_id=operation_id,
                )
            ]
            ready_to_generate = True
        elif isinstance(turn, AgentDesignClarificationTurn):
            active = row.active_clarification_json
            if row.status != AgentDesignStatus.AWAITING_CLARIFICATION.value or not isinstance(active, Mapping):
                raise AssetConflict(context.request_id)
            clarifications = self._clarifications_from_json(active)
            answered = _clarification_answers(row)
            pending = tuple(request for request in clarifications if request.request_id not in answered)
            matching = pending[0] if pending else None
            if matching is None or matching.source != turn.response.source or matching.request_id != turn.response.request_id:
                raise AssetConflict(context.request_id)
            matching_json = _clarification_json(matching)
            self._require_matching_clarification_response(
                context,
                matching_json,
                turn.response,
            )
            content = turn.response.value
            messages = [
                _message_json(
                    "assistant",
                    matching.question,
                    now=self._clock(),
                    operation_id=operation_id,
                ),
                _message_json(
                    "user",
                    content,
                    now=self._clock(),
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
        answers = _clarification_answers(row)
        interview_history = _clarification_history(row)
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
            model_ref=DEFAULT_MODEL_REF,
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
