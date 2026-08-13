"""Application service for account Memory personalization."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.personalization.repository import (
    AccountPersonalizationConflict,
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService


class AccountPersonalizationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AccountPersonalizationView:
    memory_enabled: bool
    effective_memory_enabled: bool
    platform_memory_available: bool
    version: int


@dataclass(frozen=True, slots=True)
class AccountMemoryResetResult:
    version: int
    scopes_reset: int
    history_entries: int
    documents: int
    versions: int
    dream_runs: int
    prepare_runs: int
    snapshots: int
    jobs_cancelled: int


class AccountPersonalizationService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository_builder=AccountPersonalizationRepository,
        audit=None,
    ) -> None:
        if not callable(session_factory) or not callable(repository_builder):
            raise ValueError("Account personalization configuration is invalid")
        if audit is not None and (not callable(getattr(audit, "memory_dream_settled", None)) or not callable(getattr(audit, "memory_reset_executed", None))):
            raise ValueError("Account personalization audit configuration is invalid")
        self._sessions = session_factory
        self._repository_builder = repository_builder
        self._audit = audit

    @staticmethod
    async def _platform_available(session: AsyncSession) -> bool:
        runtime = await SystemRuntimePolicyService.read_agent_runtime_for_admission(session)
        return bool(runtime.value.memory.enabled)

    @staticmethod
    def _view(preference, *, platform_available: bool) -> AccountPersonalizationView:
        return AccountPersonalizationView(
            memory_enabled=preference.memory_enabled,
            effective_memory_enabled=(platform_available and preference.memory_enabled),
            platform_memory_available=platform_available,
            version=preference.version,
        )

    async def get(self, user_id: uuid.UUID) -> AccountPersonalizationView:
        try:
            async with self._sessions() as session, session.begin():
                preference = await self._repository_builder(session).read_memory(user_id)
                available = await self._platform_available(session)
                return self._view(preference, platform_available=available)
        except (AccountPersonalizationNotFound, AccountPersonalizationConflict):
            raise
        except DBAPIError:
            raise AccountPersonalizationUnavailable from None
        except Exception:
            raise AccountPersonalizationUnavailable from None

    async def update_memory(
        self,
        user_id: uuid.UUID,
        *,
        memory_enabled: bool,
        expected_version: int,
    ) -> AccountPersonalizationView:
        try:
            async with self._sessions() as session, session.begin():
                preference = await self._repository_builder(session).update_memory(
                    user_id,
                    memory_enabled=memory_enabled,
                    expected_version=expected_version,
                )
                available = await self._platform_available(session)
                return self._view(preference, platform_available=available)
        except (AccountPersonalizationNotFound, AccountPersonalizationConflict):
            raise
        except DBAPIError:
            raise AccountPersonalizationUnavailable from None
        except Exception:
            raise AccountPersonalizationUnavailable from None

    async def reset_memory(
        self,
        user_id: uuid.UUID,
        *,
        expected_version: int,
        request_id: str,
    ) -> AccountMemoryResetResult:
        if type(request_id) is not str or not request_id:
            raise ValueError("Account Memory reset request id is invalid")
        try:
            async with self._sessions() as session, session.begin():
                result = await self._repository_builder(session).reset_memory(
                    user_id,
                    expected_version=expected_version,
                    now=datetime.now(UTC),
                )
                if self._audit is not None:
                    for settled in result.settled_dreams:
                        await self._audit.memory_dream_settled(
                            session,
                            project_id=settled.project_id,
                            job_id=settled.job_id,
                            request_id=request_id,
                            disposition="cancelled",
                        )
                    await self._audit.memory_reset_executed(
                        session,
                        user_id=user_id,
                        request_id=request_id,
                        affected_project_ids=result.affected_project_ids,
                        scopes_reset=result.scopes_reset,
                        history_entries=result.history_entries,
                        documents=result.documents,
                        versions=result.versions,
                        dream_runs=result.dream_runs,
                        prepare_runs=result.prepare_runs,
                        snapshots=result.snapshots,
                        episodes=result.episodes,
                        jobs_cancelled=result.jobs_cancelled,
                    )
                return AccountMemoryResetResult(
                    version=result.version,
                    scopes_reset=result.scopes_reset,
                    history_entries=result.history_entries,
                    documents=result.documents,
                    versions=result.versions,
                    dream_runs=result.dream_runs,
                    prepare_runs=result.prepare_runs,
                    snapshots=result.snapshots,
                    jobs_cancelled=result.jobs_cancelled,
                )
        except (AccountPersonalizationNotFound, AccountPersonalizationConflict):
            raise
        except DBAPIError:
            raise AccountPersonalizationUnavailable from None
        except Exception:
            raise AccountPersonalizationUnavailable from None


__all__ = [
    "AccountMemoryResetResult",
    "AccountPersonalizationConflict",
    "AccountPersonalizationNotFound",
    "AccountPersonalizationService",
    "AccountPersonalizationUnavailable",
    "AccountPersonalizationView",
]
