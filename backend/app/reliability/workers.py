"""PostgreSQL-backed registry for independent M6 Worker processes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.jobs.model import WorkerNodeRow

_JOB_CAPABILITIES = frozenset({"private_run", "automation_run", "retention_purge", "mcp_discovery"})


class WorkerRegistry:
    """Persist only non-sensitive fleet liveness and capability metadata."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        version: str,
    ) -> None:
        if not version or len(version) > 64 or any(character.isspace() for character in version):
            raise ValueError("worker version must be a compact non-empty identifier")
        self.session_factory = session_factory
        self._version = version

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None:
            raise ValueError("worker registry time must be timezone-aware")
        return result

    @staticmethod
    def _capabilities(value: frozenset[str]) -> list[str]:
        if not isinstance(value, frozenset):
            raise TypeError("worker capabilities must be a frozenset")
        if not value.issubset(_JOB_CAPABILITIES):
            raise ValueError("worker capabilities include an unsupported job type")
        return sorted(value)

    async def register(
        self,
        worker_id: uuid.UUID,
        capabilities: frozenset[str],
        max_concurrent_jobs: int,
        *,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(worker_id, uuid.UUID):
            raise TypeError("worker_id must be a UUID")
        if not 1 <= max_concurrent_jobs <= 128:
            raise ValueError("worker capacity must be between 1 and 128")
        registered_at = self._now(now)
        values = {
            "version": self._version,
            "capabilities_json": self._capabilities(capabilities),
            "max_concurrent_jobs": max_concurrent_jobs,
            "draining": False,
            "started_at": registered_at,
            "heartbeat_at": registered_at,
        }
        async with self.session_factory() as session, session.begin():
            await session.execute(
                pg_insert(WorkerNodeRow)
                .values(id=worker_id, **values)
                .on_conflict_do_update(
                    index_elements=[WorkerNodeRow.id],
                    set_=values,
                )
            )

    async def heartbeat(
        self,
        worker_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        heartbeat_at = self._now(now)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                sa.update(WorkerNodeRow)
                .where(
                    WorkerNodeRow.id == worker_id,
                    WorkerNodeRow.draining.is_(False),
                )
                .values(heartbeat_at=heartbeat_at)
            )
        return result.rowcount == 1

    async def mark_draining(
        self,
        worker_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        draining_at = self._now(now)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(sa.update(WorkerNodeRow).where(WorkerNodeRow.id == worker_id).values(draining=True, heartbeat_at=draining_at))
        return result.rowcount == 1

    async def remove(self, worker_id: uuid.UUID) -> bool:
        """Logically remove a node while preserving attempt-history foreign keys."""

        async with self.session_factory() as session, session.begin():
            result = await session.execute(sa.update(WorkerNodeRow).where(WorkerNodeRow.id == worker_id).values(draining=True))
        return result.rowcount == 1

    async def has_fresh_capability(
        self,
        capability: str,
        *,
        fresh_for_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        if capability not in _JOB_CAPABILITIES:
            raise ValueError("unsupported worker capability")
        if fresh_for_seconds <= 0:
            raise ValueError("fresh_for_seconds must be positive")
        threshold = self._now(now) - timedelta(seconds=fresh_for_seconds)
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(WorkerNodeRow.capabilities_json).where(
                        WorkerNodeRow.draining.is_(False),
                        WorkerNodeRow.heartbeat_at >= threshold,
                    )
                )
            ).scalars()
            return any(capability in capabilities for capabilities in rows)


__all__ = ["WorkerRegistry"]
