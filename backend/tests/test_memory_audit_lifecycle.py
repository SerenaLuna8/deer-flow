from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.audit.models import (
    AUDIT_ACTION_CONTRACTS,
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditProcess,
    AuditScope,
    AuditTargetKind,
)
from app.audit.service import (
    AuditService,
    _bind_gateway_audit_process,
    _bind_scheduler_audit_process,
    _bind_worker_audit_process,
)
from app.audit.sinks import OperationalAuditSink
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.sql import AuditRepository


def _service() -> AuditService:
    return AuditService(
        None,
        AuditHmacKeyring(
            active_key_id="memory-audit-test",
            _keys={"memory-audit-test": b"m" * 32},
        ),
    )


def _context(*, user_id: uuid.UUID, project_id: uuid.UUID) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-audit-request",
        )
    )


def test_memory_lifecycle_audit_contracts_are_closed_and_content_free() -> None:
    assert AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_DREAM_ADMITTED].target_kind is AuditTargetKind.JOB
    settled_contract = AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_DREAM_SETTLED]
    assert settled_contract.target_kind is AuditTargetKind.JOB
    gateway_cancel = next(variant for variant in settled_contract.variants if AuditProcess.GATEWAY in variant.processes)
    assert gateway_cancel.metadata_equals == (("disposition", "cancelled"),)
    restore = AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_RESTORE_EXECUTED]
    assert restore.target_kind is AuditTargetKind.PROJECT
    assert restore.authority_matches_project is True
    reset = AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_RESET_EXECUTED]
    assert reset.target_kind is AuditTargetKind.ACCOUNT
    assert {variant.scope for variant in reset.variants} == {
        AuditScope.PLATFORM,
        AuditScope.PROJECT,
    }

    admitted = AUDIT_METADATA_MODELS[AuditAction.MEMORY_DREAM_ADMITTED]
    assert (
        admitted.model_validate(
            {
                "origin": "manual",
                "trigger": "budget_rewrite",
                "history_count": 0,
            }
        ).history_count
        == 0
    )
    with pytest.raises(ValidationError):
        admitted.model_validate(
            {
                "origin": "scheduled",
                "trigger": "manual_dream",
                "history_count": 1,
            }
        )
    with pytest.raises(ValidationError):
        admitted.model_validate(
            {
                "origin": "manual",
                "trigger": "manual_dream",
                "history_count": 1,
                "content": "must never enter audit",
            }
        )

    settled = AUDIT_METADATA_MODELS[AuditAction.MEMORY_DREAM_SETTLED]
    assert settled.model_validate({"disposition": "published", "version": 3}).version == 3
    with pytest.raises(ValidationError):
        settled.model_validate(
            {
                "disposition": "published",
                "version": 3,
                "public_error_code": "MEMORY_DREAM_FAILED",
            }
        )

    restore_metadata = AUDIT_METADATA_MODELS[AuditAction.MEMORY_RESTORE_EXECUTED]
    assert (
        restore_metadata.model_validate(
            {
                "source_version": 2,
                "previous_version": 4,
                "published_version": 5,
                "changed": True,
            }
        ).published_version
        == 5
    )
    with pytest.raises(ValidationError):
        restore_metadata.model_validate(
            {
                "source_version": 2,
                "previous_version": 4,
                "published_version": 6,
                "changed": True,
            }
        )


