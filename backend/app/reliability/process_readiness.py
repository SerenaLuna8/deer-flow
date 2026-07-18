"""Public aggregate readiness for independently deployed M6 process roles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.automations.ownership import AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY
from app.final_schema import FinalSchemaProbe

SchemaState = Literal["ready", "unavailable"]


class _SchedulerOwnership(Protocol):
    @property
    def is_acquired(self) -> bool: ...

    @property
    def is_lost(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProcessReadinessSnapshot:
    ready: bool
    role: str
    worker_fleet: str
    worker_count: int
    worker_capacity: int
    worker_oldest_heartbeat_age_seconds: int | None
    scheduler: str
    scheduler_ownership: str
    schema_state: SchemaState

    def as_public_dict(self) -> dict[str, object]:
        return asdict(self)


async def _scheduler_lock_is_held(session: AsyncSession) -> bool:
    return (
        await session.scalar(
            text(
                """SELECT EXISTS (
                 SELECT 1 FROM pg_locks
                 WHERE locktype='advisory' AND granted
                   AND classid=(((CAST(:key AS bigint) >> 32) & 4294967295)::oid)
                   AND objid=((CAST(:key AS bigint) & 4294967295)::oid)
                   AND objsubid=1)"""
            ),
            {"key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY},
        )
        is True
    )


async def read_process_readiness(
    session: AsyncSession,
    *,
    role: str,
    scheduler_enabled: bool,
    worker_fresh_for_seconds: int,
    scheduler_ownership: _SchedulerOwnership | None = None,
    now: datetime | None = None,
) -> ProcessReadinessSnapshot:
    """Read only redacted fleet aggregates and final schema state."""

    if role not in {"gateway", "worker", "scheduler"}:
        raise ValueError("process role is invalid")
    if type(scheduler_enabled) is not bool or type(worker_fresh_for_seconds) is not int or worker_fresh_for_seconds < 1:
        raise ValueError("process readiness configuration is invalid")
    selected_now = now or datetime.now(UTC)
    if selected_now.tzinfo is None or selected_now.utcoffset() is None:
        raise ValueError("process readiness time must be timezone aware")
    selected_now = selected_now.astimezone(UTC)
    try:
        schema = await FinalSchemaProbe().read(session)
    except SQLAlchemyError:
        schema = None
    if schema is None or not schema.ready:
        scheduler = "disabled" if not scheduler_enabled else "unavailable"
        scheduler_state = "disabled" if not scheduler_enabled else "unowned"
        return ProcessReadinessSnapshot(
            ready=False,
            role=role,
            worker_fleet="unavailable",
            worker_count=0,
            worker_capacity=0,
            worker_oldest_heartbeat_age_seconds=None,
            scheduler=scheduler,
            scheduler_ownership=scheduler_state,
            schema_state="unavailable",
        )
    worker_relation_ready = await session.scalar(text("SELECT to_regclass('worker_nodes') IS NOT NULL"))
    if worker_relation_ready is not True:
        scheduler = "disabled" if not scheduler_enabled else "unavailable"
        scheduler_state = "disabled" if not scheduler_enabled else "unowned"
        return ProcessReadinessSnapshot(
            ready=False,
            role=role,
            worker_fleet="unavailable",
            worker_count=0,
            worker_capacity=0,
            worker_oldest_heartbeat_age_seconds=None,
            scheduler=scheduler,
            scheduler_ownership=scheduler_state,
            schema_state="unavailable",
        )
    cutoff = selected_now - timedelta(seconds=worker_fresh_for_seconds)
    worker = (
        await session.execute(
            text(
                """SELECT count(*)::bigint AS worker_count,
                          COALESCE(sum(max_concurrent_jobs),0)::bigint AS capacity,
                          min(heartbeat_at) AS oldest
                   FROM worker_nodes
                   WHERE draining=false AND heartbeat_at>=:cutoff"""
            ),
            {"cutoff": cutoff},
        )
    ).one()
    worker_count = int(worker.worker_count)
    worker_capacity = int(worker.capacity)
    oldest_age = None
    if worker.oldest is not None:
        oldest = worker.oldest
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age = max(0, int((selected_now - oldest.astimezone(UTC)).total_seconds()))
    worker_fleet = "ready" if worker_count > 0 and worker_capacity > 0 else "unavailable"

    if not scheduler_enabled:
        scheduler = "disabled"
        scheduler_state = "disabled"
    elif scheduler_ownership is not None and scheduler_ownership.is_lost:
        scheduler = "unavailable"
        scheduler_state = "ownership_lost"
    else:
        owned = (scheduler_ownership is not None and scheduler_ownership.is_acquired) or await _scheduler_lock_is_held(session)
        scheduler = "ready" if owned else "unavailable"
        scheduler_state = "owned" if owned else "unowned"

    ready = worker_fleet == "ready" and (scheduler == "ready" or scheduler == "disabled")
    return ProcessReadinessSnapshot(
        ready=ready,
        role=role,
        worker_fleet=worker_fleet,
        worker_count=worker_count,
        worker_capacity=worker_capacity,
        worker_oldest_heartbeat_age_seconds=oldest_age,
        scheduler=scheduler,
        scheduler_ownership=scheduler_state,
        schema_state="ready",
    )


__all__ = ["ProcessReadinessSnapshot", "SchemaState", "read_process_readiness"]
