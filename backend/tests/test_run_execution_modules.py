"""Compatibility and dependency contracts for the execution split."""

from __future__ import annotations

from types import SimpleNamespace

import app.reliability.execution as compatibility
import app.reliability.run_execution as split
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
