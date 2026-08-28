from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint

from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    RecoveredPrivateRunTerminal,
)
from app.reliability.run_execution.delegation_ledger_settlement import (
    settle_run_delegation_ledger_cancelled,
)
from app.reliability.run_execution.handler import PrivateRunJobHandler
from deerflow.agents.middlewares.delegation_ledger import (
    cancelled_delegation_updates,
    stale_delegation_updates,
)
from deerflow.agents.middlewares.durable_context_middleware import (
    DurableContextMiddleware,
)
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.runtime.private_scope import PrivateResourceScope

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000801")
OWNER_ID = "00000000-0000-0000-0000-000000000802"
RUN_ID = "run-m8"
THREAD_ID = "thread-m8"


def _entry(
    entry_id: str,
    *,
    run_id: str = RUN_ID,
    status: str = "in_progress",
    occurrence: int = 1,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "occurrence": occurrence,
        "project_id": str(PROJECT_ID),
        "owner_user_id": OWNER_ID,
        "run_id": run_id,
        "description": f"task {entry_id}",
        "subagent_type": "researcher",
        "status": status,
        "created_at": "2026-08-28T00:00:00Z",
    }


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.UUID("00000000-0000-0000-0000-000000000803"),
        attempt_id=uuid.UUID("00000000-0000-0000-0000-000000000804"),
        lease_token="lease-m8",
        job_type="private_run",
        scope=JobScope(PROJECT_ID, OWNER_ID),
        run_id=RUN_ID,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id="00000000000000000000000000000805",
    )


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_ID,
        membership_version=1,
    )


def test_cancelled_updates_are_exact_scope_terminal_and_idempotent() -> None:
    active = _entry("call-a", occurrence=2)
    terminal = _entry("call-b", status="completed")
    other_run = _entry("call-c", run_id="run-other")

    updates = cancelled_delegation_updates(
        [active, terminal, other_run],
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_ID,
        run_id=RUN_ID,
    )

    assert updates == [{**active, "status": "cancelled"}]
    assert updates[0]["occurrence"] == 2
    assert updates[0]["project_id"] == str(PROJECT_ID)
    assert updates[0]["owner_user_id"] == OWNER_ID
    assert updates[0]["run_id"] == RUN_ID
    assert (
        cancelled_delegation_updates(
            updates,
            project_id=str(PROJECT_ID),
            owner_user_id=OWNER_ID,
            run_id=RUN_ID,
        )
        == []
    )


def test_next_run_terminalizes_prior_run_stale_entries_only() -> None:
    prior = _entry("call-prior", run_id="run-prior")
    current = _entry("call-current")
    other_owner = {**_entry("call-other", run_id="run-prior"), "owner_user_id": str(uuid.uuid4())}

    updates = stale_delegation_updates(
        [prior, current, other_owner],
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_ID,
        current_run_id=RUN_ID,
    )

    assert updates == [{**prior, "status": "cancelled"}]


def test_next_run_before_model_persists_stale_terminal_update() -> None:
    prior = _entry("call-prior", run_id="run-prior")
    current = _entry("call-current")

    update = DurableContextMiddleware().before_model(
        {
            "messages": [],
            "delegations": [prior, current],
        },
        SimpleNamespace(
            context={
                "run_id": RUN_ID,
                "private_scope": _scope(),
                "current_run_pre_existing_message_ids": frozenset(),
            }
        ),
    )

    assert update is not None
    assert update["delegations"] == [{**prior, "status": "cancelled"}]


class _RecordingCancelSaver:
    def __init__(self, entries: list[dict[str, object]]) -> None:
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "00000000-0000-6000-8000-000000000001"
        checkpoint["channel_values"] = {"delegations": copy.deepcopy(entries)}
        checkpoint["channel_versions"] = {"delegations": "00000000000000000000000000000001.0.00000000000000000000000000000000"}
        self.item = CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": THREAD_ID,
                    "checkpoint_ns": "",
                    "checkpoint_id": checkpoint["id"],
                }
            },
            checkpoint=checkpoint,
            metadata={"source": "loop", "step": 4},
        )
        self.writes: list[tuple] = []

    async def aget_tuple_cancel_settlement(self, config):
        assert config["configurable"]["thread_id"] == THREAD_ID
        return self.item

    def get_next_version(self, current, channel):
        assert channel == "delegations"
        assert current == self.item.checkpoint["channel_versions"]["delegations"]
        return "00000000000000000000000000000002.0.00000000000000000000000000000000"

    async def aput_cancel_settlement(self, *args):
        self.writes.append(args)
        return {"configurable": {"thread_id": THREAD_ID, "checkpoint_ns": "", "checkpoint_id": args[1]["id"]}}


