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
    v1_memories: int
    source_batches: int
    candidates: int
    facts: int
    snapshots: int
    jobs_cancelled: int


class AccountPersonalizationService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository_builder=AccountPersonalizationRepository,
    ) -> None:
        if not callable(session_factory) or not callable(repository_builder):
            raise ValueError("Account personalization configuration is invalid")
        self._sessions = session_factory
        self._repository_builder = repository_builder

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
    ) -> AccountMemoryResetResult:
        try:
            async with self._sessions() as session, session.begin():
                result = await self._repository_builder(session).reset_memory(
                    user_id,
                    expected_version=expected_version,
                    now=datetime.now(UTC),
                )
                return AccountMemoryResetResult(
                    version=result.version,
                    scopes_reset=result.scopes_reset,
                    v1_memories=result.v1_memories,
                    source_batches=result.source_batches,
                    candidates=result.candidates,
                    facts=result.facts,
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
