from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.errors import (
    AutomationActiveRun,
    AutomationConflict,
    AutomationError,
    AutomationForbidden,
    AutomationInvalid,
    AutomationNotFound,
    AutomationOnceExpired,
    AutomationUnavailable,
    AutomationVersionConflict,
)
from app.automations.models import (
    AutomationChanges,
    AutomationCreate,
    AutomationView,
)
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.executable_agent import require_executable_agent
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedAgentSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.scheduled_task_runs import ScheduledTaskRunRepository
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from deerflow.scheduler.schedules import next_scheduled_occurrence

_ACTIVE_EXECUTION_STATUSES = frozenset({"launching", "running"})
_MAX_EXPECTED_VERSION = 2**63 - 1


class ProjectAutomationService:
    """Project-and-owner scoped Automation definition lifecycle."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        *,
        min_once_delay_seconds: int = 60,
    ) -> None:
        if type(min_once_delay_seconds) is not int or min_once_delay_seconds < 0:
            raise ValueError("min_once_delay_seconds must be a non-negative integer")
        self._session_factory = session_factory
        self._clock = clock
        self._min_once_delay_seconds = min_once_delay_seconds
        self._revalidator = PrivateWorkRevalidator()
        self._resolver = ProjectAssetResolver(session_factory)

    async def create(
        self,
        context: PrivateWorkContext,
        command: AutomationCreate,
    ) -> AutomationView:
        context = self._issued_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                command = self._validated_create(context, command)
                await self._validate_target(
                    session,
                    context,
                    current,
                    context_mode=command.context_mode,
                    thread_id=command.thread_id,
                    agent_asset_id=command.agent_asset_id,
                    agent_scope=command.agent_scope,
                )
                now = self._now(context.request_id)
                next_run_at = self._next_occurrence(
                    context.request_id,
                    command.schedule_type,
                    command.schedule_spec,
                    command.timezone,
                    now,
                )
                if next_run_at is None:
                    raise AutomationInvalid(context.request_id)
                self._validate_once_delay(
                    context.request_id,
                    command.schedule_type,
                    next_run_at,
                    now,
                )
                record = await ScheduledTaskRepository(session).create(
                    context.resource_scope,
                    ScheduledTaskCreate(
                        task_id=f"task-{uuid.uuid4().hex}",
                        thread_id=command.thread_id,
                        context_mode=command.context_mode,
                        agent_asset_id=command.agent_asset_id,
                        agent_scope=command.agent_scope,
                        title=command.title,
                        prompt=command.prompt,
                        schedule_type=command.schedule_type,
                        schedule_spec=dict(command.schedule_spec),
                        timezone=command.timezone,
                        next_run_at=next_run_at,
                    ),
                )
            return self._view(record)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def get(
        self,
        context: PrivateWorkContext,
        task_id: str,
    ) -> AutomationView:
        context = self._issued_context(context)
        self._validate_task_id(context.request_id, task_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                record = await ScheduledTaskRepository(session).get(
                    context.resource_scope,
                    task_id,
                )
                if record is None:
                    raise AutomationNotFound(context.request_id)
            return self._view(record)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def list(
        self,
        context: PrivateWorkContext,
        limit: int,
        offset: int,
        thread_id: str | None = None,
    ) -> tuple[AutomationView, ...]:
        context = self._issued_context(context)
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
            raise AutomationInvalid(context.request_id)
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 64):
            raise AutomationInvalid(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                repository = ScheduledTaskRepository(session)
                if thread_id is None:
                    records = await repository.list(
                        context.resource_scope,
                        limit=limit,
                        offset=offset,
                    )
                else:
                    records = await repository.list_by_thread(
                        context.resource_scope,
                        thread_id,
                        limit=limit,
                        offset=offset,
                    )
            return tuple(self._view(record) for record in records)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def update(
        self,
        context: PrivateWorkContext,
        task_id: str,
        changes: AutomationChanges,
    ) -> AutomationView:
        context = self._issued_context(context)
        self._validate_task_id(context.request_id, task_id)
        changes = self._validated_changes(context.request_id, changes)
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    lock=True,
                )
                task = await self._lock_mutable(
                    session,
                    context,
                    task_id,
                    changes.expected_version,
                )
                now = self._now(context.request_id)
                values = self._update_values(
                    context.request_id,
                    task,
                    changes,
                    now,
                )
                await self._prepare_mutation(
                    session,
                    context,
                    task.id,
                    error_code="AUTOMATION_UPDATED",
                    now=now,
                )
                await self._validate_target_record(session, context, current, task)
                updated = await ScheduledTaskRepository(session).update(
                    context.resource_scope,
                    task.id,
                    expected_version=changes.expected_version,
                    values=values,
                )
                if updated is None:
                    raise AutomationVersionConflict(context.request_id)
            return self._view(updated)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def pause(
        self,
        context: PrivateWorkContext,
        task_id: str,
        expected_version: int,
    ) -> AutomationView:
        return await self._set_paused(
            context,
            task_id,
            expected_version,
            error_code="AUTOMATION_PAUSED",
        )

    async def resume(
        self,
        context: PrivateWorkContext,
        task_id: str,
        expected_version: int,
    ) -> AutomationView:
        context = self._issued_context(context)
        self._validate_task_id(context.request_id, task_id)
        self._validate_expected_version(context.request_id, expected_version)
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                task = await self._lock_mutable(
                    session,
                    context,
                    task_id,
                    expected_version,
                )
                if task.status != "paused":
                    raise AutomationConflict(context.request_id)
                now = self._now(context.request_id)
                await self._prepare_mutation(
                    session,
                    context,
                    task.id,
                    error_code="AUTOMATION_RESUMED",
                    now=now,
                )
                await self._validate_target_record(session, context, current, task)
                next_run_at = self._next_occurrence(
                    context.request_id,
                    task.schedule_type,
                    task.schedule_spec,
                    task.timezone,
                    now,
                )
                if next_run_at is None:
                    raise AutomationOnceExpired(context.request_id)
                updated = await ScheduledTaskRepository(session).update(
                    context.resource_scope,
                    task.id,
                    expected_version=expected_version,
                    values={"status": "enabled", "next_run_at": next_run_at},
                )
                if updated is None:
                    raise AutomationVersionConflict(context.request_id)
            return self._view(updated)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def delete(
        self,
        context: PrivateWorkContext,
        task_id: str,
        expected_version: int,
    ) -> None:
        context = self._issued_context(context)
        self._validate_task_id(context.request_id, task_id)
        self._validate_expected_version(context.request_id, expected_version)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    lock=True,
                )
                task = await self._lock_mutable(
                    session,
                    context,
                    task_id,
                    expected_version,
                )
                now = self._now(context.request_id)
                await self._prepare_mutation(
                    session,
                    context,
                    task.id,
                    error_code="AUTOMATION_DELETED",
                    now=now,
                )
                deleted = await ScheduledTaskRepository(session).soft_delete(
                    context.resource_scope,
                    task.id,
                    expected_version=expected_version,
                    deleted_at=now,
                )
                if not deleted:
                    raise AutomationVersionConflict(context.request_id)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def _set_paused(
        self,
        context: PrivateWorkContext,
        task_id: str,
        expected_version: int,
        *,
        error_code: str,
    ) -> AutomationView:
        context = self._issued_context(context)
        self._validate_task_id(context.request_id, task_id)
        self._validate_expected_version(context.request_id, expected_version)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.AUTOMATION_MANAGE_OWN,
                    lock=True,
                )
                task = await self._lock_mutable(
                    session,
                    context,
                    task_id,
                    expected_version,
                )
                if task.status != "enabled":
                    raise AutomationConflict(context.request_id)
                now = self._now(context.request_id)
                await self._prepare_mutation(
                    session,
                    context,
                    task.id,
                    error_code=error_code,
                    now=now,
                )
                updated = await ScheduledTaskRepository(session).update(
                    context.resource_scope,
                    task.id,
                    expected_version=expected_version,
                    values={"status": "paused", "next_run_at": None},
                )
                if updated is None:
                    raise AutomationVersionConflict(context.request_id)
            return self._view(updated)
        except Exception as error:
            self._raise_mapped(error, context.request_id)

    async def _lock_mutable(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
        expected_version: int,
    ) -> ScheduledTaskRecord:
        self._validate_expected_version(context.request_id, expected_version)
        task = await ScheduledTaskRepository(session).lock_active(
            context.resource_scope,
            task_id,
        )
        if task is None:
            raise AutomationNotFound(context.request_id)
        if task.version != expected_version:
            raise AutomationVersionConflict(context.request_id)
        return task

    async def _prepare_mutation(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        task_id: str,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        repository = ScheduledTaskRunRepository(session)
        active = await repository.lock_active_by_task(context.resource_scope, task_id)
        if any(item.status in _ACTIVE_EXECUTION_STATUSES for item in active):
            raise AutomationActiveRun(context.request_id)
        await repository.cancel_queued(
            context.resource_scope,
            task_id,
            now=now if now is not None else self._now(context.request_id),
            error_code=error_code,
        )

    async def _validate_target_record(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        current: ProjectContext,
        task: ScheduledTaskRecord,
    ) -> None:
        await self._validate_target(
            session,
            context,
            current,
            context_mode=task.context_mode,
            thread_id=task.thread_id,
            agent_asset_id=task.agent_asset_id,
            agent_scope=task.agent_scope,
        )

    async def _validate_target(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        current: ProjectContext,
        *,
        context_mode: str,
        thread_id: str | None,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> ResolvedAgentSnapshot:
        if context_mode == "fresh_thread_per_run":
            if thread_id is not None:
                raise AutomationInvalid(context.request_id)
        elif context_mode == "reuse_thread":
            if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 64:
                raise AutomationInvalid(context.request_id)
            thread = await PrivateThreadRepository(session).get(
                scope=context.resource_scope,
                thread_id=thread_id,
                lock=True,
            )
            if thread is None or thread.agent_asset_id != agent_asset_id or thread.agent_scope != agent_scope:
                raise AutomationNotFound(context.request_id)
        else:
            raise AutomationInvalid(context.request_id)

        await require_executable_agent(
            session,
            context,
            ThreadAgentRef(agent_asset_id, agent_scope),
        )
        resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
            session,
            current,
            AssetSelection(AssetKind.AGENT, agent_asset_id),
        )
        if type(resolved) is not ResolvedAgentSnapshot or resolved.asset_id != agent_asset_id or resolved.scope.value != agent_scope:
            raise AutomationNotFound(context.request_id)
        return resolved

    def _update_values(
        self,
        request_id: str,
        task: ScheduledTaskRecord,
        changes: AutomationChanges,
        now: datetime,
    ) -> dict[str, object]:
        values: dict[str, object] = {}
        if changes.title is not None:
            values["title"] = changes.title
        if changes.prompt is not None:
            values["prompt"] = changes.prompt

        schedule_fields_present = changes.schedule_spec is not None or changes.timezone is not None
        if schedule_fields_present:
            schedule_spec: Mapping[str, object] = changes.schedule_spec if changes.schedule_spec is not None else task.schedule_spec
            timezone = changes.timezone if changes.timezone is not None else task.timezone
            normalized = self._normalized_schedule(
                request_id,
                task.schedule_type,
                schedule_spec,
                timezone,
            )
            schedule_changed = dict(normalized) != dict(task.schedule_spec) or timezone != task.timezone
            if not schedule_changed:
                return values
            schedule_spec = normalized
            if dict(normalized) != dict(task.schedule_spec):
                values["schedule_spec"] = dict(normalized)
            if timezone != task.timezone:
                values["timezone"] = timezone
            next_run_at = self._next_occurrence(
                request_id,
                task.schedule_type,
                schedule_spec,
                timezone,
                now,
            )
            if next_run_at is None:
                raise AutomationOnceExpired(request_id)
            self._validate_once_delay(
                request_id,
                task.schedule_type,
                next_run_at,
                now,
            )
            values["next_run_at"] = next_run_at if task.status == "enabled" else None
        return values

    def _validate_once_delay(
        self,
        request_id: str,
        schedule_type: str,
        next_run_at: datetime,
        now: datetime,
    ) -> None:
        if schedule_type == "once" and next_run_at < now + timedelta(seconds=self._min_once_delay_seconds):
            raise AutomationInvalid(request_id)

    @classmethod
    def _validated_create(
        cls,
        context: PrivateWorkContext,
        command: AutomationCreate,
    ) -> AutomationCreate:
        if type(command) is not AutomationCreate:
            raise AutomationInvalid(context.request_id)
        title = cls._validated_title(context.request_id, command.title)
        prompt = cls._validated_prompt(context.request_id, command.prompt)
        if not isinstance(command.agent_asset_id, uuid.UUID) or command.agent_scope not in {"project", "system"}:
            raise AutomationInvalid(context.request_id)
        if command.context_mode not in {"fresh_thread_per_run", "reuse_thread"}:
            raise AutomationInvalid(context.request_id)
        if command.context_mode == "fresh_thread_per_run" and command.thread_id is not None:
            raise AutomationInvalid(context.request_id)
        if command.context_mode == "reuse_thread" and (not isinstance(command.thread_id, str) or not command.thread_id or len(command.thread_id) > 64):
            raise AutomationInvalid(context.request_id)
        normalized = cls._normalized_schedule(
            context.request_id,
            command.schedule_type,
            command.schedule_spec,
            command.timezone,
        )
        return AutomationCreate(
            title=title,
            prompt=prompt,
            context_mode=command.context_mode,
            thread_id=command.thread_id,
            agent_asset_id=command.agent_asset_id,
            agent_scope=command.agent_scope,
            schedule_type=command.schedule_type,
            schedule_spec=normalized,
            timezone=command.timezone,
        )

    @classmethod
    def _validated_changes(
        cls,
        request_id: str,
        changes: AutomationChanges,
    ) -> AutomationChanges:
        if type(changes) is not AutomationChanges:
            raise AutomationInvalid(request_id)
        cls._validate_expected_version(request_id, changes.expected_version)
        if changes.title is not None:
            cls._validated_title(request_id, changes.title)
        if changes.prompt is not None:
            cls._validated_prompt(request_id, changes.prompt)
        if changes.schedule_spec is not None and not isinstance(changes.schedule_spec, Mapping):
            raise AutomationInvalid(request_id)
        if changes.timezone is not None and (not isinstance(changes.timezone, str) or not changes.timezone or len(changes.timezone) > 64):
            raise AutomationInvalid(request_id)
        return changes

    @staticmethod
    def _validated_title(request_id: str, title: object) -> str:
        if not isinstance(title, str) or not title.strip() or len(title) > 255:
            raise AutomationInvalid(request_id)
        return title

    @staticmethod
    def _validated_prompt(request_id: str, prompt: object) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise AutomationInvalid(request_id)
        return prompt

    @staticmethod
    def _normalized_schedule(
        request_id: str,
        schedule_type: object,
        schedule_spec: object,
        timezone: object,
    ) -> Mapping[str, object]:
        if schedule_type not in {"once", "cron"} or not isinstance(schedule_spec, Mapping) or not isinstance(timezone, str) or not timezone or len(timezone) > 64:
            raise AutomationInvalid(request_id)
        if schedule_type == "cron":
            cron = schedule_spec.get("cron")
            normalized: dict[str, object] = {"cron": cron}
        else:
            normalized = {"run_at": schedule_spec.get("run_at")}
        if set(schedule_spec) != set(normalized):
            raise AutomationInvalid(request_id)
        return MappingProxyType(normalized)

    @staticmethod
    def _next_occurrence(
        request_id: str,
        schedule_type: str,
        schedule_spec: Mapping[str, object],
        timezone: str,
        now: datetime,
    ) -> datetime | None:
        try:
            return next_scheduled_occurrence(
                schedule_type,
                schedule_spec,
                timezone,
                now=now,
                coalesce=True,
            )
        except (TypeError, ValueError):
            raise AutomationInvalid(request_id) from None

    def _now(self, request_id: str) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise AutomationUnavailable(request_id)
        return now.astimezone(UTC)

    @staticmethod
    def _issued_context(context: PrivateWorkContext) -> PrivateWorkContext:
        try:
            return require_issued_private_work_context(context)
        except PrivateWorkNotFound as error:
            raise AutomationNotFound(error.request_id) from None

    @staticmethod
    def _validate_task_id(request_id: str, task_id: object) -> None:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
            raise AutomationNotFound(request_id)

    @staticmethod
    def _validate_expected_version(request_id: str, expected_version: object) -> None:
        if type(expected_version) is not int or expected_version < 1 or expected_version > _MAX_EXPECTED_VERSION:
            raise AutomationInvalid(request_id)

    @staticmethod
    def _view(record: ScheduledTaskRecord) -> AutomationView:
        return AutomationView(
            id=record.id,
            thread_id=record.thread_id,
            context_mode=record.context_mode,  # type: ignore[arg-type]
            agent_asset_id=record.agent_asset_id,
            agent_scope=record.agent_scope,  # type: ignore[arg-type]
            title=record.title,
            prompt=record.prompt,
            schedule_type=record.schedule_type,  # type: ignore[arg-type]
            schedule_spec=MappingProxyType(dict(record.schedule_spec)),
            timezone=record.timezone,
            status=record.status,  # type: ignore[arg-type]
            next_run_at=record.next_run_at,
            last_run_at=record.last_run_at,
            last_outcome=record.last_outcome,
            last_error_code=record.last_error_code,
            run_count=record.run_count,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _raise_mapped(error: Exception, request_id: str):
        if isinstance(error, AutomationError):
            raise error
        if isinstance(error, PrivateWorkNotFound):
            raise AutomationNotFound(request_id) from None
        if isinstance(error, PrivateWorkForbidden):
            raise AutomationForbidden(request_id) from None
        if isinstance(error, PrivateWorkUnavailable):
            raise AutomationUnavailable(request_id) from None
        if isinstance(error, AssetResolutionUnavailable):
            raise AutomationNotFound(request_id) from None
        if isinstance(error, AssetForbidden):
            raise AutomationForbidden(request_id) from None
        if isinstance(error, AssetValidationFailed):
            raise AutomationInvalid(request_id) from None
        if isinstance(error, IntegrityError):
            raise AutomationNotFound(request_id) from None
        if isinstance(error, (AssetStorageUnavailable, DBAPIError, SATimeoutError)):
            raise AutomationUnavailable(request_id) from None
        if isinstance(error, (TypeError, ValueError)):
            raise AutomationInvalid(request_id) from None
        raise error


__all__ = ["ProjectAutomationService"]
