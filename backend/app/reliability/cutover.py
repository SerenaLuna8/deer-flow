"""M6 reliability cutover guard."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.reliability.errors import ReliabilityCutover, ReliabilityDatabaseUnavailable
from deerflow.persistence.automations import AutomationCutoverStateRow
from deerflow.persistence.private_work.model import PrivateWorkCutoverStateRow
from deerflow.persistence.reliability import ReliabilityCutoverStateRow
from deerflow.persistence.revisions import REVISION_ANCESTRY, RevisionAncestry
from deerflow.trace_context import generate_trace_id, get_current_trace_id

RELIABILITY_REQUIRED_REVISION = "0015_project_reliability_finalize"


@dataclass(frozen=True, slots=True)
class _MarkerState:
    exists: bool
    complete: bool


class ReliabilityCutoverGuard:
    """Read M4, M5, and M6 authority on every execution-boundary check."""

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
    ) -> ReliabilityCutoverGuard:
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
            return _MarkerState(exists=False, complete=False)
        row = (
            await session.execute(
                select(
                    PrivateWorkCutoverStateRow.stage,
                    PrivateWorkCutoverStateRow.cutover_at,
                ).where(PrivateWorkCutoverStateRow.id == 1)
            )
        ).one_or_none()
        return _MarkerState(
            exists=True,
            complete=(row is not None and row.stage == "cutover_complete" and row.cutover_at is not None),
        )

    @staticmethod
    async def _read_automation_marker(session: AsyncSession) -> _MarkerState:
        table = await session.scalar(text("SELECT to_regclass('automation_cutover_state')"))
        if table is None:
            return _MarkerState(exists=False, complete=False)
        row = (
            await session.execute(
                select(
                    AutomationCutoverStateRow.stage,
                    AutomationCutoverStateRow.final_schema_probe_complete,
                    AutomationCutoverStateRow.cutover_at,
                ).where(AutomationCutoverStateRow.id == 1)
            )
        ).one_or_none()
        return _MarkerState(
            exists=True,
            complete=(row is not None and row.stage == "cutover_complete" and row.final_schema_probe_complete is True and row.cutover_at is not None),
        )

    @staticmethod
    async def _read_reliability_marker(session: AsyncSession) -> _MarkerState:
        table = await session.scalar(text("SELECT to_regclass('reliability_cutover_state')"))
        if table is None:
            return _MarkerState(exists=False, complete=False)
        row = (
            await session.execute(
                select(
                    ReliabilityCutoverStateRow.stage,
                    ReliabilityCutoverStateRow.source_probe_complete,
                    ReliabilityCutoverStateRow.active_run_probe_complete,
                    ReliabilityCutoverStateRow.quota_backfill_probe_complete,
                    ReliabilityCutoverStateRow.job_relation_probe_complete,
                    ReliabilityCutoverStateRow.audit_trigger_probe_complete,
                    ReliabilityCutoverStateRow.stream_probe_complete,
                    ReliabilityCutoverStateRow.recovery_probe_complete,
                    ReliabilityCutoverStateRow.final_schema_probe_complete,
                    ReliabilityCutoverStateRow.schema_revision,
                    ReliabilityCutoverStateRow.cutover_at,
                ).where(ReliabilityCutoverStateRow.id == 1)
            )
        ).one_or_none()
        return _MarkerState(
            exists=True,
            complete=(row is not None and row.stage == "cutover_complete" and all(row[1:9]) and row.schema_revision is not None and row.cutover_at is not None),
        )

    async def _require_m6_open(self) -> None:
        try:
            async with self._session() as session:
                private_marker = await self._read_private_marker(session)
                automation_marker = await self._read_automation_marker(session)
                reliability_marker = await self._read_reliability_marker(session)
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            raise ReliabilityDatabaseUnavailable(self.request_id) from None
        if not private_marker.complete or not automation_marker.complete or not reliability_marker.complete or not self._revisions.contains(str(revision), RELIABILITY_REQUIRED_REVISION):
            raise ReliabilityCutover(self.request_id)

    async def require_queue_open(self) -> None:
        await self._require_m6_open()

    async def require_gateway_open(self) -> None:
        await self._require_m6_open()

    async def require_worker_open(self) -> None:
        await self._require_m6_open()

    async def require_legacy_execution_open(self) -> None:
        try:
            async with self._session() as session:
                marker = await self._read_reliability_marker(session)
        except SQLAlchemyError:
            raise ReliabilityDatabaseUnavailable(self.request_id) from None
        if marker.complete:
            raise ReliabilityCutover(self.request_id)


__all__ = ["RELIABILITY_REQUIRED_REVISION", "ReliabilityCutoverGuard"]