@pytest.mark.anyio
async def test_cancel_settlement_persists_current_run_ledger_without_overwriting_terminal() -> None:
    active = _entry("call-a", occurrence=2)
    terminal = _entry("call-b", status="failed")
    other_run = _entry("call-c", run_id="run-other")
    saver = _RecordingCancelSaver([active, terminal, other_run])

    changed = await settle_run_delegation_ledger_cancelled(
        saver,
        thread_id=THREAD_ID,
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_ID,
        run_id=RUN_ID,
    )

    assert changed is True
    assert len(saver.writes) == 1
    config, checkpoint, metadata, new_versions = saver.writes[0]
    assert config["configurable"]["checkpoint_id"] == saver.item.checkpoint["id"]
    assert checkpoint["channel_values"]["delegations"] == [
        {**active, "status": "cancelled"},
        terminal,
        other_run,
    ]
    assert checkpoint["channel_versions"]["delegations"] == new_versions["delegations"]
    assert metadata["source"] == "update"
    assert metadata["step"] == 5

    saver.item = CheckpointTuple(
        config=saver.item.config,
        checkpoint=checkpoint,
        metadata=metadata,
    )
    assert (
        await settle_run_delegation_ledger_cancelled(
            saver,
            thread_id=THREAD_ID,
            project_id=str(PROJECT_ID),
            owner_user_id=OWNER_ID,
            run_id=RUN_ID,
        )
        is False
    )
    assert len(saver.writes) == 1


class _Authority:
    def __init__(self, *, cancelled: bool) -> None:
        self.cancel_requested = cancelled
        self.expected_worker_id = uuid.UUID("00000000-0000-0000-0000-000000000806")

    def bind_heartbeat_callback(self, callback) -> None:
        self.heartbeat_callback = callback


def _execution() -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(resource_scope=_scope()),
        runtime_kind="chat",
        run=SimpleNamespace(thread_id=THREAD_ID),
    )


def _handler(executor) -> PrivateRunJobHandler:
    return PrivateRunJobHandler(
        AsyncMock(),
        executor=executor,
    )


@pytest.mark.anyio
async def test_cancel_present_before_executor_settles_ledger_first() -> None:
    execution = _execution()
    handler = _handler(AsyncMock())
    handler._begin = AsyncMock(return_value=(execution, True, None, _scope()))  # type: ignore[method-assign]
    handler._settle_cancelled_delegations = AsyncMock()  # type: ignore[attr-defined,method-assign]
    authority = _Authority(cancelled=False)

    settlement = await handler._handle_with_trace(_claim(), authority)  # type: ignore[arg-type]

    assert settlement.outcome.status == "cancelled"
    handler._settle_cancelled_delegations.assert_awaited_once_with(  # type: ignore[attr-defined]
        _claim(),
        authority,
        execution=execution,
        scope=_scope(),
    )
    handler._executor.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_inflight_stop_settles_ledger_before_cancel_outcome() -> None:
    execution = _execution()
    authority = _Authority(cancelled=False)

    async def execute(_execution, active_authority):
        active_authority.cancel_requested = True
        return AgentExecutionResult.succeeded()

    handler = _handler(SimpleNamespace(execute=AsyncMock(side_effect=execute)))
    handler._begin = AsyncMock(return_value=(execution, False, None, _scope()))  # type: ignore[method-assign]
    handler._recover_sealed_suspension_after_execution = AsyncMock(return_value=None)  # type: ignore[method-assign]
    handler._settle_cancelled_delegations = AsyncMock()  # type: ignore[attr-defined,method-assign]

    settlement = await handler._handle_with_trace(_claim(), authority)  # type: ignore[arg-type]

    assert settlement.outcome.status == "cancelled"
    handler._settle_cancelled_delegations.assert_awaited_once_with(  # type: ignore[attr-defined]
        _claim(),
        authority,
        execution=execution,
        scope=_scope(),
    )


@pytest.mark.anyio
async def test_recovered_cancel_terminal_settles_ledger_without_graph_replay() -> None:
    executor = AsyncMock()
    handler = _handler(executor)
    handler._begin = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            None,
            True,
            RecoveredPrivateRunTerminal(AgentExecutionResult.cancelled()),
            _scope(),
        )
    )
    handler._settle_cancelled_delegations = AsyncMock()  # type: ignore[attr-defined,method-assign]
    authority = _Authority(cancelled=True)

    settlement = await handler._handle_with_trace(_claim(), authority)  # type: ignore[arg-type]

    assert settlement.outcome.status == "cancelled"
    handler._settle_cancelled_delegations.assert_awaited_once_with(  # type: ignore[attr-defined]
        _claim(),
        authority,
        execution=None,
        scope=_scope(),
    )
    executor.execute.assert_not_awaited()
