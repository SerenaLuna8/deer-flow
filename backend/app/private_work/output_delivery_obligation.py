"""Durable output-delivery obligations attached to host execution approvals.

The source Run may commit output files before a host command reaches its
approval boundary.  Those files remain a server-owned delivery obligation for
the exact approval continuation; browser metadata and model messages are never
used as authority for this lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryCandidateRow,
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
)
from deerflow.persistence.private_work import PrivateArtifactRow, PrivateFileRow

_OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs/"
_MAX_PRESENTED_PATHS = 256


class OutputDeliveryObligationConflict(RuntimeError):
    """Persisted obligation coordinates or lifecycle state are inconsistent."""


@dataclass(frozen=True, slots=True)
class OutputDeliveryCandidateSnapshot:
    file_id: uuid.UUID
    logical_path: str
    file_version: int
    sha256: str


@dataclass(frozen=True, slots=True)
class OutputDeliveryObligationSnapshot:
    approval_id: uuid.UUID
    mode: str
    status: str
    continuation_run_id: str
    continuation_job_id: uuid.UUID
    candidates: tuple[OutputDeliveryCandidateSnapshot, ...]


def _intent_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass"),
    ).hexdigest()


def _canonical_uuid(value: object) -> uuid.UUID:
    """Normalize trusted internal coordinates without widening their syntax."""

    if isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    if type(value) is not str:
        raise OutputDeliveryObligationConflict()
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise OutputDeliveryObligationConflict() from None
    if str(parsed) != value:
        raise OutputDeliveryObligationConflict()
    return parsed


def _normalize_intent_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    if type(paths) is not tuple or not paths or len(paths) > _MAX_PRESENTED_PATHS:
        raise OutputDeliveryObligationConflict()
    logical_paths: set[str] = set()
    for path in paths:
        if (
            type(path) is not str
            or not path.startswith(_OUTPUTS_VIRTUAL_PREFIX)
            or len(path) <= len(_OUTPUTS_VIRTUAL_PREFIX)
            or len(path) > len("/mnt/user-data/") + 1024
            or "\\" in path
            or "//" in path
            or any(part in {"", ".", ".."} for part in path.split("/")[4:])
        ):
            raise OutputDeliveryObligationConflict()
        logical_paths.add(f"outputs/{path.removeprefix(_OUTPUTS_VIRTUAL_PREFIX)}")
    return tuple(sorted(logical_paths))


def _same_private_scope(
    obligation: ExecutionApprovalOutputDeliveryObligationRow,
    approval: ExecutionApprovalRequestRow,
) -> bool:
    return obligation.approval_id == approval.id and obligation.project_id == approval.project_id and obligation.owner_user_id == approval.owner_user_id and obligation.thread_id == approval.thread_id


async def _lock_by_approval_id(
    session: AsyncSession,
    approval_id: uuid.UUID,
) -> ExecutionApprovalOutputDeliveryObligationRow | None:
    return await session.scalar(
        sa.select(ExecutionApprovalOutputDeliveryObligationRow)
        .where(
            ExecutionApprovalOutputDeliveryObligationRow.approval_id == approval_id,
        )
        .with_for_update(of=ExecutionApprovalOutputDeliveryObligationRow)
        .execution_options(populate_existing=True)
    )


async def _candidate_snapshots(
    session: AsyncSession,
    approval_id: uuid.UUID,
) -> tuple[OutputDeliveryCandidateSnapshot, ...]:
    rows = tuple(
        (
            await session.scalars(
                sa.select(ExecutionApprovalOutputDeliveryCandidateRow)
                .where(
                    ExecutionApprovalOutputDeliveryCandidateRow.approval_id == approval_id,
                )
                .order_by(
                    ExecutionApprovalOutputDeliveryCandidateRow.logical_path,
                    ExecutionApprovalOutputDeliveryCandidateRow.file_id,
                )
            )
        ).all()
    )
    return tuple(
        OutputDeliveryCandidateSnapshot(
            file_id=row.file_id,
            logical_path=row.logical_path,
            file_version=row.file_version,
            sha256=row.sha256,
        )
        for row in rows
    )


async def seal_source_output_delivery_obligation(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    now: datetime,
) -> ExecutionApprovalOutputDeliveryObligationRow | None:
    """Rebuild and seal an ``any_one`` obligation from source DB authority.

    The approval row must already be locked and still staged.  A source Run
    with no ready output, or with at least one output already represented by a
    live source-Run Artifact, has no outstanding obligation and therefore does
    not need a row.
    """

    if approval.status != "staged":
        raise OutputDeliveryObligationConflict()
    existing = await _lock_by_approval_id(session, approval.id)
    if existing is not None:
        if not _same_private_scope(existing, approval):
            raise OutputDeliveryObligationConflict()
        if existing.mode != "any_one" or existing.status != "deferred" or existing.continuation_run_id is not None or existing.continuation_job_id is not None:
            raise OutputDeliveryObligationConflict()

    output_files = tuple(
        (
            await session.scalars(
                sa.select(PrivateFileRow)
                .where(
                    PrivateFileRow.project_id == approval.project_id,
                    PrivateFileRow.owner_user_id == approval.owner_user_id,
                    PrivateFileRow.thread_id == approval.thread_id,
                    PrivateFileRow.created_by_run_id == approval.source_run_id,
                    PrivateFileRow.kind == "output",
                    PrivateFileRow.logical_path.like("outputs/%"),
                    PrivateFileRow.logical_path != "outputs/",
                    PrivateFileRow.status == "ready",
                    PrivateFileRow.deleted_at.is_(None),
                )
                .order_by(PrivateFileRow.logical_path, PrivateFileRow.id)
                .with_for_update(of=PrivateFileRow)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not output_files:
        if existing is not None:
            raise OutputDeliveryObligationConflict()
        return None

    output_file_ids = tuple(row.id for row in output_files)
    presented_file_id = await session.scalar(
        sa.select(PrivateArtifactRow.file_id)
        .where(
            PrivateArtifactRow.project_id == approval.project_id,
            PrivateArtifactRow.owner_user_id == approval.owner_user_id,
            PrivateArtifactRow.thread_id == approval.thread_id,
            PrivateArtifactRow.run_id == approval.source_run_id,
            PrivateArtifactRow.file_id.in_(output_file_ids),
            PrivateArtifactRow.deleted_at.is_(None),
        )
        .limit(1)
    )
    if presented_file_id is not None:
        if existing is not None:
            raise OutputDeliveryObligationConflict()
        return None

    if existing is not None:
        persisted_candidates = await _candidate_snapshots(session, approval.id)
        expected_candidates = tuple(
            OutputDeliveryCandidateSnapshot(
                file_id=row.id,
                logical_path=row.logical_path,
                file_version=row.version,
                sha256=row.sha256,
            )
            for row in output_files
        )
        if persisted_candidates != expected_candidates:
            raise OutputDeliveryObligationConflict()
        return existing

    obligation = ExecutionApprovalOutputDeliveryObligationRow(
        approval_id=approval.id,
        project_id=approval.project_id,
        owner_user_id=approval.owner_user_id,
        thread_id=approval.thread_id,
        mode="any_one",
        status="deferred",
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(obligation)
    await session.flush()
    session.add_all(
        [
            ExecutionApprovalOutputDeliveryCandidateRow(
                approval_id=approval.id,
                file_id=row.id,
                project_id=approval.project_id,
                owner_user_id=approval.owner_user_id,
                thread_id=approval.thread_id,
                logical_path=row.logical_path,
                file_version=row.version,
                sha256=row.sha256,
                created_at=now,
            )
            for row in output_files
        ]
    )
    await session.flush()
    return obligation


async def assign_output_delivery_obligation(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    continuation_run_id: str,
    continuation_job_id: uuid.UUID,
    now: datetime,
) -> ExecutionApprovalOutputDeliveryObligationRow | None:
    """Bind a deferred obligation to the exact admitted continuation."""

    if approval.status != "approved" or approval.decision != "allow_once" or approval.continuation_run_id != continuation_run_id or approval.continuation_job_id != continuation_job_id:
        raise OutputDeliveryObligationConflict()
    obligation = await _lock_by_approval_id(session, approval.id)
    if obligation is None:
        return None
    if not _same_private_scope(obligation, approval):
        raise OutputDeliveryObligationConflict()
    if obligation.status == "assigned":
        if obligation.continuation_run_id != continuation_run_id or obligation.continuation_job_id != continuation_job_id:
            raise OutputDeliveryObligationConflict()
        return obligation
    if obligation.status != "deferred" or obligation.continuation_run_id is not None or obligation.continuation_job_id is not None:
        raise OutputDeliveryObligationConflict()
    obligation.status = "assigned"
    obligation.continuation_run_id = continuation_run_id
    obligation.continuation_job_id = continuation_job_id
    obligation.assigned_at = now
    obligation.version += 1
    obligation.updated_at = now
    await session.flush()
    return obligation


async def record_output_delivery_intent_in_session(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    continuation_run_id: str,
    continuation_job_id: uuid.UUID,
    presented_paths: tuple[str, ...],
    tool_call_id: str,
    now: datetime,
) -> ExecutionApprovalOutputDeliveryObligationRow | None:
    """Persist one idempotent, exact-continuation ``present_files`` intent."""

    if type(tool_call_id) is not str or not tool_call_id or tool_call_id.strip() != tool_call_id or len(tool_call_id) > 128:
        raise OutputDeliveryObligationConflict()
    logical_paths = _normalize_intent_paths(presented_paths)
    if approval.continuation_run_id != continuation_run_id or approval.continuation_job_id != continuation_job_id or approval.decision != "allow_once" or approval.status not in {"approved", "claimed", "finished", "launch_failed"}:
        raise OutputDeliveryObligationConflict()
    obligation = await _lock_by_approval_id(session, approval.id)
    if obligation is None:
        return None
    if not _same_private_scope(obligation, approval) or obligation.continuation_run_id != continuation_run_id or obligation.continuation_job_id != continuation_job_id:
        raise OutputDeliveryObligationConflict()
    candidate_paths = {candidate.logical_path for candidate in await _candidate_snapshots(session, approval.id)}
    payload = {
        "schema_version": 1,
        "logical_paths": list(logical_paths),
    }
    digest = _intent_digest(payload)
    if candidate_paths.isdisjoint(logical_paths):
        raise OutputDeliveryObligationConflict()
    if obligation.status in {"intent_recorded", "delivered"}:
        # The v1 schema owns one durable intent slot. A replay may arrive with
        # a new ToolCall id after checkpoint loss, but it is idempotent only
        # when it names the exact same normalized paths. Never acknowledge a
        # second path set that exists only in Worker memory: takeover would
        # otherwise restore the older payload and silently lose an Artifact.
        if obligation.intent_digest != digest or obligation.intent_private_json != payload:
            raise OutputDeliveryObligationConflict()
        return obligation
    if obligation.status != "assigned":
        raise OutputDeliveryObligationConflict()
    obligation.status = "intent_recorded"
    obligation.intent_tool_call_id = tool_call_id
    obligation.intent_digest = digest
    obligation.intent_private_json = payload
    obligation.intent_recorded_at = now
    obligation.version += 1
    obligation.updated_at = now
    await session.flush()
    return obligation


async def deliver_output_obligation_in_session(
    session: AsyncSession,
    *,
    approval_id: uuid.UUID,
    project_id: uuid.UUID,
    owner_user_id: str,
    thread_id: str,
    continuation_run_id: str,
    logical_path: str,
    artifact_id: uuid.UUID,
    now: datetime,
) -> bool:
    """CAS an intent to delivered when an exact continuation Artifact exists."""

    obligation = await session.scalar(
        sa.select(ExecutionApprovalOutputDeliveryObligationRow)
        .where(
            ExecutionApprovalOutputDeliveryObligationRow.approval_id == approval_id,
            ExecutionApprovalOutputDeliveryObligationRow.project_id == project_id,
            ExecutionApprovalOutputDeliveryObligationRow.owner_user_id == owner_user_id,
            ExecutionApprovalOutputDeliveryObligationRow.thread_id == thread_id,
            ExecutionApprovalOutputDeliveryObligationRow.continuation_run_id == continuation_run_id,
        )
        .with_for_update(of=ExecutionApprovalOutputDeliveryObligationRow)
        .execution_options(populate_existing=True)
    )
    if obligation is None:
        return False
    if obligation.status == "delivered":
        return obligation.satisfied_artifact_id == artifact_id
    if obligation.status != "intent_recorded":
        raise OutputDeliveryObligationConflict()
    intent = obligation.intent_private_json
    intent_paths = intent.get("logical_paths") if isinstance(intent, dict) else None
    if not isinstance(intent_paths, list) or any(type(path) is not str for path in intent_paths) or logical_path not in intent_paths:
        return False

    artifact = await session.scalar(
        sa.select(PrivateArtifactRow).where(
            PrivateArtifactRow.id == artifact_id,
            PrivateArtifactRow.project_id == project_id,
            PrivateArtifactRow.owner_user_id == owner_user_id,
            PrivateArtifactRow.thread_id == thread_id,
            PrivateArtifactRow.run_id == continuation_run_id,
            PrivateArtifactRow.deleted_at.is_(None),
        )
    )
    if artifact is None:
        raise OutputDeliveryObligationConflict()
    file_row = await session.scalar(
        sa.select(PrivateFileRow).where(
            PrivateFileRow.id == artifact.file_id,
            PrivateFileRow.project_id == project_id,
            PrivateFileRow.owner_user_id == owner_user_id,
            PrivateFileRow.thread_id == thread_id,
            PrivateFileRow.logical_path == logical_path,
            PrivateFileRow.kind == "output",
            PrivateFileRow.status == "ready",
            PrivateFileRow.deleted_at.is_(None),
        )
    )
    if file_row is None:
        raise OutputDeliveryObligationConflict()
    candidate = await session.scalar(
        sa.select(ExecutionApprovalOutputDeliveryCandidateRow).where(
            ExecutionApprovalOutputDeliveryCandidateRow.approval_id == obligation.approval_id,
            ExecutionApprovalOutputDeliveryCandidateRow.project_id == project_id,
            ExecutionApprovalOutputDeliveryCandidateRow.owner_user_id == owner_user_id,
            ExecutionApprovalOutputDeliveryCandidateRow.thread_id == thread_id,
            ExecutionApprovalOutputDeliveryCandidateRow.logical_path == logical_path,
        )
    )
    if candidate is None:
        return False
    exact_candidate = file_row.id == candidate.file_id and file_row.version == candidate.file_version and file_row.sha256 == candidate.sha256
    continuation_successor = file_row.created_by_run_id == continuation_run_id
    if not exact_candidate and not continuation_successor:
        raise OutputDeliveryObligationConflict()

    obligation.status = "delivered"
    obligation.satisfied_artifact_id = artifact_id
    obligation.version += 1
    obligation.terminal_at = now
    obligation.updated_at = now
    await session.flush()
    return True


async def transition_output_delivery_obligation_for_approval_terminal(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    approval_status: str,
    now: datetime,
) -> None:
    """Converge an obligation when its owning approval becomes terminal."""

    target_status = {
        "denied": "cancelled",
        "expired": "cancelled",
        "cancelled": "cancelled",
        "unknown": "blocked_unknown",
    }.get(approval_status)
    if target_status is None:
        return
    obligation = await _lock_by_approval_id(session, approval.id)
    if obligation is None:
        return
    if not _same_private_scope(obligation, approval):
        raise OutputDeliveryObligationConflict()
    if obligation.status == target_status:
        return
    if obligation.status in {"delivered", "cancelled", "blocked_unknown", "failed"}:
        raise OutputDeliveryObligationConflict()
    obligation.status = target_status
    obligation.version += 1
    obligation.terminal_at = now
    obligation.updated_at = now
    await session.flush()


async def settle_continuation_output_delivery(
    session: AsyncSession,
    *,
    approval_id_value: object,
    project_id: uuid.UUID | str,
    owner_user_id: str,
    thread_id: str,
    continuation_run_id: str,
    continuation_job_id: uuid.UUID | str,
    settled_status: str,
    now: datetime,
    ambiguous_side_effect: bool = False,
) -> None:
    """Apply the authoritative continuation terminal gate in its transaction."""

    if type(ambiguous_side_effect) is not bool:
        raise OutputDeliveryObligationConflict()
    if approval_id_value is None:
        return
    if type(approval_id_value) is not str:
        raise OutputDeliveryObligationConflict()
    approval_id = _canonical_uuid(approval_id_value)
    project_uuid = _canonical_uuid(project_id)
    continuation_job_uuid = _canonical_uuid(continuation_job_id)
    approval = await session.scalar(
        sa.select(ExecutionApprovalRequestRow)
        .where(
            ExecutionApprovalRequestRow.id == approval_id,
            ExecutionApprovalRequestRow.project_id == project_uuid,
            ExecutionApprovalRequestRow.owner_user_id == owner_user_id,
            ExecutionApprovalRequestRow.thread_id == thread_id,
            ExecutionApprovalRequestRow.continuation_run_id == continuation_run_id,
            ExecutionApprovalRequestRow.continuation_job_id == continuation_job_uuid,
        )
        .with_for_update(of=ExecutionApprovalRequestRow)
        .execution_options(populate_existing=True)
    )
    if approval is None:
        raise OutputDeliveryObligationConflict()
    obligation = await _lock_by_approval_id(session, approval_id)
    if obligation is None:
        return
    if (
        _canonical_uuid(obligation.project_id) != project_uuid
        or obligation.owner_user_id != owner_user_id
        or obligation.thread_id != thread_id
        or obligation.continuation_run_id != continuation_run_id
        or _canonical_uuid(obligation.continuation_job_id) != continuation_job_uuid
    ):
        raise OutputDeliveryObligationConflict()
    if obligation.status == "delivered":
        return
    if settled_status == "success":
        raise OutputDeliveryObligationConflict()
    if obligation.status == "blocked_unknown":
        return
    unresolved_claim = approval.status in {"claimed", "unknown"}
    target_status = (
        "blocked_unknown"
        if ambiguous_side_effect or unresolved_claim
        else {
            "error": "failed",
            "timeout": "failed",
            "interrupted": "cancelled",
        }.get(settled_status)
    )
    if target_status is None:
        raise OutputDeliveryObligationConflict()
    if obligation.status == target_status:
        return
    if obligation.status not in {"assigned", "intent_recorded"}:
        raise OutputDeliveryObligationConflict()
    obligation.status = target_status
    obligation.version += 1
    obligation.terminal_at = now
    obligation.updated_at = now
    await session.flush()


async def load_output_delivery_obligation_for_continuation(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    continuation_run_id: str,
    continuation_job_id: uuid.UUID,
    lock: bool = False,
) -> OutputDeliveryObligationSnapshot | None:
    """Load the server-owned obligation for an exact continuation boundary."""

    statement = sa.select(ExecutionApprovalOutputDeliveryObligationRow).where(
        ExecutionApprovalOutputDeliveryObligationRow.approval_id == approval.id,
    )
    if lock:
        statement = statement.with_for_update(
            of=ExecutionApprovalOutputDeliveryObligationRow,
        ).execution_options(populate_existing=True)
    obligation = await session.scalar(statement)
    if obligation is None:
        return None
    if (
        not _same_private_scope(obligation, approval)
        or approval.continuation_run_id != continuation_run_id
        or approval.continuation_job_id != continuation_job_id
        or obligation.continuation_run_id != continuation_run_id
        or obligation.continuation_job_id != continuation_job_id
        or obligation.status
        not in {
            "assigned",
            "intent_recorded",
            "delivered",
            "cancelled",
            "blocked_unknown",
            "failed",
        }
    ):
        raise OutputDeliveryObligationConflict()
    return OutputDeliveryObligationSnapshot(
        approval_id=obligation.approval_id,
        mode=obligation.mode,
        status=obligation.status,
        continuation_run_id=continuation_run_id,
        continuation_job_id=continuation_job_id,
        candidates=await _candidate_snapshots(session, approval.id),
    )


async def restore_output_delivery_intent_paths_in_session(
    session: AsyncSession,
    *,
    approval: ExecutionApprovalRequestRow,
    continuation_run_id: str,
    continuation_job_id: uuid.UUID,
) -> tuple[str, ...]:
    """Restore a committed intent after checkpoint or Worker-memory loss."""

    obligation = await _lock_by_approval_id(session, approval.id)
    if obligation is None:
        return ()
    if not _same_private_scope(obligation, approval) or obligation.continuation_run_id != continuation_run_id or obligation.continuation_job_id != continuation_job_id:
        raise OutputDeliveryObligationConflict()
    if obligation.status == "assigned":
        return ()
    if obligation.status not in {"intent_recorded", "delivered"}:
        raise OutputDeliveryObligationConflict()
    payload = obligation.intent_private_json
    logical_paths = payload.get("logical_paths") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "logical_paths"}
        or payload.get("schema_version") != 1
        or not isinstance(logical_paths, list)
        or not logical_paths
        or len(logical_paths) > _MAX_PRESENTED_PATHS
        or any(type(path) is not str or not path.startswith("outputs/") or path == "outputs/" or len(path) > 1024 or "\\" in path or "//" in path or any(part in {"", ".", ".."} for part in path.split("/")[1:]) for path in logical_paths)
        or logical_paths != sorted(set(logical_paths))
        or obligation.intent_digest != _intent_digest(payload)
    ):
        raise OutputDeliveryObligationConflict()
    return tuple(f"/mnt/user-data/{logical_path}" for logical_path in logical_paths)


__all__ = [
    "OutputDeliveryCandidateSnapshot",
    "OutputDeliveryObligationConflict",
    "OutputDeliveryObligationSnapshot",
    "assign_output_delivery_obligation",
    "deliver_output_obligation_in_session",
    "load_output_delivery_obligation_for_continuation",
    "record_output_delivery_intent_in_session",
    "restore_output_delivery_intent_paths_in_session",
    "seal_source_output_delivery_obligation",
    "settle_continuation_output_delivery",
    "transition_output_delivery_obligation_for_approval_terminal",
]
