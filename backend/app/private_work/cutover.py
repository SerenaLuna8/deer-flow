from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.errors import PrivateWorkCutover, PrivateWorkUnavailable
from deerflow.persistence.private_work.model import PrivateWorkCutoverStateRow
from deerflow.persistence.revisions import REVISION_ANCESTRY, RevisionAncestry
from deerflow.trace_context import generate_trace_id, get_current_trace_id

PRIVATE_WORK_REQUIRED_REVISION = "0011_private_artifact_tombstone"
# Compatibility export for test/support callers that still use the old name.
PRIVATE_WORK_FINAL_REVISION = PRIVATE_WORK_REQUIRED_REVISION


@dataclass(frozen=True)
class _CutoverState:
    stage: str | None
    cutover_complete: bool


class PrivateWorkCutoverGuard:
    """Read the singleton private-work cutover marker at each boundary."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        request_id: str | None = None,
        revisions: RevisionAncestry = REVISION_ANCESTRY,
    ) -> None:
        self._session_factory = session_factory
        self._request_session: AsyncSession | None = None
        self._request_id = request_id
        self._revisions = revisions

    @classmethod
    def for_session(
        cls,
        session: AsyncSession,
        *,
        request_id: str | None = None,
        revisions: RevisionAncestry = REVISION_ANCESTRY,
    ) -> PrivateWorkCutoverGuard:
        guard = cls.__new__(cls)
        guard._session_factory = None
        guard._request_session = session
        guard._request_id = request_id
        guard._revisions = revisions
        return guard

    @property
    def request_id(self) -> str:
        return self._request_id or get_current_trace_id() or generate_trace_id()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        if self._request_session is not None:
            yield self._request_session
            return
        async with self._session_factory() as session:
            yield session

    async def _read_marker(self, session: AsyncSession) -> _CutoverState:
        marker_table = await session.scalar(text("SELECT to_regclass('private_work_cutover_state')"))
        if marker_table is None:
            return _CutoverState(stage=None, cutover_complete=False)
        row = (
            await session.execute(
                select(
                    PrivateWorkCutoverStateRow.stage,
                    PrivateWorkCutoverStateRow.cutover_at,
                ).where(PrivateWorkCutoverStateRow.id == 1)
            )
        ).one_or_none()
        if row is None:
            return _CutoverState(stage=None, cutover_complete=False)
        return _CutoverState(
            stage=row.stage,
            cutover_complete=(row.stage == "cutover_complete" and row.cutover_at is not None),
        )

    async def require_legacy_open(self) -> None:
        try:
            async with self._session() as session:
                marker = await self._read_marker(session)
        except SQLAlchemyError:
            raise PrivateWorkUnavailable(self.request_id) from None
        if marker.cutover_complete:
            raise PrivateWorkCutover(self.request_id)

    async def require_project_open(self) -> None:
        try:
            async with self._session() as session:
                marker = await self._read_marker(session)
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            raise PrivateWorkUnavailable(self.request_id) from None
        if not marker.cutover_complete or not self._revisions.contains(str(revision), PRIVATE_WORK_REQUIRED_REVISION):
            raise PrivateWorkCutover(self.request_id)


__all__ = [
    "PRIVATE_WORK_FINAL_REVISION",
    "PRIVATE_WORK_REQUIRED_REVISION",
    "PrivateWorkCutoverGuard",
]
