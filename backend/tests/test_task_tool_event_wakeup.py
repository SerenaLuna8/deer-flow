"""U8: subagent event-driven waiting — timing and heartbeat equivalence.

The ``task`` tool used to poll ``_background_tasks`` every 5 seconds, so a
200ms subtask still paid up to 5 seconds of tail latency. These tests pin the
new contract:

- terminal transitions wake the waiter in well under one heartbeat;
- the 5-second heartbeat survives as an upper bound, not a floor;
- non-terminal notifications are debounced (no event storms);
- the wait budget is a monotonic deadline, not a poll counter;
- result objects without a change signal degrade to heartbeat polling.

``conftest.py`` replaces ``deerflow.subagents.executor`` with a mock to break
a production import cycle. Most timing cases therefore isolate the signal and
wait helpers with small result doubles; one clean-process acceptance probe also
drives the production ``SubagentResult`` through the complete ``task`` tool and
asserts its final ``ToolMessage`` plus exact progress-event volume.
"""

import asyncio
import importlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from deerflow.subagents.change_signal import SubagentChangeSignal, wait_for_change

# `deerflow.tools.builtins` rebinds the attribute `task_tool` to the
# StructuredTool instance, so a plain `import ... as` would grab the tool
# object; resolve the module itself instead.
task_tool = importlib.import_module("deerflow.tools.builtins.task_tool")

# Wall-clock ceiling used to prove "no 5-second sleep happened".
_SUBSECOND = 1.0
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _FakeResult:
    """Duck-typed stand-in for SubagentResult (the real one is mocked out)."""

    completed_at: datetime | None = None
    status: object = field(default_factory=object)
    changes: SubagentChangeSignal = field(default_factory=SubagentChangeSignal)

    def finish_after(self, delay_seconds: float) -> threading.Thread:
        """Flip to terminal from a foreign thread after ``delay_seconds``."""

        def _finish() -> None:
            time.sleep(delay_seconds)
            self.completed_at = datetime.now()
            self.changes.notify(terminal=True)

        thread = threading.Thread(target=_finish, daemon=True)
        thread.start()
        return thread


class _FakeStatus:
    """Stands in for the SubagentStatus enum that conftest mocks away."""

    COMPLETED = object()
    FAILED = object()
    CANCELLED = object()
    TIMED_OUT = object()


def _install_task(monkeypatch: pytest.MonkeyPatch, result: object | None) -> None:
    monkeypatch.setattr(task_tool, "SubagentStatus", _FakeStatus)
    monkeypatch.setattr(task_tool, "get_background_task_result", lambda task_id: result)


