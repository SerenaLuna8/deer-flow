from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
            active_key_id="host-approval-audit-test",
            _keys={"host-approval-audit-test": b"h" * 32},
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
            request_id="host-approval-audit-request",
        ),
    )


def test_host_execution_approval_audit_contracts_are_closed_and_content_free() -> None:
    worker_actions = (
        AuditAction.HOST_EXECUTION_APPROVAL_REQUESTED,
        AuditAction.HOST_EXECUTION_APPROVAL_AVAILABLE,
        AuditAction.HOST_EXECUTION_APPROVAL_CLAIMED,
    )
    for action in worker_actions:
        contract = AUDIT_ACTION_CONTRACTS[action]
        assert contract.target_kind is AuditTargetKind.RUN
        assert len(contract.variants) == 1
        assert contract.variants[0].scope is AuditScope.PROJECT
        assert contract.variants[0].processes == {AuditProcess.WORKER}

    decided_contract = AUDIT_ACTION_CONTRACTS[AuditAction.HOST_EXECUTION_APPROVAL_DECIDED]
    assert decided_contract.target_kind is AuditTargetKind.RUN
    assert {variant.actor for variant in decided_contract.variants} == {"user"}

    terminal_contract = AUDIT_ACTION_CONTRACTS[AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL]
    assert terminal_contract.target_kind is AuditTargetKind.RUN
    worker_terminal = next(variant for variant in terminal_contract.variants if AuditProcess.WORKER in variant.processes)
    assert worker_terminal.metadata_equals == ()
    assert {variant.metadata_equals for variant in terminal_contract.variants if AuditProcess.GATEWAY in variant.processes} == {
        (("status", "expired"),),
        (("status", "cancelled"),),
        (("status", "unknown"),),
    }

    decision_model = AUDIT_METADATA_MODELS[AuditAction.HOST_EXECUTION_APPROVAL_DECIDED]
    assert decision_model.model_validate({"decision": "allow_once"}).decision == ("allow_once")
    with pytest.raises(ValidationError):
        decision_model.model_validate(
            {
                "decision": "deny",
                "command": "must never enter audit",
            },
        )

    terminal_model = AUDIT_METADATA_MODELS[AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL]
    for status in (
        "finished",
        "launch_failed",
        "unknown",
        "cancelled",
        "expired",
    ):
        assert terminal_model.model_validate({"status": status}).status == status
    with pytest.raises(ValidationError):
        terminal_model.model_validate(
            {
                "status": "finished",
                "approval_id": str(uuid.uuid4()),
            },
        )


@pytest.mark.asyncio
async def test_host_execution_approval_sinks_bind_closed_actor_and_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    context = _context(user_id=user_id, project_id=project_id)
    session = object()

    worker_service = _service()
    worker_append = AsyncMock()
    monkeypatch.setattr(worker_service, "append", worker_append)
    worker = OperationalAuditSink(
        worker_service,
        process_context=_bind_worker_audit_process(worker_service),
    )
    await worker.host_execution_approval_requested(
        session,
        project_id=project_id,
        source_run_id=str(run_id),
        request_id="worker-request",
        occurred_at=now,
    )
    await worker.host_execution_approval_available(
        session,
        project_id=project_id,
        source_run_id=str(run_id),
        request_id="worker-request",
        occurred_at=now,
    )
    await worker.host_execution_approval_claimed(
        session,
        project_id=project_id,
        source_run_id=str(run_id),
        request_id="worker-request",
        occurred_at=now,
    )
    await worker.host_execution_approval_terminal(
        session,
        project_id=project_id,
        source_run_id=str(run_id),
        status="finished",
        request_id="worker-request",
        occurred_at=now,
    )

    assert [call.args[2] for call in worker_append.await_args_list] == [
        AuditAction.HOST_EXECUTION_APPROVAL_REQUESTED,
        AuditAction.HOST_EXECUTION_APPROVAL_AVAILABLE,
        AuditAction.HOST_EXECUTION_APPROVAL_CLAIMED,
        AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL,
    ]
    for call in worker_append.await_args_list:
        assert call.args[3].kind is AuditTargetKind.RUN
        assert call.args[3].authority_id == run_id
        assert call.args[3].project_id == project_id
        assert call.args[4] is AuditOutcome.SUCCESS
        assert set(call.args[5]).issubset({"status"})
        assert call.kwargs.get("job_id") is None
        assert call.kwargs.get("attempt_id") is None

    gateway_service = _service()
    gateway_append = AsyncMock()
    monkeypatch.setattr(gateway_service, "append", gateway_append)
    gateway = OperationalAuditSink(
        gateway_service,
        process_context=_bind_gateway_audit_process(gateway_service),
    )
    await gateway.host_execution_approval_decided(
        session,
        context,
        source_run_id=str(run_id),
        decision="deny",
        occurred_at=now,
    )
    await gateway.host_execution_approval_terminal(
        session,
        project_id=project_id,
        source_run_id=str(run_id),
        status="expired",
        request_id=context.request_id,
        occurred_at=now,
    )
    assert [call.args[2] for call in gateway_append.await_args_list] == [
        AuditAction.HOST_EXECUTION_APPROVAL_DECIDED,
        AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL,
    ]
    assert gateway_append.await_args_list[0].args[1].user_id == user_id
    assert gateway_append.await_args_list[0].args[5] == {"decision": "deny"}

    with pytest.raises(AuditAuthorityRejected):
        await gateway.host_execution_approval_requested(
            session,
            project_id=project_id,
            source_run_id=str(run_id),
            request_id=context.request_id,
            occurred_at=now,
        )
    with pytest.raises(AuditAuthorityRejected):
        await worker.host_execution_approval_decided(
            session,
            context,
            source_run_id=str(run_id),
            decision="allow_once",
            occurred_at=now,
        )

    closed_gateway_service = _service()
    closed_gateway = OperationalAuditSink(
        closed_gateway_service,
        process_context=_bind_gateway_audit_process(closed_gateway_service),
    )
    with pytest.raises(AuditAuthorityRejected):
        await closed_gateway.host_execution_approval_terminal(
            session,
            project_id=project_id,
            source_run_id=str(run_id),
            status="finished",
            request_id=context.request_id,
            occurred_at=now,
        )


@pytest.mark.asyncio
async def test_host_execution_audit_persists_only_hmac_private_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def append(_repository: AuditRepository, **values: object):
        captured.update(values)
        return SimpleNamespace(id=uuid.uuid4(), **values)

    monkeypatch.setattr(AuditRepository, "append", append)
    service = _service()
    worker = OperationalAuditSink(
        service,
        process_context=_bind_worker_audit_process(service),
    )
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    request_id = "host-approval-private-correlation"
    await worker.host_execution_approval_requested(
        object(),
        project_id=project_id,
        source_run_id=str(run_id),
        request_id=request_id,
        occurred_at=datetime.now(UTC),
    )

    assert captured["project_id"] == project_id
    assert captured["target_kind"] == "run"
    assert captured["target_ref_hmac"] != str(run_id)
    assert captured["request_id"] != request_id
    assert captured["job_id"] is None
    assert captured["attempt_id"] is None
    assert captured["metadata_json"] == {}
    assert str(run_id) not in repr(captured)
    assert request_id not in repr(captured)
