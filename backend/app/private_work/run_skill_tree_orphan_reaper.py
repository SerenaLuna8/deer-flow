"""Fail-closed startup reconciliation for durable Run Skill owner trees."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.private_work.run_skill_tree_materializer import (
    MaterializationOwnerMetadata,
    read_materialization_owner_metadata,
    remove_materialization_owner_if_unchanged,
)
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.sandbox.sandbox_provider import (
    ProviderRunMountLease,
    ProviderRunMountOwnerAbsentProof,
    ProviderRunMountOwnerReconciliation,
    ProviderRunMountOwnerUnknown,
)
from deerflow.utils.asyncio import joined_to_thread

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_NAMESPACE = b"actweave:run-skill-tree-orphan:v1\0"
_ACTIVE_JOB_STATUSES = frozenset({"leased", "running"})
_KNOWN_JOB_STATUSES = frozenset(
    {
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "dead",
    }
)

type _OwnerDisposition = Literal[
    "deleted_never_acquired",
    "deleted_provider_absent",
    "preserved_active",
    "preserved_grace",
    "preserved_lock",
    "preserved_unknown",
]


class _ProviderReconciliationPort(Protocol):
    async def ensure_run_readonly_mount_owner_absent_async(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerReconciliation: ...


@dataclass(frozen=True, slots=True)
class RunSkillTreeOrphanReapReport:
    scanned: int = 0
    deleted_never_acquired: int = 0
    deleted_provider_absent: int = 0
    preserved_active: int = 0
    preserved_grace: int = 0
    preserved_lock: int = 0
    preserved_unknown: int = 0

    @property
    def deleted(self) -> int:
        return self.deleted_never_acquired + self.deleted_provider_absent


class RunSkillTreeOrphanReaper:
    """One-shot Worker startup reaper with per-owner PostgreSQL fencing."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        materialization_root: Path,
        provider: _ProviderReconciliationPort,
        grace_seconds: int,
    ) -> None:
        if (
            not isinstance(engine, AsyncEngine)
            or not isinstance(materialization_root, Path)
            or not materialization_root.is_absolute()
            or ".." in materialization_root.parts
            or materialization_root.name != "run-skill-materializations"
            or not callable(
                getattr(
                    provider,
                    "ensure_run_readonly_mount_owner_absent_async",
                    None,
                )
            )
            or type(grace_seconds) is not int
            or grace_seconds < 0
        ):
            raise ValueError("Invalid Run Skill orphan reaper configuration")
        self._engine = engine
        self._materialization_root = materialization_root
        self._provider = provider
        self._grace = timedelta(seconds=grace_seconds)

    async def reconcile_once(self) -> RunSkillTreeOrphanReapReport:
        """Reconcile every valid immediate owner directory exactly once."""

        try:
            owner_ids, invalid_entries = await joined_to_thread(
                scan_materialization_owner_ids,
                self._materialization_root,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Run Skill orphan scan unavailable; preserving materialization root",
                exc_info=True,
            )
            return RunSkillTreeOrphanReapReport(preserved_unknown=1)

        report = RunSkillTreeOrphanReapReport(
            preserved_unknown=invalid_entries,
        )
        for owner_id in owner_ids:
            try:
                disposition = await self._reap_owner(owner_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                disposition = "preserved_unknown"
                logger.warning(
                    "Run Skill orphan owner reconciliation failed closed",
                    exc_info=True,
                )
            report = replace(
                report,
                scanned=report.scanned + 1,
                **{
                    disposition: getattr(report, disposition) + 1,
                },
            )
        return report

    async def reap_startup(self) -> RunSkillTreeOrphanReapReport:
        """Run the one-shot reconciliation used before Worker admission."""

        return await self.reconcile_once()

    async def _reap_owner(self, owner_id: uuid.UUID) -> _OwnerDisposition:
        key = _owner_advisory_lock_key(owner_id)
        try:
            async with self._engine.connect() as connection:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": key},
                )
                if acquired is not True:
                    await _rollback_if_needed(connection)
                    return "preserved_lock"
                backend_pid = await connection.scalar(
                    text("SELECT pg_backend_pid()"),
                )
                if type(backend_pid) is not int:
                    await _rollback_if_needed(connection)
                    return "preserved_lock"
                try:
                    return await self._reap_locked_owner(
                        connection,
                        owner_id=owner_id,
                        backend_pid=backend_pid,
                    )
                finally:
                    await _rollback_if_needed(connection)
                    try:
                        released = await connection.scalar(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": key},
                        )
                        if released is not True:
                            logger.warning(
                                "Run Skill orphan advisory unlock was not confirmed",
                            )
                    except Exception:
                        logger.warning(
                            "Run Skill orphan advisory unlock failed",
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            return "preserved_unknown"

    async def _reap_locked_owner(
        self,
        connection: AsyncConnection,
        *,
        owner_id: uuid.UUID,
        backend_pid: int,
    ) -> _OwnerDisposition:
        try:
            metadata = await joined_to_thread(
                read_materialization_owner_metadata,
                self._materialization_root,
                owner_id,
            )
        except Exception:
            return "preserved_unknown"

        disposition = await self._classify_database_authority(
            connection,
            metadata,
        )
        await _rollback_if_needed(connection)
        if disposition is not None:
            return disposition

        provider_proof: ProviderRunMountOwnerAbsentProof | None = None
        if metadata.state in {"acquiring", "mounted", "release_pending"}:
            persisted_lease = _persisted_lease(metadata)
            try:
                reconciliation = await self._provider.ensure_run_readonly_mount_owner_absent_async(
                    owner_id,
                    persisted_lease=persisted_lease,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return "preserved_unknown"
            if type(reconciliation) is ProviderRunMountOwnerUnknown:
                return "preserved_unknown"
            if type(reconciliation) is not ProviderRunMountOwnerAbsentProof or not reconciliation.matches_owner(owner_id) or (persisted_lease is not None and reconciliation.provider_kind != persisted_lease.provider_kind):
                return "preserved_unknown"
            provider_proof = reconciliation

        final_disposition = await self._classify_database_authority(
            connection,
            metadata,
        )
        if final_disposition is not None:
            await _rollback_if_needed(connection)
            return final_disposition
        if not await _connection_still_owns_lock(
            connection,
            backend_pid=backend_pid,
            key=_owner_advisory_lock_key(owner_id),
        ):
            await _rollback_if_needed(connection)
            return "preserved_lock"
        await _rollback_if_needed(connection)

        try:
            deleted = await joined_to_thread(
                remove_materialization_owner_if_unchanged,
                self._materialization_root,
                metadata,
            )
        except Exception:
            return "preserved_unknown"
        if not deleted:
            return "preserved_unknown"
        return "deleted_provider_absent" if provider_proof is not None else "deleted_never_acquired"

    async def _classify_database_authority(
        self,
        connection: AsyncConnection,
        metadata: MaterializationOwnerMetadata,
    ) -> _OwnerDisposition | None:
        try:
            now = await connection.scalar(select(func.clock_timestamp()))
            row = (
                await connection.execute(
                    select(
                        JobRow.job_type,
                        JobRow.status,
                        JobRow.lease_owner_id,
                        JobRow.lease_token_hash,
                        JobRow.lease_expires_at,
                        JobAttemptRow.id,
                        JobAttemptRow.worker_id,
                        JobAttemptRow.lease_token_hash,
                        JobAttemptRow.finished_at,
                    )
                    .select_from(JobRow)
                    .outerjoin(
                        JobAttemptRow,
                        and_(
                            JobAttemptRow.job_id == JobRow.id,
                            JobAttemptRow.id == metadata.attempt_id,
                        ),
                    )
                    .where(JobRow.id == metadata.job_id),
                )
            ).one_or_none()
        except Exception:
            return "preserved_unknown"
        if now is None or getattr(now, "tzinfo", None) is None:
            return "preserved_unknown"
        if now < metadata.updated_at + self._grace:
            return "preserved_grace"
        if row is None:
            return None
        (
            job_type,
            status,
            lease_owner_id,
            job_lease_hash,
            lease_expires_at,
            attempt_id,
            attempt_worker_id,
            attempt_lease_hash,
            attempt_finished_at,
        ) = row
        if job_type != "private_run" or status not in _KNOWN_JOB_STATUSES:
            return "preserved_unknown"
        if attempt_id is None:
            return "preserved_unknown"
        exact_unfinished_owner = status in _ACTIVE_JOB_STATUSES and lease_owner_id == metadata.worker_id and attempt_worker_id == metadata.worker_id and attempt_finished_at is None
        if exact_unfinished_owner and (lease_expires_at is None or type(job_lease_hash) is not str or type(attempt_lease_hash) is not str or job_lease_hash != attempt_lease_hash):
            return "preserved_unknown"
        active = exact_unfinished_owner and lease_expires_at > now
        return "preserved_active" if active else None


def scan_materialization_owner_ids(
    root: Path,
) -> tuple[tuple[uuid.UUID, ...], int]:
    """Enumerate only immediate owner roots under the dedicated trusted root."""

    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts or root.name != "run-skill-materializations":
        raise ValueError("Invalid Run Skill materialization root")
    try:
        root_status = root.lstat()
    except FileNotFoundError:
        return (), 0
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ValueError("Untrusted materialization root")
    owner_ids: list[uuid.UUID] = []
    invalid_entries = 0
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                owner_id = uuid.UUID(hex=entry.name)
                entry_status = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                invalid_entries += 1
                continue
            if owner_id.hex != entry.name or not stat.S_ISDIR(entry_status.st_mode) or stat.S_ISLNK(entry_status.st_mode) or stat.S_IMODE(entry_status.st_mode) != 0o700:
                invalid_entries += 1
                continue
            owner_ids.append(owner_id)
    return tuple(sorted(owner_ids, key=lambda value: value.hex)), invalid_entries


def _owner_advisory_lock_key(owner_id: uuid.UUID) -> int:
    digest = hashlib.sha256(_ADVISORY_LOCK_NAMESPACE + owner_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _persisted_lease(
    metadata: MaterializationOwnerMetadata,
) -> ProviderRunMountLease | None:
    coordinates = (
        metadata.provider_kind,
        metadata.sandbox_id,
        metadata.mount_lease_id,
    )
    if all(value is None for value in coordinates):
        return None
    if any(value is None for value in coordinates):
        raise ValueError("Incomplete provider mount identity")
    return ProviderRunMountLease(
        owner_id=metadata.owner_id,
        provider_kind=metadata.provider_kind,
        sandbox_id=metadata.sandbox_id,
        mount_lease_id=metadata.mount_lease_id,
    )


async def _rollback_if_needed(connection: AsyncConnection) -> None:
    if connection.in_transaction():
        await connection.rollback()


async def _connection_still_owns_lock(
    connection: AsyncConnection,
    *,
    backend_pid: int,
    key: int,
) -> bool:
    unsigned = key & ((1 << 64) - 1)
    class_id = unsigned >> 32
    object_id = unsigned & ((1 << 32) - 1)
    try:
        observed_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        owns_lock = await connection.scalar(
            text(
                """SELECT EXISTS (
                       SELECT 1
                         FROM pg_locks
                        WHERE locktype = 'advisory'
                          AND pid = pg_backend_pid()
                          AND granted
                          AND classid = CAST(:class_id AS oid)
                          AND objid = CAST(:object_id AS oid)
                          AND objsubid = 1
                   )""",
            ),
            {
                "class_id": class_id,
                "object_id": object_id,
            },
        )
    except Exception:
        return False
    return observed_pid == backend_pid and owns_lock is True


__all__ = [
    "RunSkillTreeOrphanReapReport",
    "RunSkillTreeOrphanReaper",
    "scan_materialization_owner_ids",
]
