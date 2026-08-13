"""Scheduler transaction and lane isolation contracts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.scheduler.app import SchedulerApp
from app.system_runtime_settings import AutomationsPolicyValue


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, identity: int) -> None:
        self.identity = identity

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session(len(self.sessions))
        self.sessions.append(session)
        return session


class _Ownership:
    is_lost = False

    def __init__(self) -> None:
        self.verify_calls = 0

    @asynccontextmanager
    async def hold(self):
        yield

    async def verify(self) -> None:
        self.verify_calls += 1


class _Automation:
    def __init__(self) -> None:
        self.reconcile_sessions: list[int] = []
        self.admit_sessions: list[int] = []
        self.admitted = asyncio.Event()

    async def reconcile_admitted_runs(self, session) -> None:
        self.reconcile_sessions.append(session.identity)

    async def admit_due_occurrences(self, session, *, now) -> None:
        assert now.tzinfo is not None
        self.admit_sessions.append(session.identity)


class _Dream:
    def __init__(self) -> None:
        self.calls = 0
        self.admitted = asyncio.Event()

    async def admit_due(self, *, now) -> None:
        assert now.tzinfo is not None
        self.calls += 1
        self.admitted.set()


class _DisabledAutomationsPolicy:
    async def read_current(self, session):
        del session
        return AutomationsPolicyValue(enabled=False, poll_interval_seconds=1)


@pytest.mark.asyncio
async def test_scheduler_keeps_automation_and_dream_in_separate_transactions() -> None:
    stop_event = asyncio.Event()
    factory = _SessionFactory()
    automation = _Automation()
    dream = _Dream()
    ownership = _Ownership()
    app = SchedulerApp(
        enabled=True,
        ownership=ownership,
        service=automation,
        session_factory=factory,
        poll_interval_seconds=0.01,
        dream_service=dream,
    )

    task = asyncio.create_task(app.run(stop_event))
    await asyncio.wait_for(dream.admitted.wait(), timeout=1)
    stop_event.set()
    await task

    assert automation.reconcile_sessions == [0]
    assert automation.admit_sessions == [1]
    assert dream.calls == 1
    assert len(factory.sessions) == 2
    assert ownership.verify_calls == 1


@pytest.mark.asyncio
async def test_scheduler_skips_automation_admission_when_policy_disables_polling() -> None:
    stop_event = asyncio.Event()
    factory = _SessionFactory()
    automation = _Automation()
    dream = _Dream()
    ownership = _Ownership()
    app = SchedulerApp(
        enabled=True,
        ownership=ownership,
        service=automation,
        session_factory=factory,
        poll_interval_seconds=0.01,
        dream_service=dream,
        policy_reader=_DisabledAutomationsPolicy(),
    )

    task = asyncio.create_task(app.run(stop_event))
    await asyncio.wait_for(dream.admitted.wait(), timeout=1)
    stop_event.set()
    await task

    assert automation.reconcile_sessions == [0]
    assert automation.admit_sessions == []
    assert dream.calls == 1
    assert ownership.verify_calls == 1
