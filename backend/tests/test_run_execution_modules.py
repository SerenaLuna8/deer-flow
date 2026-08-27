"""Compatibility and dependency contracts for the execution split."""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

import app.reliability.execution as compatibility
import app.reliability.run_execution as split
import app.reliability.run_execution.boundary as boundary_module
from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunMaterializationCancelState
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.run_execution.boundary import (
    PrivateRunExecutionBoundary,
)
from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    PrivateRunExecution,
)
from app.reliability.run_execution.errors import (
    AmbiguousExternalSideEffect,
    PermanentExecutionError,
    TransientExecutionError,
)
from app.reliability.run_execution.executor import RunAgentPrivateExecutor
from app.reliability.run_execution.handler import PrivateRunJobHandler
from app.reliability.run_execution.ports import PrivateRunExecutor
from app.reliability.run_execution.projections import (
    checkpoint_progress_cursor,
)
from app.reliability.run_execution.settlement import (
    PrivateRunJobTerminalPort,
)
from app.reliability.run_execution.stream_authority import (
    LeaseAuthorizedRunEventStore,
    LeaseAuthorizedStreamBridge,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked


def test_legacy_execution_module_reexports_public_contracts() -> None:
    assert compatibility.AgentExecutionResult is AgentExecutionResult
    assert compatibility.PrivateRunExecution is PrivateRunExecution
    assert compatibility.PrivateRunExecutionBoundary is PrivateRunExecutionBoundary
    assert compatibility.PrivateRunExecutor is PrivateRunExecutor
    assert compatibility.RunAgentPrivateExecutor is RunAgentPrivateExecutor
    assert compatibility.PrivateRunJobHandler is PrivateRunJobHandler
    assert split.RunAgentPrivateExecutor is RunAgentPrivateExecutor
    assert split.PrivateRunJobHandler is PrivateRunJobHandler
    assert compatibility.AmbiguousExternalSideEffect is AmbiguousExternalSideEffect
    assert compatibility.PermanentExecutionError is PermanentExecutionError
    assert compatibility.TransientExecutionError is TransientExecutionError
    assert compatibility.LeaseAuthorizedRunEventStore is LeaseAuthorizedRunEventStore
    assert compatibility.LeaseAuthorizedStreamBridge is LeaseAuthorizedStreamBridge
    assert compatibility.PrivateRunJobTerminalPort is PrivateRunJobTerminalPort


def test_checkpoint_progress_cursor_includes_pending_writes() -> None:
    saver = SimpleNamespace(serde=SimpleNamespace(dumps_typed=lambda value: ("json", str(value).encode())))
    base = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
        pending_writes=[],
    )
    with_pending = SimpleNamespace(
        config=base.config,
        pending_writes=[("task-1", "messages", {"value": 1})],
    )

    assert checkpoint_progress_cursor(saver, base) == "checkpoint-1"
    cursor = checkpoint_progress_cursor(saver, with_pending)
    assert cursor is not None
    assert cursor.startswith("pw:")
    assert cursor == checkpoint_progress_cursor(saver, with_pending)


def test_materialization_fence_uses_shared_governance_locks() -> None:
    source = inspect.getsource(PrivateRunExecutionBoundary._materialization_fence)
    suffix = inspect.getsource(
        PrivateRunExecutionBoundary.lock_and_assert_materialization_active_in_session,
    )

    assert 'lock_mode="share"' in source
    assert "lock=True" not in source
    assert source.index("self._revalidator.require") < source.index("self.lock_and_assert_materialization_active_in_session")
    assert source.index("self.lock_and_assert_materialization_active_in_session") < source.index("await persist()")
    assert "_revalidator" not in suffix
    assert "_factory" not in suffix
    assert "User" not in suffix


def test_context_evidence_terminal_writes_keep_lease_but_allow_user_cancel() -> None:
    active = inspect.getsource(
        PrivateRunExecutionBoundary.lock_and_assert_materialization_active_in_session,
    )
    settlement = inspect.getsource(
        PrivateRunExecutionBoundary.lock_and_assert_context_evidence_settlement_in_session,
    )
    shared = inspect.getsource(
        PrivateRunExecutionBoundary._lock_and_assert_materialization_attempt,
    )

    assert "allow_cancel_requested=False" in active
    assert "allow_cancel_requested=True" in settlement
    assert "assert_materialization_attempt_active" in shared
    assert "if not allow_cancel_requested" in shared


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ordinary_cancel_requested", "authorization_revoked"),
    [(True, False), (False, True)],
    ids=["ordinary-stop", "authorization-revoked"],
)
async def test_context_evidence_settlement_distinguishes_stop_from_authorization_revocation(
    monkeypatch: pytest.MonkeyPatch,
    ordinary_cancel_requested: bool,
    authorization_revoked: bool,
) -> None:
    locked_context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="context-evidence-cancel-priority-test",
    )
    state = PrivateRunMaterializationCancelState(
        cancel_requested=ordinary_cancel_requested,
        authorization_revoked=authorization_revoked,
    )

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def assert_materialization_attempt_active(self, **_kwargs):
            return state

    monkeypatch.setattr(boundary_module, "PrivateRunRepository", Repository)
    boundary = object.__new__(PrivateRunExecutionBoundary)
    boundary._context = PrivateWorkContext.from_project(locked_context)
    boundary._claim = SimpleNamespace(
        run_id=str(uuid.uuid4()),
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease",
    )
    boundary._expected_worker_id = uuid.uuid4()
    boundary._abort_event = None
    boundary._lease_lost = False
    boundary._authorization_revoked = False
    boundary._cancel_requested = False

    if authorization_revoked:
        with pytest.raises(AuthorizationRevoked):
            await boundary.lock_and_assert_context_evidence_settlement_in_session(
                SimpleNamespace(),
                locked_context,
            )
    else:
        await boundary.lock_and_assert_context_evidence_settlement_in_session(
            SimpleNamespace(),
            locked_context,
        )

    assert boundary.cancel_requested is True
    assert boundary.authorization_revoked is authorization_revoked
