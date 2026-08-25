"""Process-owned PostgreSQL cohort authority for Run Skill snapshot writers."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.private_work.legacy_run_skill_snapshot_writer import (
    RunSkillSnapshotWriterReadback,
)

_COORDINATE_NAMESPACE = 0x41575231
_OWNER_NAMESPACE = 0x41575232
_SENTINEL_KEY = 0
_MAX_ENCODED_COORDINATE_BYTES = 1024
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 1.0

_TRY_LOCK = text(
    """SELECT pg_try_advisory_lock_shared(
               CAST(:namespace_key AS integer),
               CAST(:lock_key AS integer)
           )"""
)
_UNLOCK_ALL = text("SELECT pg_advisory_unlock_all()")
_BACKEND_PID = text("SELECT pg_backend_pid()")
_COHORT_LOCK_ROWS = text(
    """WITH cohort_pids AS (
           SELECT pid
             FROM pg_locks
            WHERE locktype = 'advisory'
              AND database = (
                    SELECT oid FROM pg_database WHERE datname = current_database()
                  )
              AND classid = CAST(:coordinate_namespace AS oid)
              AND objid = CAST(:sentinel_key AS oid)
              AND objsubid = 2
              AND granted
       )
       SELECT locks.pid, locks.classid::bigint AS classid,
              locks.objid::bigint AS objid, locks.mode
         FROM pg_locks AS locks
         JOIN cohort_pids USING (pid)
        WHERE locks.locktype = 'advisory'
          AND locks.database = (
                SELECT oid FROM pg_database WHERE datname = current_database()
              )
          AND locks.classid IN (
                CAST(:coordinate_namespace AS oid),
                CAST(:owner_namespace AS oid)
              )
          AND locks.objsubid = 2
          AND locks.granted
        ORDER BY locks.pid, locks.classid, locks.objid"""
)


class RunSkillWriterCohortConflict(RuntimeError):
    """A different complete writer coordinate is already published."""


class RunSkillWriterCohortUnavailable(RuntimeError):
    """This process cannot prove its exact live writer cohort authority."""


@dataclass(frozen=True, slots=True)
class RunSkillWriterCohortReadback:
    writer_mode: str
    artifact_version: str
    legacy_policy_digest: str
    process_role: Literal["gateway", "scheduler"]
    ready: bool


def _canonical_coordinate(readback: RunSkillSnapshotWriterReadback) -> bytes:
    if (
        type(readback) is not RunSkillSnapshotWriterReadback
        or not isinstance(readback.writer_mode, str)
        or not readback.writer_mode
        or not isinstance(readback.artifact_version, str)
        or not readback.artifact_version
        or not isinstance(readback.legacy_policy_digest, str)
        or not readback.legacy_policy_digest
        or readback.ready is not True
    ):
        raise RunSkillWriterCohortUnavailable(
            "Run Skill writer coordinate is unavailable",
        )
    encoded = json.dumps(
        {
            "artifact_version": readback.artifact_version,
            "legacy_policy_digest": readback.legacy_policy_digest,
            "writer_mode": readback.writer_mode,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not encoded or len(encoded) > _MAX_ENCODED_COORDINATE_BYTES:
        raise RunSkillWriterCohortUnavailable(
            "Run Skill writer coordinate is unavailable",
        )
    return encoded


def _positioned_lock_keys(value: bytes) -> tuple[int, ...]:
    return tuple(1 + position * 256 + byte for position, byte in enumerate(value))


def _decode_positioned_lock_keys(keys: list[int]) -> bytes | None:
    if not keys:
        return None
    positions: dict[int, int] = {}
    for key in keys:
        if key <= 0:
            return None
        encoded = key - 1
        position, byte = divmod(encoded, 256)
        if position in positions:
            return None
        positions[position] = byte
    if set(positions) != set(range(len(positions))):
        return None
    return bytes(positions[position] for position in range(len(positions)))


async def _try_session_lock(
    connection: AsyncConnection,
    *,
    namespace_key: int,
    lock_key: int,
) -> None:
    acquired = await connection.scalar(
        _TRY_LOCK,
        {"namespace_key": namespace_key, "lock_key": lock_key},
    )
    if acquired is not True:
        raise RunSkillWriterCohortUnavailable(
            "Run Skill writer cohort lock is unavailable",
        )


async def _read_lock_rows(connection_or_session: AsyncConnection | AsyncSession):
    return (
        await connection_or_session.execute(
            _COHORT_LOCK_ROWS,
            {
                "coordinate_namespace": _COORDINATE_NAMESPACE,
                "owner_namespace": _OWNER_NAMESPACE,
                "sentinel_key": _SENTINEL_KEY,
            },
        )
    ).all()


def _cohort_snapshot(
    rows: list[object],
) -> tuple[dict[int, bytes], dict[int, bytes]] | None:
    by_pid: dict[int, dict[int, list[int]]] = {}
    for raw_row in rows:
        pid, classid, objid, mode = raw_row
        if mode != "ShareLock":
            return None
        by_namespace = by_pid.setdefault(int(pid), {})
        by_namespace.setdefault(int(classid), []).append(int(objid))

    coordinates: dict[int, bytes] = {}
    owners: dict[int, bytes] = {}
    for pid, by_namespace in by_pid.items():
        coordinate_keys = by_namespace.get(_COORDINATE_NAMESPACE, [])
        if coordinate_keys.count(_SENTINEL_KEY) != 1:
            return None
        coordinate_keys.remove(_SENTINEL_KEY)
        coordinate = _decode_positioned_lock_keys(coordinate_keys)
        if coordinate is None:
            return None
        coordinates[pid] = coordinate
        owner = _decode_positioned_lock_keys(
            by_namespace.get(_OWNER_NAMESPACE, []),
        )
        if owner is None or len(owner) != 16:
            return None
        owners[pid] = owner
    if len(set(owners.values())) != len(owners):
        return None
    return coordinates, owners


_active_process_lease: RunSkillWriterCohortLease | None = None


class RunSkillWriterCohortLease:
    """One dedicated session that publishes an exact process writer identity."""

    __slots__ = (
        "_backend_pid",
        "_closed",
        "_connection",
        "_coordinate",
        "_heartbeat_interval_seconds",
        "_lost",
        "_monitor_task",
        "_owner_token",
        "_process_role",
        "_registered",
        "_writer_readback",
    )

    def __init__(
        self,
        *,
        connection: AsyncConnection,
        writer_readback: RunSkillSnapshotWriterReadback,
        coordinate: bytes,
        process_role: Literal["gateway", "scheduler"],
        owner_token: bytes,
        backend_pid: int,
        heartbeat_interval_seconds: float,
    ) -> None:
        self._connection = connection
        self._writer_readback = writer_readback
        self._coordinate = coordinate
        self._process_role = process_role
        self._owner_token = owner_token
        self._backend_pid = backend_pid
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._lost = asyncio.Event()
        self._closed = False
        self._registered = False
        self._monitor_task: asyncio.Task[None] | None = None

    @classmethod
    async def acquire(
        cls,
        engine: AsyncEngine,
        writer_readback: RunSkillSnapshotWriterReadback,
        *,
        process_role: Literal["gateway", "scheduler"],
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        process_authority: bool = False,
    ) -> RunSkillWriterCohortLease:
        """Start a complete lease; incomplete coordinates are never published."""

        global _active_process_lease
        if not isinstance(engine, AsyncEngine):
            raise TypeError("Run Skill writer cohort requires an AsyncEngine")
        if process_role not in {"gateway", "scheduler"}:
            raise ValueError("Run Skill writer cohort role is invalid")
        if not isinstance(heartbeat_interval_seconds, (int, float)) or isinstance(heartbeat_interval_seconds, bool) or heartbeat_interval_seconds <= 0:
            raise ValueError("Run Skill writer cohort heartbeat is invalid")
        coordinate = _canonical_coordinate(writer_readback)
        owner_token = secrets.token_bytes(16)
        connection = await engine.connect()
        lease: RunSkillWriterCohortLease | None = None
        try:
            for lock_key in _positioned_lock_keys(coordinate):
                await _try_session_lock(
                    connection,
                    namespace_key=_COORDINATE_NAMESPACE,
                    lock_key=lock_key,
                )
            for lock_key in _positioned_lock_keys(owner_token):
                await _try_session_lock(
                    connection,
                    namespace_key=_OWNER_NAMESPACE,
                    lock_key=lock_key,
                )
            # Publish only after every exact coordinate and owner byte is held.
            await _try_session_lock(
                connection,
                namespace_key=_COORDINATE_NAMESPACE,
                lock_key=_SENTINEL_KEY,
            )
            backend_pid = int(await connection.scalar(_BACKEND_PID))
            await connection.commit()
            rows = list(await _read_lock_rows(connection))
            snapshot = _cohort_snapshot(rows)
            if snapshot is None:
                raise RunSkillWriterCohortUnavailable(
                    "Run Skill writer cohort owner is unavailable",
                )
            coordinates, owners = snapshot
            if not coordinates or any(value != coordinate for value in coordinates.values()):
                raise RunSkillWriterCohortConflict(
                    "Run Skill writer cohort coordinate is mixed",
                )
            if owners.get(backend_pid) != owner_token:
                raise RunSkillWriterCohortUnavailable(
                    "Run Skill writer cohort owner is unavailable",
                )
            await connection.commit()
            lease = cls(
                connection=connection,
                writer_readback=writer_readback,
                coordinate=coordinate,
                process_role=process_role,
                owner_token=owner_token,
                backend_pid=backend_pid,
                heartbeat_interval_seconds=float(heartbeat_interval_seconds),
            )
            if process_authority:
                if _active_process_lease is not None:
                    raise RunSkillWriterCohortUnavailable(
                        "Run Skill writer cohort process authority is already active",
                    )
                _active_process_lease = lease
                lease._registered = True
            lease._monitor_task = asyncio.create_task(lease._monitor())
            return lease
        except BaseException:
            if lease is not None and lease._registered and _active_process_lease is lease:
                _active_process_lease = None
            try:
                if not connection.closed:
                    await connection.execute(_UNLOCK_ALL)
                    await connection.commit()
            finally:
                await connection.close()
            raise

    @property
    def backend_pid(self) -> int:
        """Internal process ownership fact; never include it in public readiness."""

        return self._backend_pid

    @property
    def ready(self) -> bool:
        return not self._closed and not self._lost.is_set() and not self._connection.closed

    @property
    def readback(self) -> RunSkillWriterCohortReadback:
        return RunSkillWriterCohortReadback(
            writer_mode=self._writer_readback.writer_mode,
            artifact_version=self._writer_readback.artifact_version,
            legacy_policy_digest=self._writer_readback.legacy_policy_digest,
            process_role=self._process_role,
            ready=self.ready,
        )

    async def _monitor(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                rows = list(await _read_lock_rows(self._connection))
                snapshot = _cohort_snapshot(rows)
                await self._connection.commit()
                if snapshot is None or snapshot[0].get(self._backend_pid) != self._coordinate or snapshot[1].get(self._backend_pid) != self._owner_token:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._lost.set()

    async def assert_ready(self, session: AsyncSession) -> None:
        if not isinstance(session, AsyncSession) or not session.in_transaction():
            raise RunSkillWriterCohortUnavailable(
                "Run Skill writer cohort requires the Admission transaction",
            )
        if not self.ready:
            raise RunSkillWriterCohortUnavailable(
                "Run Skill writer cohort owner is unavailable",
            )
        rows = list(await _read_lock_rows(session))
        snapshot = _cohort_snapshot(rows)
        if snapshot is None:
            raise RunSkillWriterCohortUnavailable(
                "Run Skill writer cohort owner is unavailable",
            )
        coordinates, owners = snapshot
        if not coordinates or any(value != self._coordinate for value in coordinates.values()):
            raise RunSkillWriterCohortConflict(
                "Run Skill writer cohort coordinate is mixed",
            )
        if coordinates.get(self._backend_pid) != self._coordinate or owners.get(self._backend_pid) != self._owner_token:
            self._lost.set()
            raise RunSkillWriterCohortUnavailable(
                "Run Skill writer cohort owner is unavailable",
            )

    async def wait_lost(self) -> None:
        await self._lost.wait()

    async def close(self) -> None:
        global _active_process_lease
        if self._closed:
            return
        self._closed = True
        if self._registered and _active_process_lease is self:
            _active_process_lease = None
            self._registered = False
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        try:
            if not self._connection.closed:
                await self._connection.execute(_UNLOCK_ALL)
                await self._connection.commit()
        except BaseException:
            self._lost.set()
        finally:
            await self._connection.close()
            self._lost.set()


async def require_active_run_skill_writer_cohort(
    session: AsyncSession,
    writer_readback: RunSkillSnapshotWriterReadback,
) -> None:
    lease = _active_process_lease
    if lease is None or _canonical_coordinate(writer_readback) != lease._coordinate:
        raise RunSkillWriterCohortUnavailable(
            "Run Skill writer cohort process authority is unavailable",
        )
    await lease.assert_ready(session)


async def active_run_skill_writer_cohort_ready(
    session: AsyncSession,
    writer_readback: RunSkillSnapshotWriterReadback,
) -> bool:
    try:
        await require_active_run_skill_writer_cohort(session, writer_readback)
    except (RunSkillWriterCohortConflict, RunSkillWriterCohortUnavailable):
        return False
    return True


__all__ = [
    "RunSkillWriterCohortConflict",
    "RunSkillWriterCohortLease",
    "RunSkillWriterCohortReadback",
    "RunSkillWriterCohortUnavailable",
    "active_run_skill_writer_cohort_ready",
    "require_active_run_skill_writer_cohort",
]
