from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.scheduler.app import SchedulerApp


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

    async def reconcile_admitted_runs(self, session) -> None:
        self.reconcile_sessions.append(session.identity)

    async def admit_due_occurrences(self, session, *, now) -> None:
        assert now.tzinfo is not None
        self.admit_sessions.append(session.identity)


class _Memory:
    def __init__(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self.sessions: list[int] = []

    async def admit_due(self, session, *, now) -> None:
        assert now.tzinfo is not None
        self.sessions.append(session.identity)
        self.stop_event.set()
        raise RuntimeError("injected isolated Memory poll failure")


@pytest.mark.asyncio
async def test_scheduler_keeps_automation_and_memory_in_separate_transactions() -> None:
    stop_event = asyncio.Event()
    factory = _SessionFactory()
    automation = _Automation()
    memory = _Memory(stop_event)
    ownership = _Ownership()
    app = SchedulerApp(
        enabled=True,
        ownership=ownership,
        service=automation,
        session_factory=factory,
        poll_interval_seconds=0.01,
        memory_service=memory,
    )

    await app.run(stop_event)

    assert automation.reconcile_sessions == [0]
    assert automation.admit_sessions == [1]
    assert memory.sessions == [2]
    assert ownership.verify_calls == 1
