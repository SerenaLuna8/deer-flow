from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.errors import AutomationCutover, AutomationUnavailable
from deerflow.persistence.automations import AutomationCutoverStateRow
from deerflow.persistence.private_work.model import PrivateWorkCutoverStateRow
from deerflow.persistence.revisions import REVISION_ANCESTRY, RevisionAncestry
from deerflow.trace_context import generate_trace_id, get_current_trace_id

AUTOMATION_REQUIRED_REVISION = "0013_project_automation_finalize"


@dataclass(frozen=True, slots=True)
class _MarkerState:
    complete: bool


class AutomationCutoverGuard:
    """Fail closed around the M4/M5 marker and final-revision boundary."""

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
    ) -> AutomationCutoverGuard:
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

    @staticmethod
    async def _read_private_marker(session: AsyncSession) -> _MarkerState:
        table = await session.scalar(text("SELECT to_regclass('private_work_cutover_state')"))
        if table is None:
            return _MarkerState(complete=False)
        row = (
            await session.execute(
                select(
                    PrivateWorkCutoverStateRow.stage,
                    PrivateWorkCutoverStateRow.cutover_at,
                ).where(PrivateWorkCutoverStateRow.id == 1)
            )
        ).one_or_none()
        return _MarkerState(complete=(row is not None and row.stage == "cutover_complete" and row.cutover_at is not None))

    @staticmethod
    async def _read_automation_marker(session: AsyncSession) -> _MarkerState:
        table = await session.scalar(text("SELECT to_regclass('automation_cutover_state')"))
        if table is None:
            return _MarkerState(complete=False)
        row = (
            await session.execute(
                select(
                    AutomationCutoverStateRow.stage,
                    AutomationCutoverStateRow.final_schema_probe_complete,
                    AutomationCutoverStateRow.cutover_at,
                ).where(AutomationCutoverStateRow.id == 1)
            )
        ).one_or_none()
        return _MarkerState(complete=(row is not None and row.stage == "cutover_complete" and row.final_schema_probe_complete is True and row.cutover_at is not None))

    async def require_legacy_open(self) -> None:
        try:
            async with self._session() as session:
                marker = await self._read_automation_marker(session)
        except SQLAlchemyError:
            raise AutomationUnavailable(self.request_id) from None
        if marker.complete:
            raise AutomationCutover(self.request_id)

    async def require_project_open(self) -> None:
        try:
            async with self._session() as session:
                private_marker = await self._read_private_marker(session)
                automation_marker = await self._read_automation_marker(session)
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            raise AutomationUnavailable(self.request_id) from None
        if (
            not private_marker.complete
            or not automation_marker.complete
            or not self._revisions.contains(
                str(revision),
                AUTOMATION_REQUIRED_REVISION,
            )
        ):
            raise AutomationCutover(self.request_id)


__all__ = ["AUTOMATION_REQUIRED_REVISION", "AutomationCutoverGuard"]