def test_real_subagent_result_returns_a_full_tool_message_in_under_a_second() -> None:
    """Run the production result holder and full tool path outside conftest's mock."""
    completed = subprocess.run(
        [sys.executable, "tests/support/task_tool_event_probe.py"],
        cwd=_BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["elapsed"] < _SUBSECOND
    assert result["event_types"] == [
        "task_started",
        "task_running",
        "task_running",
        "task_completed",
    ]
    assert result["tool_status"] == "completed"


# ---------------------------------------------------------------------------
# SubagentChangeSignal primitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_notify_from_a_thread_wakes_the_waiter_immediately() -> None:
    signal = SubagentChangeSignal()
    event = signal.subscribe()

    threading.Thread(target=lambda: (time.sleep(0.2), signal.notify(terminal=True)), daemon=True).start()

    started = time.monotonic()
    woken = await wait_for_change(event, heartbeat_seconds=5.0)
    elapsed = time.monotonic() - started

    assert woken is True
    assert elapsed < _SUBSECOND


@pytest.mark.asyncio
async def test_subscribing_after_the_terminal_transition_returns_a_set_event() -> None:
    signal = SubagentChangeSignal()
    signal.notify(terminal=True)

    event = signal.subscribe()

    assert event.is_set()
    assert await wait_for_change(event, heartbeat_seconds=0.05) is True


@pytest.mark.asyncio
async def test_non_terminal_notifications_are_debounced() -> None:
    signal = SubagentChangeSignal(debounce_seconds=60.0)
    event = signal.subscribe()

    signal.notify()
    await asyncio.sleep(0)
    assert event.is_set()

    event.clear()
    signal.notify()  # inside the debounce window: dropped
    await asyncio.sleep(0)
    assert not event.is_set()

    signal.notify(terminal=True)  # terminal bypasses the debounce window
    await asyncio.sleep(0)
    assert event.is_set()


@pytest.mark.asyncio
async def test_notification_before_subscribe_does_not_debounce_the_first_real_wakeup() -> None:
    """mark_running commonly fires before task_tool installs its waiter."""

    signal = SubagentChangeSignal(debounce_seconds=60.0)
    signal.notify()  # no waiter yet: there was no event storm to suppress
    event = signal.subscribe()

    signal.notify()
    await asyncio.sleep(0)

    assert event.is_set()


@pytest.mark.asyncio
async def test_heartbeat_timeout_returns_false_without_a_change() -> None:
    signal = SubagentChangeSignal()
    event = signal.subscribe()

    assert await wait_for_change(event, heartbeat_seconds=0.05) is False


@pytest.mark.asyncio
async def test_unsubscribed_waiters_are_not_woken() -> None:
    signal = SubagentChangeSignal()
    event = signal.subscribe()
    signal.unsubscribe(event)

    signal.notify(terminal=True)
    await asyncio.sleep(0)

    assert not event.is_set()


def test_notify_survives_a_waiter_whose_loop_already_closed() -> None:
    signal = SubagentChangeSignal()
    loop = asyncio.new_event_loop()
    try:

        async def _subscribe() -> asyncio.Event:
            return signal.subscribe()

        loop.run_until_complete(_subscribe())
    finally:
        loop.close()

    # Must not raise even though call_soon_threadsafe now fails.
    signal.notify(terminal=True)


# ---------------------------------------------------------------------------
# task_tool waiting helpers (deadline + event, heartbeat fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_200ms_subtask_returns_in_under_a_second(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance: 200ms completion must not pay the 5s polling tail."""
    result = _FakeResult()
    _install_task(monkeypatch, result)
    result.finish_after(0.2)

    started = time.monotonic()
    terminal = await task_tool._await_subagent_terminal("t-fast", wait_budget_seconds=30.0)
    elapsed = time.monotonic() - started

    assert terminal is result
    assert elapsed < _SUBSECOND


@pytest.mark.asyncio
async def test_the_wait_budget_is_a_deadline_not_a_poll_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _FakeResult()  # never turns terminal
    _install_task(monkeypatch, result)

    started = time.monotonic()
    terminal = await task_tool._await_subagent_terminal("t-stuck", wait_budget_seconds=0.3)
    elapsed = time.monotonic() - started

    assert terminal is None
    assert 0.2 <= elapsed < _SUBSECOND


@pytest.mark.asyncio
async def test_waiting_degrades_to_heartbeat_polling_without_a_change_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    class _LegacyResult:
        completed_at = datetime.now()
        status = object()

    _install_task(monkeypatch, _LegacyResult())

    terminal = await task_tool._await_subagent_terminal("t-legacy", wait_budget_seconds=1.0)

    assert terminal is not None


@pytest.mark.asyncio
async def test_a_vanished_task_returns_none_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_task(monkeypatch, None)

    started = time.monotonic()
    terminal = await task_tool._await_subagent_terminal("t-gone", wait_budget_seconds=30.0)
    elapsed = time.monotonic() - started

    assert terminal is None
    assert elapsed < _SUBSECOND


@pytest.mark.asyncio
async def test_deferred_cleanup_removes_the_task_once_it_turns_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _FakeResult()
    _install_task(monkeypatch, result)
    removed: list[str] = []
    monkeypatch.setattr(task_tool, "cleanup_background_task", removed.append)
    result.finish_after(0.1)

    started = time.monotonic()
    await task_tool._deferred_cleanup_subagent_task("t-cleanup", "trace", wait_budget_seconds=30.0)
    elapsed = time.monotonic() - started

    assert removed == ["t-cleanup"]
    assert elapsed < _SUBSECOND


@pytest.mark.asyncio
async def test_deferred_cleanup_leaves_a_stuck_task_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _FakeResult()  # never terminal
    _install_task(monkeypatch, result)
    removed: list[str] = []
    monkeypatch.setattr(task_tool, "cleanup_background_task", removed.append)

    await task_tool._deferred_cleanup_subagent_task("t-wedged", "trace", wait_budget_seconds=0.2)

    assert removed == []


@pytest.mark.asyncio
async def test_the_waiter_unsubscribes_after_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper must not leak waiter registrations on the shared signal."""
    result = _FakeResult()
    _install_task(monkeypatch, result)
    result.finish_after(0.05)

    await task_tool._await_subagent_terminal("t-leak", wait_budget_seconds=30.0)

    assert result.changes._waiters == []
