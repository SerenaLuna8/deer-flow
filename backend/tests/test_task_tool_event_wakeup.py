"""Subagent lifecycle wake-up and clean-process wire acceptance."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from deerflow.subagents.change_signal import SubagentChangeSignal, wait_for_change

_SUBSECOND = 1.0
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_real_lifecycle_returns_a_full_tool_message_in_under_a_second() -> None:
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
    assert result["usage_receipt_is_internal"] is True


@pytest.mark.asyncio
async def test_terminal_notify_from_a_thread_wakes_the_waiter_immediately() -> None:
    signal = SubagentChangeSignal()
    event = signal.subscribe()

    threading.Thread(
        target=lambda: (time.sleep(0.2), signal.notify(terminal=True)),
        daemon=True,
    ).start()

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
    signal.notify()
    await asyncio.sleep(0)
    assert not event.is_set()

    signal.notify(terminal=True)
    await asyncio.sleep(0)
    assert event.is_set()


@pytest.mark.asyncio
async def test_notification_before_subscribe_does_not_debounce_first_wakeup() -> None:
    signal = SubagentChangeSignal(debounce_seconds=60.0)
    signal.notify()
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

        async def subscribe() -> asyncio.Event:
            return signal.subscribe()

        loop.run_until_complete(subscribe())
    finally:
        loop.close()

    signal.notify(terminal=True)