@pytest.mark.asyncio
async def test_memory_lifecycle_sinks_bind_actor_target_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    job_id = uuid.uuid4()
    context = _context(user_id=user_id, project_id=project_id)
    session = object()

    gateway_service = _service()
    gateway_append = AsyncMock()
    monkeypatch.setattr(gateway_service, "append", gateway_append)
    gateway = OperationalAuditSink(
        gateway_service,
        process_context=_bind_gateway_audit_process(gateway_service),
    )
    await gateway.memory_dream_admitted(
        session,
        project_id=project_id,
        job_id=job_id,
        request_id=context.request_id,
        origin="manual",
        trigger="manual_dream",
        history_count=2,
        context=context,
    )
    await gateway.memory_restore_executed(
        session,
        context,
        source_version=2,
        previous_version=4,
        published_version=5,
        changed=True,
    )
    await gateway.memory_reset_executed(
        session,
        user_id=user_id,
        request_id=context.request_id,
        affected_project_ids=(other_project_id, project_id, project_id),
        scopes_reset=2,
        history_entries=3,
        documents=2,
        versions=5,
        dream_runs=1,
        prepare_runs=2,
        snapshots=4,
        episodes=6,
        jobs_cancelled=1,
    )

    assert gateway_append.await_count == 5
    actions = [call.args[2] for call in gateway_append.await_args_list]
    assert actions == [
        AuditAction.MEMORY_DREAM_ADMITTED,
        AuditAction.MEMORY_RESTORE_EXECUTED,
        AuditAction.MEMORY_RESET_EXECUTED,
        AuditAction.MEMORY_RESET_EXECUTED,
        AuditAction.MEMORY_RESET_EXECUTED,
    ]
    reset_calls = gateway_append.await_args_list[2:]
    assert reset_calls[0].args[3].project_id is None
    assert {call.args[3].project_id for call in reset_calls[1:]} == {
        project_id,
        other_project_id,
    }
    assert all(call.args[4] is AuditOutcome.SUCCESS for call in reset_calls)

    scheduler_service = _service()
    scheduler_append = AsyncMock()
    monkeypatch.setattr(scheduler_service, "append", scheduler_append)
    scheduler = OperationalAuditSink(
        scheduler_service,
        process_context=_bind_scheduler_audit_process(scheduler_service),
    )
    await scheduler.memory_dream_admitted(
        session,
        project_id=project_id,
        job_id=job_id,
        request_id="memory-dream-scheduler",
        origin="scheduled",
        trigger="auto_dream",
        history_count=2,
    )
    assert scheduler_append.await_args.args[2] is AuditAction.MEMORY_DREAM_ADMITTED

    worker_service = _service()
    worker_append = AsyncMock()
    monkeypatch.setattr(worker_service, "append", worker_append)
    worker = OperationalAuditSink(
        worker_service,
        process_context=_bind_worker_audit_process(worker_service),
    )
    action_limit = AsyncMock(return_value=False)
    monkeypatch.setattr(
        AuditRepository,
        "job_action_limit_reached",
        action_limit,
    )
    settlement_session = SimpleNamespace(
        scalar=AsyncMock(return_value="succeeded"),
    )
    await worker.memory_dream_settled(
        settlement_session,
        project_id=project_id,
        job_id=job_id,
        request_id="memory-dream-worker",
        disposition="published",
        version=5,
    )
    assert worker_append.await_args.args[2] is AuditAction.MEMORY_DREAM_SETTLED
    locked_statement = settlement_session.scalar.await_args.args[0]
    assert locked_statement._for_update_arg is not None
    action_limit.assert_awaited_once_with(
        project_id=project_id,
        job_id=job_id,
        action=AuditAction.MEMORY_DREAM_SETTLED.value,
        limit=1,
    )


@pytest.mark.asyncio
async def test_memory_dream_settled_guard_allows_only_gateway_cancel_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    job_id = uuid.uuid4()
    service = _service()
    append = AsyncMock()
    monkeypatch.setattr(service, "append", append)
    guard = AsyncMock(side_effect=(False, True))
    monkeypatch.setattr(AuditRepository, "job_action_limit_reached", guard)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value="cancelled"),
    )
    gateway = OperationalAuditSink(
        service,
        process_context=_bind_gateway_audit_process(service),
    )

    assert (
        await gateway.memory_dream_settled(
            session,
            project_id=project_id,
            job_id=job_id,
            request_id="memory-reset",
            disposition="cancelled",
        )
        is True
    )
    assert (
        await gateway.memory_dream_settled(
            session,
            project_id=project_id,
            job_id=job_id,
            request_id="memory-reset-retry",
            disposition="cancelled",
        )
        is False
    )
    assert append.await_count == 1
    assert session.scalar.await_count == 2

    with pytest.raises(AuditAuthorityRejected):
        await gateway.memory_dream_settled(
            session,
            project_id=project_id,
            job_id=job_id,
            request_id="memory-reset",
            disposition="published",
            version=1,
        )
