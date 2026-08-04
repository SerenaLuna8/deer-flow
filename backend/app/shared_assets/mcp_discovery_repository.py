"""Durable, project-scoped MCP discovery request persistence."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobIdempotencyConflict,
    JobRepository,
    JobScope,
)
from deerflow.persistence.shared_assets import (
    McpServerRow,
    McpServerVersionRow,
    McpToolDiscoveryAttemptRow,
)

McpToolDiscoveryTrigger = Literal["auto", "manual"]
McpToolDiscoveryResultStatus = Literal["succeeded", "failed", "cancelled"]
McpToolDiscoveryStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
McpToolDiscoveryErrorCode = Literal[
    "mcp_discovery_unavailable",
    "mcp_catalog_invalid",
]

_ACTIVE_JOB_STATUSES = frozenset({"queued", "leased", "running", "retry_wait"})
_MCP_DISCOVERY_ERROR_CODES = frozenset({"mcp_discovery_unavailable", "mcp_catalog_invalid"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class McpToolDiscoveryResultConflict(RuntimeError):
    """A durable discovery result cannot be replaced by another outcome."""


@dataclass(frozen=True, slots=True)
class McpToolDiscoveryAttemptRecord:
    attempt_id: uuid.UUID
    project_id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    requested_by_user_id: str
    trigger: McpToolDiscoveryTrigger
    payload_checksum: str
    grant_digest: str
    status: McpToolDiscoveryStatus
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    public_error_code: McpToolDiscoveryErrorCode | None
    revision: int


def _uuid(value: object, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a UUID") from None


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _status(
    job_status: str,
    result_status: str | None,
) -> McpToolDiscoveryStatus:
    if job_status == "queued":
        return "queued"
    if job_status in {"leased", "running", "retry_wait"}:
        return "running"
    if job_status == "succeeded":
        if result_status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("invalid persisted MCP discovery terminal result")
        return cast(McpToolDiscoveryStatus, result_status)
    if job_status in {"failed", "dead"}:
        return "failed"
    if job_status == "cancelled":
        return "cancelled"
    raise ValueError("invalid persisted MCP discovery job status")


class McpToolDiscoveryAttemptRepository:
    """Session-bound discovery admission and response projection.

    The caller owns the transaction. Discovery jobs are deliberately
    single-attempt and unsafe to replay because a lost lease can leave the
    remote side-effect state unknown.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def enqueue(
        self,
        *,
        project_id: uuid.UUID,
        requested_by_user_id: str,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        payload_checksum: str,
        grant_digest: str,
        trigger: McpToolDiscoveryTrigger,
        idempotency_key: str,
    ) -> McpToolDiscoveryAttemptRecord:
        project = _uuid(project_id, field="project_id")
        requester = str(_uuid(requested_by_user_id, field="requested_by_user_id"))
        asset = _uuid(mcp_server_id, field="mcp_server_id")
        version = _uuid(mcp_server_version_id, field="mcp_server_version_id")
        checksum = _digest(payload_checksum, field="payload_checksum")
        closure_digest = _digest(grant_digest, field="grant_digest")
        if trigger not in {"auto", "manual"}:
            raise ValueError("unsupported MCP discovery trigger")

        await self._require_visible_version(
            project_id=project,
            mcp_server_id=asset,
            mcp_server_version_id=version,
            payload_checksum=checksum,
        )
        requested_at = datetime.now(UTC)
        job_id = await JobRepository(self.session).enqueue(
            EnqueueJob(
                job_type="mcp_discovery",
                scope=JobScope(project, requester),
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                origin_trace_id=None,
                max_attempts=1,
                retry_safety="unsafe",
            )
        )
        await self.session.scalar(
            insert(McpToolDiscoveryAttemptRow)
            .values(
                job_id=job_id,
                project_id=project,
                mcp_server_id=asset,
                mcp_server_version_id=version,
                requested_by_user_id=requester,
                trigger=trigger,
                payload_checksum=checksum,
                grant_digest=closure_digest,
                requested_at=requested_at,
                revision=1,
            )
            .on_conflict_do_nothing(
                index_elements=[McpToolDiscoveryAttemptRow.job_id],
            )
            .returning(McpToolDiscoveryAttemptRow.job_id)
        )
        record = await self.get(project, job_id)
        if record is None:
            raise RuntimeError("MCP discovery attempt admission was not persisted")
        if record.requested_by_user_id != requester or record.mcp_server_id != asset or record.mcp_server_version_id != version or record.payload_checksum != checksum or record.grant_digest != closure_digest or record.trigger != trigger:
            raise JobIdempotencyConflict("MCP discovery idempotency target conflict")
        return record

    async def get(
        self,
        project_id: uuid.UUID,
        attempt_id: uuid.UUID,
    ) -> McpToolDiscoveryAttemptRecord | None:
        project = _uuid(project_id, field="project_id")
        attempt = _uuid(attempt_id, field="attempt_id")
        row = (
            await self.session.execute(
                self._record_statement().where(
                    McpToolDiscoveryAttemptRow.project_id == project,
                    McpToolDiscoveryAttemptRow.job_id == attempt,
                )
            )
        ).one_or_none()
        return None if row is None else self._record(row)

    async def latest_for_version(
        self,
        project_id: uuid.UUID,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> McpToolDiscoveryAttemptRecord | None:
        project = _uuid(project_id, field="project_id")
        asset = _uuid(asset_id, field="asset_id")
        version = _uuid(version_id, field="version_id")
        row = (
            await self.session.execute(
                self._record_statement()
                .where(
                    McpToolDiscoveryAttemptRow.project_id == project,
                    McpToolDiscoveryAttemptRow.mcp_server_id == asset,
                    McpToolDiscoveryAttemptRow.mcp_server_version_id == version,
                )
                .order_by(
                    McpToolDiscoveryAttemptRow.requested_at.desc(),
                    McpToolDiscoveryAttemptRow.job_id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else self._record(row)

    async def active_for_closure(
        self,
        project_id: uuid.UUID,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        payload_checksum: str,
        grant_digest: str,
    ) -> McpToolDiscoveryAttemptRecord | None:
        project = _uuid(project_id, field="project_id")
        asset = _uuid(asset_id, field="asset_id")
        version = _uuid(version_id, field="version_id")
        checksum = _digest(payload_checksum, field="payload_checksum")
        closure_digest = _digest(grant_digest, field="grant_digest")
        row = (
            await self.session.execute(
                self._record_statement()
                .where(
                    McpToolDiscoveryAttemptRow.project_id == project,
                    McpToolDiscoveryAttemptRow.mcp_server_id == asset,
                    McpToolDiscoveryAttemptRow.mcp_server_version_id == version,
                    McpToolDiscoveryAttemptRow.payload_checksum == checksum,
                    McpToolDiscoveryAttemptRow.grant_digest == closure_digest,
                    JobRow.status.in_(_ACTIVE_JOB_STATUSES),
                )
                .order_by(
                    McpToolDiscoveryAttemptRow.requested_at.desc(),
                    McpToolDiscoveryAttemptRow.job_id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else self._record(row)

    async def mark_result(
        self,
        attempt_id: uuid.UUID,
        result_status: McpToolDiscoveryResultStatus,
        public_error_code: McpToolDiscoveryErrorCode | None,
    ) -> McpToolDiscoveryAttemptRecord:
        attempt = _uuid(attempt_id, field="attempt_id")
        if result_status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("unsupported MCP discovery result status")
        if result_status == "failed":
            if public_error_code not in _MCP_DISCOVERY_ERROR_CODES:
                raise ValueError("failed MCP discovery requires a stable public error code")
        elif public_error_code is not None:
            raise ValueError("successful or cancelled MCP discovery cannot carry an error code")

        pair = (
            await self.session.execute(
                select(McpToolDiscoveryAttemptRow, JobRow)
                .join(JobRow, JobRow.id == McpToolDiscoveryAttemptRow.job_id)
                .where(
                    McpToolDiscoveryAttemptRow.job_id == attempt,
                    JobRow.job_type == "mcp_discovery",
                )
                .with_for_update(of=(McpToolDiscoveryAttemptRow, JobRow))
            )
        ).one_or_none()
        if pair is None:
            raise ValueError("MCP discovery attempt is unavailable")
        row, job = pair
        if row.result_status is None:
            if job.status not in {"leased", "running"}:
                raise ValueError("MCP discovery attempt is not running")
            row.result_status = result_status
            row.public_error_code = public_error_code
            row.revision += 1
            await self.session.flush()
        elif row.result_status != result_status or row.public_error_code != public_error_code:
            raise McpToolDiscoveryResultConflict("MCP discovery result is already terminal")
        return self._record((row, job))

    @staticmethod
    def _record_statement():
        return select(McpToolDiscoveryAttemptRow, JobRow).join(
            JobRow,
            and_(
                JobRow.id == McpToolDiscoveryAttemptRow.job_id,
                JobRow.project_id == McpToolDiscoveryAttemptRow.project_id,
                JobRow.owner_user_id == McpToolDiscoveryAttemptRow.requested_by_user_id,
                JobRow.job_type == "mcp_discovery",
            ),
        )

    @staticmethod
    def _record(row) -> McpToolDiscoveryAttemptRecord:
        attempt, job = row
        if (
            attempt.trigger not in {"auto", "manual"}
            or _HEX_DIGEST.fullmatch(attempt.payload_checksum) is None
            or _HEX_DIGEST.fullmatch(attempt.grant_digest) is None
            or attempt.result_status not in {None, "succeeded", "failed", "cancelled"}
            or (attempt.result_status == "failed" and attempt.public_error_code not in _MCP_DISCOVERY_ERROR_CODES)
            or (attempt.result_status != "failed" and attempt.public_error_code is not None)
            or attempt.revision < 1
        ):
            raise ValueError("invalid persisted MCP discovery attempt")
        status = _status(job.status, attempt.result_status)
        error_code = attempt.public_error_code
        if status == "failed" and error_code is None:
            error_code = "mcp_discovery_unavailable"
        elif status != "failed":
            error_code = None
        return McpToolDiscoveryAttemptRecord(
            attempt_id=attempt.job_id,
            project_id=attempt.project_id,
            mcp_server_id=attempt.mcp_server_id,
            mcp_server_version_id=attempt.mcp_server_version_id,
            requested_by_user_id=attempt.requested_by_user_id,
            trigger=cast(McpToolDiscoveryTrigger, attempt.trigger),
            payload_checksum=attempt.payload_checksum,
            grant_digest=attempt.grant_digest,
            status=status,
            requested_at=attempt.requested_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            public_error_code=cast(
                McpToolDiscoveryErrorCode | None,
                error_code,
            ),
            revision=attempt.revision,
        )

    async def _require_visible_version(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        payload_checksum: str,
    ) -> None:
        found = await self.session.scalar(
            select(McpServerVersionRow.id)
            .join(
                McpServerRow,
                McpServerRow.id == McpServerVersionRow.mcp_server_id,
            )
            .where(
                McpServerVersionRow.id == mcp_server_version_id,
                McpServerVersionRow.mcp_server_id == mcp_server_id,
                McpServerVersionRow.payload_checksum == payload_checksum,
                McpServerVersionRow.workflow_status == "published",
                or_(
                    and_(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == project_id,
                    ),
                    and_(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                    ),
                ),
            )
        )
        if found is None:
            raise ValueError("MCP discovery target is unavailable")


__all__ = [
    "McpToolDiscoveryAttemptRecord",
    "McpToolDiscoveryAttemptRepository",
    "McpToolDiscoveryErrorCode",
    "McpToolDiscoveryResultConflict",
    "McpToolDiscoveryResultStatus",
    "McpToolDiscoveryStatus",
    "McpToolDiscoveryTrigger",
]
