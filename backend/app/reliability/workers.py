"""PostgreSQL-backed registry for independent M6 Worker processes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.workflow_runtime import WorkflowRuntimeMaterializedIdentity
from deerflow.persistence.jobs.model import WorkerNodeRow

_JOB_CAPABILITIES = frozenset(
    {
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
        "memory_dream",
        "memory_seal",
    }
)


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
    def _capabilities(value: frozenset[str]) -> list[str]:
        if not isinstance(value, frozenset):
            raise TypeError("worker capabilities must be a frozenset")
        if not value.issubset(_JOB_CAPABILITIES):
            raise ValueError("worker capabilities include an unsupported job type")
        return sorted(value)

    @staticmethod
    def _runtime_profile_digests(value: frozenset[str]) -> list[str]:
        if not isinstance(value, frozenset):
            raise TypeError("Worker runtime profile digests must be a frozenset")
        if len(value) > 128 or any(not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest) for digest in value):
            raise ValueError("Worker runtime profile digests must be unique lowercase SHA-256 values")
        return sorted(value)

    @staticmethod
    async def _workflow_runtime_identity(
        session: AsyncSession,
    ) -> dict[str, object]:
        locked = await SystemRuntimePolicyMaterializer.materialize_workflow_runtime_current_locked_in_session(
            session,
            for_update=True,
        )
        identity = WorkflowRuntimeMaterializedIdentity.from_locked(locked)
        return {
            "workflow_runtime_policy_section": "workflow_runtime",
            "workflow_runtime_policy_version_id": identity.policy_version_id,
            "workflow_runtime_policy_revision": identity.revision,
            "workflow_runtime_policy_schema_version": identity.schema_version,
            "workflow_runtime_policy_checksum": identity.payload_checksum,
        }

    async def register(
        self,
        worker_id: uuid.UUID,
        capabilities: frozenset[str],
        max_concurrent_jobs: int,
        *,
        runtime_profile_digests: frozenset[str],
    ) -> None:
        if not isinstance(worker_id, uuid.UUID):
            raise TypeError("worker_id must be a UUID")
        if not 1 <= max_concurrent_jobs <= 128:
            raise ValueError("worker capacity must be between 1 and 128")
        database_clock = sa.func.statement_timestamp()
        values = {
            "version": self._version,
            "capabilities_json": self._capabilities(capabilities),
            "runtime_profile_digests_json": self._runtime_profile_digests(
                runtime_profile_digests,
            ),
            "max_concurrent_jobs": max_concurrent_jobs,
            "draining": False,
            "started_at": database_clock,
            "heartbeat_at": database_clock,
        }
        async with self.session_factory() as session, session.begin():
            values.update(await self._workflow_runtime_identity(session))
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
        runtime_profile_digests: frozenset[str],
    ) -> bool:
        profiles = self._runtime_profile_digests(runtime_profile_digests)
        async with self.session_factory() as session, session.begin():
            identity = await self._workflow_runtime_identity(session)
            result = await session.execute(
                sa.update(WorkerNodeRow)
                .where(
                    WorkerNodeRow.id == worker_id,
                    WorkerNodeRow.draining.is_(False),
                )
                .values(
                    heartbeat_at=sa.func.statement_timestamp(),
                    runtime_profile_digests_json=profiles,
                    **identity,
                )
            )
        return result.rowcount == 1

    async def mark_draining(
        self,
        worker_id: uuid.UUID,
    ) -> bool:
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                sa.update(WorkerNodeRow)
                .where(WorkerNodeRow.id == worker_id)
                .values(
                    draining=True,
                    heartbeat_at=sa.func.statement_timestamp(),
                )
            )
        return result.rowcount == 1

    async def remove(self, worker_id: uuid.UUID) -> bool:
        """Logically remove a node while preserving attempt-history foreign keys."""

        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                sa.update(WorkerNodeRow)
                .where(WorkerNodeRow.id == worker_id)
                .values(
                    draining=True,
                    heartbeat_at=sa.func.statement_timestamp(),
                )
            )
        return result.rowcount == 1

    async def has_fresh_capability(
        self,
        capability: str,
        *,
        fresh_for_seconds: float,
    ) -> bool:
        if capability not in _JOB_CAPABILITIES:
            raise ValueError("unsupported worker capability")
        if fresh_for_seconds <= 0:
            raise ValueError("fresh_for_seconds must be positive")
        database_clock = sa.func.statement_timestamp()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(WorkerNodeRow.capabilities_json).where(
                        WorkerNodeRow.draining.is_(False),
                        WorkerNodeRow.heartbeat_at >= database_clock - timedelta(seconds=fresh_for_seconds),
                        WorkerNodeRow.heartbeat_at <= database_clock,
                    )
                )
            ).scalars()
            return any(capability in capabilities for capabilities in rows)


__all__ = ["WorkerRegistry"]
