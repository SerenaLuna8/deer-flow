"""Gateway admission and owner-scoped control for durable Dream preparation."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateLifecyclePort,
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
from app.private_work.memory_observability import record_memory_failure
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from deerflow.memory_contract import (
    DEFAULT_MEMORY_NAMESPACE,
    MemoryDocumentScope,
    MemoryDreamPrepareAdmission,
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareNotFound,
    MemoryDreamPrepareRecord,
)
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareRepository,
)
from deerflow.persistence.private_work.memory_dream_store import (
    MemoryDreamStore,
)


class MemoryDreamPrepareService:
    """Admit/read/cancel preparations without executing model work in Gateway."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        repository_builder=MemoryDreamPrepareRepository,
        job_repository_builder=JobRepository,
        dream_store_builder=MemoryDreamStore,
        revalidator: PrivateWorkRevalidator | None = None,
        account_private_lifecycle: AccountPrivateLifecyclePort | None = None,
        audit=None,
    ) -> None:
        if not all(
            callable(value)
            for value in (
                session_factory,
                repository_builder,
                job_repository_builder,
                dream_store_builder,
            )
        ):
            raise ValueError("Dream preparation service configuration is invalid")
        self._sessions = session_factory
        self._repository_builder = repository_builder
        self._job_repository_builder = job_repository_builder
        self._dream_store_builder = dream_store_builder
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._account_private_lifecycle = account_private_lifecycle or AccountPrivateLifecycle()
        if audit is not None and not callable(getattr(audit, "memory_dream_settled", None)):
            raise ValueError("Dream preparation audit port is invalid")
        self._audit = audit

    @staticmethod
    def _scope(context: PrivateWorkContext) -> MemoryDocumentScope:
        context = require_issued_private_work_context(context)
        return MemoryDocumentScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            namespace=DEFAULT_MEMORY_NAMESPACE,
        )

    def _repository(self, session: AsyncSession) -> MemoryDreamPrepareRepository:
        return self._repository_builder(
            session,
            jobs=self._job_repository_builder(session),
        )

    async def admit(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        operation_id: uuid.UUID,
    ) -> MemoryDreamPrepareAdmission:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                account_private_generation = await self._account_private_lifecycle.require_active_after_membership(
                    session,
                    scope.owner_user_id,
                )
                return await self._repository(session).admit(
                    scope,
                    account_private_generation=account_private_generation,
                    thread_id=thread_id,
                    operation_id=operation_id,
                    request_id=context.request_id,
                    now=datetime.now(UTC),
                )
        except MemoryDreamPrepareNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDreamPrepareConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "prepare_admit",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "prepare_admit",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def read(
        self,
        context: PrivateWorkContext,
        job_id: uuid.UUID,
    ) -> MemoryDreamPrepareRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                )
                return await self._repository(session).read(scope, job_id)
        except MemoryDreamPrepareNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "prepare_read",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "prepare_read",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def read_latest(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
    ) -> MemoryDreamPrepareRecord:
        scope = self._scope(context)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                )
                return await self._repository(session).read_latest(
                    scope,
                    thread_id=thread_id,
                )
        except MemoryDreamPrepareNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "prepare_read_latest",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "prepare_read_latest",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None

    async def cancel(
        self,
        context: PrivateWorkContext,
        job_id: uuid.UUID,
    ) -> MemoryDreamPrepareRecord:
        scope = self._scope(context)
        now = datetime.now(UTC)
        try:
            async with self._sessions() as session, session.begin():
                await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                repository = self._repository(session)
                jobs = self._job_repository_builder(session)
                current = await repository.request_cancel(
                    scope,
                    job_id=job_id,
                    reason="user_cancelled",
                    now=now,
                )
                # Preparation success means the child Dream has been admitted,
                # not necessarily settled.  Cancellation therefore continues
                # cooperatively into the child even after the parent terminal.
                if current.dream_job_id is not None:
                    settled = await self._dream_store_builder(
                        session,
                        jobs=jobs,
                    ).request_dream_cancel(
                        scope,
                        current.dream_job_id,
                        reason="dream_prepare_cancelled",
                        now=now,
                    )
                    if settled and self._audit is not None:
                        await self._audit.memory_dream_settled(
                            session,
                            project_id=scope.project_id,
                            job_id=current.dream_job_id,
                            request_id=context.request_id,
                            disposition="cancelled",
                        )
                return await repository.read(scope, job_id)
        except MemoryDreamPrepareNotFound:
            raise PrivateWorkNotFound(context.request_id) from None
        except MemoryDreamPrepareConflict:
            raise PrivateWorkConflict(context.request_id) from None
        except PrivateWorkError:
            raise
        except DBAPIError as error:
            record_memory_failure(
                "prepare_cancel",
                error,
                failure_category="database",
            )
            raise PrivateWorkUnavailable(context.request_id) from None
        except Exception as error:
            record_memory_failure(
                "prepare_cancel",
                error,
                failure_category="internal",
            )
            raise PrivateWorkUnavailable(context.request_id) from None


__all__ = ["MemoryDreamPrepareService"]
