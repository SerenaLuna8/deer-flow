from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work import checkpointer as checkpointer_module
from app.private_work import retention_purge as retention_purge_module
from app.private_work import run_service as run_service_module
from app.private_work.errors import PrivateWorkConflict
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
)
from app.private_work.retention_purge import RetentionExecutionApprovalActive
from app.private_work.run_repository import PrivateRunConflict
from app.private_work.run_service import PrivateRunService


def _approval_row() -> SimpleNamespace:
    return SimpleNamespace(
        id="approval-id",
        project_id="project-id",
        owner_user_id="owner-id",
        thread_id="thread-id",
        source_run_id="source-run-id",
        status="pending",
        version=1,
        terminal_at=None,
        updated_at=None,
    )


def _audit() -> SimpleNamespace:
    return SimpleNamespace(
        host_execution_approval_terminal=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_thread_cancel_transitions_output_delivery_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = AsyncMock()
    monkeypatch.setattr(
        checkpointer_module,
        "transition_output_delivery_obligation_for_approval_terminal",
        transition,
    )
    saver = object.__new__(checkpointer_module._ScopedCheckpointSaver)
    saver._approval_audit = _audit()
    saver._context = SimpleNamespace(request_id="request-id")
    row = _approval_row()
    now = datetime.now(UTC)
    session = object()

    await saver._cancel_thread_execution_approval(session, row, now=now)

    transition.assert_awaited_once_with(
        session,
        approval=row,
        approval_status="cancelled",
        now=now,
    )
    saver._approval_audit.host_execution_approval_terminal.assert_awaited_once()
    assert row.status == "cancelled"
    assert row.version == 2
    assert row.terminal_at == now


@pytest.mark.asyncio
async def test_run_cancel_transitions_output_delivery_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = AsyncMock()
    monkeypatch.setattr(
        run_service_module,
        "transition_output_delivery_obligation_for_approval_terminal",
        transition,
    )
    service = object.__new__(PrivateRunService)
    service._approval_audit = _audit()
    row = _approval_row()
    context = SimpleNamespace(request_id="request-id")
    now = datetime.now(UTC)
    session = object()

    await service._cancel_execution_approval(session, context, row, now=now)

    transition.assert_awaited_once_with(
        session,
        approval=row,
        approval_status="cancelled",
        now=now,
    )
    service._approval_audit.host_execution_approval_terminal.assert_awaited_once()
    assert row.status == "cancelled"
    assert row.version == 2
    assert row.terminal_at == now


@pytest.mark.asyncio
async def test_retention_cancel_transitions_output_delivery_before_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transition = AsyncMock()
    monkeypatch.setattr(
        retention_purge_module,
        "transition_output_delivery_obligation_for_approval_terminal",
        transition,
    )
    audit = _audit()
    row = _approval_row()
    now = datetime.now(UTC)
    session = object()

    await retention_purge_module._cancel_retention_approval(
        session,
        row,
        now=now,
        request_id="request-id",
        audit=audit,
    )

    transition.assert_awaited_once_with(
        session,
        approval=row,
        approval_status="cancelled",
        now=now,
    )
    audit.host_execution_approval_terminal.assert_awaited_once()
    assert row.status == "cancelled"
    assert row.version == 2
    assert row.terminal_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "expected_error"),
    [
        ("thread", PrivateWorkConflict),
        ("run", PrivateRunConflict),
        ("retention", RetentionExecutionApprovalActive),
    ],
)
async def test_direct_cancel_fails_closed_for_terminal_obligation(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    expected_error: type[Exception],
) -> None:
    transition = AsyncMock(side_effect=OutputDeliveryObligationConflict())
    audit = _audit()
    row = _approval_row()
    now = datetime.now(UTC)
    session = object()

    if entrypoint == "thread":
        monkeypatch.setattr(
            checkpointer_module,
            "transition_output_delivery_obligation_for_approval_terminal",
            transition,
        )
        saver = object.__new__(checkpointer_module._ScopedCheckpointSaver)
        saver._approval_audit = audit
        saver._context = SimpleNamespace(request_id="request-id")
        invocation = saver._cancel_thread_execution_approval(
            session,
            row,
            now=now,
        )
    elif entrypoint == "run":
        monkeypatch.setattr(
            run_service_module,
            "transition_output_delivery_obligation_for_approval_terminal",
            transition,
        )
        service = object.__new__(PrivateRunService)
        service._approval_audit = audit
        invocation = service._cancel_execution_approval(
            session,
            SimpleNamespace(request_id="request-id"),
            row,
            now=now,
        )
    else:
        monkeypatch.setattr(
            retention_purge_module,
            "transition_output_delivery_obligation_for_approval_terminal",
            transition,
        )
        invocation = retention_purge_module._cancel_retention_approval(
            session,
            row,
            now=now,
            request_id="request-id",
            audit=audit,
        )

    with pytest.raises(expected_error):
        await invocation

    audit.host_execution_approval_terminal.assert_not_awaited()
    assert row.status == "pending"
    assert row.version == 1
    assert row.terminal_at is None
