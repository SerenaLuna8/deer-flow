from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from deerflow.persistence.feedback import FeedbackRow


@dataclass(frozen=True, slots=True)
class PrivateFeedbackRecord:
    feedback_id: str
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    run_id: str
    message_id: str | None
    rating: int
    comment: str | None
    created_at: datetime


class PrivateFeedbackService:
    """Authorize and persist one owner-scoped feedback record per private Run."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._revalidator = PrivateWorkRevalidator()

    @staticmethod
    def _record(row: FeedbackRow) -> PrivateFeedbackRecord:
        return PrivateFeedbackRecord(
            feedback_id=row.feedback_id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            thread_id=row.thread_id,
            run_id=row.run_id,
            message_id=row.message_id,
            rating=row.rating,
            comment=row.comment,
            created_at=row.created_at,
        )

    @staticmethod
    def _feedback_statement(
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
    ):
        return select(FeedbackRow).where(
            FeedbackRow.project_id == context.project_id,
            FeedbackRow.owner_user_id == str(context.user_id),
            FeedbackRow.thread_id == thread_id,
            FeedbackRow.run_id == run_id,
        )

    async def _authorize_run(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        run_id: str,
        *,
        mutation: bool,
    ) -> None:
        current = await self._revalidator.require(
            session,
            context,
            Capability.PRIVATE_WORK_READ_OWN,
            lock=mutation,
        )
        thread = await PrivateThreadRepository(session).get(
            scope=context.resource_scope,
            thread_id=thread_id,
            lock=mutation,
        )
        run = await PrivateRunRepository(session).get(
            scope=context.resource_scope,
            run_id=run_id,
            lock=mutation,
        )
        if thread is None or run is None or run.thread_id != thread_id:
            raise PrivateWorkNotFound(context.request_id)
        # Resolve the private resource before returning 403. This preserves the
        # public 404 boundary for a different owner or project while allowing a
        # downgraded owner to receive the documented capability error.
        if mutation and Capability.PRIVATE_WORK_CREATE not in current.capabilities:
            raise PrivateWorkForbidden(context.request_id)

    async def get(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        run_id: str,
    ) -> PrivateFeedbackRecord | None:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorize_run(
                    session,
                    context,
                    thread_id,
                    run_id,
                    mutation=False,
                )
                row = (await session.execute(self._feedback_statement(context, thread_id, run_id))).scalar_one_or_none()
                return None if row is None else self._record(row)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def upsert(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        run_id: str,
        rating: int,
        message_id: str | None,
        comment: str | None,
    ) -> PrivateFeedbackRecord:
        context = require_issued_private_work_context(context)
        if rating not in {-1, 1}:
            raise PrivateWorkInvalid(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorize_run(
                    session,
                    context,
                    thread_id,
                    run_id,
                    mutation=True,
                )
                row = (
                    await session.execute(
                        self._feedback_statement(
                            context,
                            thread_id,
                            run_id,
                        ).with_for_update(of=FeedbackRow)
                    )
                ).scalar_one_or_none()
                now = datetime.now(UTC)
                if row is None:
                    row = FeedbackRow(
                        feedback_id=str(uuid.uuid4()),
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        run_id=run_id,
                        message_id=message_id,
                        rating=rating,
                        comment=comment,
                        created_at=now,
                    )
                    session.add(row)
                else:
                    row.message_id = message_id
                    row.rating = rating
                    row.comment = comment
                    row.created_at = now
                await session.flush()
                return self._record(row)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def delete(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        run_id: str,
    ) -> bool:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._authorize_run(
                    session,
                    context,
                    thread_id,
                    run_id,
                    mutation=True,
                )
                row = (
                    await session.execute(
                        self._feedback_statement(
                            context,
                            thread_id,
                            run_id,
                        ).with_for_update(of=FeedbackRow)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return False
                await session.delete(row)
                return True
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
