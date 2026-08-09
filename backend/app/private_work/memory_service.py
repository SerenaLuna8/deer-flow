"""Application service for owner-private Memory documents."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.memory_dream_service import MemoryDreamAdmissionService
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    RuntimePolicySection,
)
from deerflow.agents.memory.dream import (
    MemoryDocumentInvalid,
    estimate_memory_tokens,
    validate_memory_document,
)
from deerflow.config.app_config import AppConfig
from deerflow.persistence.private_work.memory_document_repository import (
    DEFAULT_EPISODE_RETENTION_DAYS,
    DEFAULT_MEMORY_NAMESPACE,
    MemoryDocumentConflict,
    MemoryDocumentNotFound,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDocumentState,
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionRecord,
    MemoryEpisodeRecord,
    MemoryPendingEntryRecord,
)
from deerflow.runtime.context_compaction import ThreadCompactionResult

_DREAM_ARCHIVE_KEEP: tuple[str, int] = ("messages", 0)
_DREAM_ARCHIVE_SEAL_RETRIES = 3


class DreamThreadArchiveBarrier(Protocol):
    async def compact(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        force: bool,
        keep: tuple[str, int | float] | None,
        app_config: AppConfig,
    ) -> ThreadCompactionResult: ...

    async def lock_and_verify_dream_archive_ready(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        app_config: AppConfig,
    ) -> bool: ...


class PrivateMemoryDocumentService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository_builder=MemoryDocumentRepository,
        revalidator: PrivateWorkRevalidator | None = None,
        dream_admission: MemoryDreamAdmissionService | None = None,
        personalization_repository_builder=AccountPersonalizationRepository,
        dream_archive_barrier: DreamThreadArchiveBarrier | None = None,
    ) -> None:
        if not callable(session_factory) or not callable(repository_builder) or not callable(personalization_repository_builder):
            raise ValueError("Memory document service configuration is invalid")
        self._sessions = session_factory
        self._repository_builder = repository_builder
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._dream_admission = dream_admission or MemoryDreamAdmissionService(
            repository_builder=repository_builder,
            personalization_repository_builder=(personalization_repository_builder),
        )
        self._personalization_repository_builder = personalization_repository_builder
        self._dream_archive_barrier = dream_archive_barrier

    @staticmethod
    def _scope(context: PrivateWorkContext) -> MemoryDocumentScope:
        context = require_issued_private_work_context(context)
        return MemoryDocumentScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
        )

    async def get(
        self,
        context: PrivateWorkContext,
    ) -> tuple[MemoryDocumentState, str]:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                state = await self._repository_builder(session).read_state(scope)
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
                # Derived, never stored: what the next Run admission would do
                # with this document under the current platform budget.
                injection_status = "ok"
                if state.document.version >= 1 and isinstance(policy, AgentRuntimePolicyValue) and estimate_memory_tokens(state.document.content) > policy.memory.max_injection_tokens:
                    injection_status = "skipped_over_budget"
                return state, injection_status
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_versions(
        self,
        context: PrivateWorkContext,
        *,
        limit: int,
        offset: int,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_versions(
                    scope,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_pending(
        self,
        context: PrivateWorkContext,
        *,
        limit: int,
        offset: int,
    ) -> tuple[MemoryPendingEntryRecord, ...]:
        """Backlog read model: what the next Dream will organize, oldest first."""

        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).list_pending_entries(
                    scope,
                    limit=limit,
                    offset=offset,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def list_episodes(
        self,
        context: PrivateWorkContext,
        *,
        q: str | None,
        tags: tuple[str, ...],
        before: datetime | None,
        limit: int,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        """Archive read model: ranked search with ``q``, time browse without."""

        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                repository = self._repository_builder(session)
                now = datetime.now(UTC)
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                )
                retention_days = policy.memory.episode_retention_days if isinstance(policy, AgentRuntimePolicyValue) else DEFAULT_EPISODE_RETENTION_DAYS
                if q is not None:
                    return await repository.search_episodes(
                        scope,
                        query=q,
                        tags=tags,
                        limit=limit,
                        retention_days=retention_days,
                        now=now,
                    )
                return await repository.list_episodes(
                    scope,
                    tags=tags,
                    before=before,
                    limit=limit,
                    retention_days=retention_days,
                    now=now,
                )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def get_version(
        self,
        context: PrivateWorkContext,
        version: int,
    ) -> MemoryDocumentVersionRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_READ_OWN,
                )
                return await self._repository_builder(session).read_version(
                    scope,
                    version,
                )
        except MemoryDocumentNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def dream(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str | None = None,
        app_config: AppConfig | None = None,
    ) -> MemoryDreamAdmissionRecord:
        scope = self._scope(context)
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 64):
            raise PrivateWorkConflict(context.request_id)
        try:
            if thread_id is not None:
                return await self._drain_thread_and_admit_dream(
                    context,
                    scope,
                    thread_id,
                    app_config=app_config,
                )
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                return await self._dream_admission.admit(
                    session,
                    scope,
                    trigger="manual_dream",
                    now=datetime.now(UTC),
                )
        except PrivateWorkError:
            raise
        except MemoryDocumentConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except MemoryDocumentInvalid:
            raise PrivateWorkConflict(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def _drain_thread_and_admit_dream(
        self,
        context: PrivateWorkContext,
        scope: MemoryDocumentScope,
        thread_id: str,
        *,
        app_config: AppConfig | None,
    ) -> MemoryDreamAdmissionRecord:
        """Drain complete turns outside locks, then admit behind a locked head."""

        barrier = self._dream_archive_barrier
        if barrier is None or app_config is None:
            raise PrivateWorkUnavailable(context.request_id)

        committed_checkpoints: set[str] = set()
        stale_seals = 0
        while True:
            result = await barrier.compact(
                context,
                thread_id,
                force=True,
                keep=_DREAM_ARCHIVE_KEEP,
                app_config=app_config,
            )
            if result.compacted:
                checkpoint_id = result.checkpoint_id
                if result.removed_message_count <= 0 or not isinstance(checkpoint_id, str) or not checkpoint_id or checkpoint_id in committed_checkpoints:
                    raise PrivateWorkUnavailable(context.request_id)
                committed_checkpoints.add(checkpoint_id)
                continue
            if result.reason != "not_enough_messages":
                raise PrivateWorkUnavailable(context.request_id)

            async with self._sessions() as session, session.begin():
                ready = await barrier.lock_and_verify_dream_archive_ready(
                    session,
                    context,
                    thread_id,
                    app_config=app_config,
                )
                if ready:
                    return await self._dream_admission.admit(
                        session,
                        scope,
                        trigger="manual_dream",
                        now=datetime.now(UTC),
                    )

            stale_seals += 1
            if stale_seals >= _DREAM_ARCHIVE_SEAL_RETRIES:
                raise PrivateWorkConflict(context.request_id)

    async def restore(
        self,
        context: PrivateWorkContext,
        *,
        target_version: int,
        expected_current_version: int,
    ) -> MemoryDocumentVersionRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    lock=True,
                )
                policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                    session,
                    RuntimePolicySection.AGENT_RUNTIME,
                    for_update=True,
                )
                preference = await self._personalization_repository_builder(session).read_memory(
                    scope.owner_user_id,
                    for_update=True,
                )
                if not preference.memory_enabled or not isinstance(policy, AgentRuntimePolicyValue) or not policy.memory.enabled:
                    raise MemoryDocumentConflict("Memory is disabled")
                repository = self._repository_builder(session)
                state = await repository.read_state(scope, for_update=True)
                if state.document.sections_policy_version_id is None:
                    raise MemoryDocumentNotFound
                target = await repository.read_version(scope, target_version)
                validate_memory_document(
                    target.content,
                    policy.memory.max_injection_tokens,
                    sections=state.document.sections,
                )
                return await repository.restore_version(
                    scope,
                    target_version=target_version,
                    expected_current_version=expected_current_version,
                    expected_sections=state.document.sections,
                    max_tokens=policy.memory.max_injection_tokens,
                    now=datetime.now(UTC),
                )
        except MemoryDocumentNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDocumentConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except AccountPersonalizationNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDocumentInvalid:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception:
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = ["PrivateMemoryDocumentService"]
