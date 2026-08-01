from __future__ import annotations

import ast
import asyncio
import re
import uuid
from collections import deque
from pathlib import Path

import pytest

from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
    WorkerService,
)
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import JobClaim, JobHeartbeat, JobScope
from deerflow.trace_context import get_current_trace_id


class _Transaction:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    async def __aenter__(self):
        self.backend.transaction_active = True
        return self

    async def __aexit__(self, *_args):
        self.backend.transaction_active = False
        return False


class _Session:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction(self.backend)


class _Factory:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    def __call__(self) -> _Session:
        return _Session(self.backend)


class _FakeBackend:
    def __init__(self, job_count: int) -> None:
        self.claims = deque(_claim(index) for index in range(job_count))
        self.marked_running: list[uuid.UUID] = []
        self.succeeded: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str]] = []
        self.heartbeats = 0
        self.heartbeat_result: JobHeartbeat | bool = JobHeartbeat(cancel_requested=False)
        self.heartbeat_error: Exception | None = None
        self.claim_calls = 0
        self.transaction_active = False
        self.claim_gate: asyncio.Event | None = None
        self.claim_started: asyncio.Event | None = None


class _FakeRepository:
    def __init__(self, session: _Session) -> None:
        self.backend = session.backend

    async def claim_next(self, **_kwargs):
        self.backend.claim_calls += 1
        if self.backend.claim_started is not None:
            self.backend.claim_started.set()
        if self.backend.claim_gate is not None:
            await self.backend.claim_gate.wait()
        return self.backend.claims.popleft() if self.backend.claims else None

    async def mark_running(self, job_id, **_kwargs):
        self.backend.marked_running.append(job_id)
        return True

    async def heartbeat(self, _job_id, **_kwargs):
        self.backend.heartbeats += 1
        if self.backend.heartbeat_error is not None:
            raise self.backend.heartbeat_error
        return self.backend.heartbeat_result

    async def settle_success(self, job_id, **_kwargs):
        self.backend.succeeded.append(job_id)
        return True

    async def settle_cancelled(self, job_id, **_kwargs):
        self.backend.cancelled.append(job_id)
        return True

    async def retry_or_dead(self, job_id, *, public_error_code, **_kwargs):
        self.backend.failed.append((job_id, public_error_code))
        return True


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.heartbeat_result = True
        self.heartbeat_called: asyncio.Event | None = None
        self.heartbeat_gate: asyncio.Event | None = None
        self.heartbeat_cancel_suppressed: asyncio.Event | None = None
        self.mark_draining_error: Exception | None = None

    async def register(self, *_args, **_kwargs) -> None:
        self.calls.append("register")

    async def heartbeat(self, *_args, **_kwargs) -> bool:
        self.calls.append("heartbeat")
        if self.heartbeat_called is not None:
            self.heartbeat_called.set()
        if self.heartbeat_gate is not None:
            try:
                await self.heartbeat_gate.wait()
            except asyncio.CancelledError:
                if self.heartbeat_cancel_suppressed is not None:
                    self.heartbeat_cancel_suppressed.set()
                await self.heartbeat_gate.wait()
        return self.heartbeat_result

    async def mark_draining(self, *_args, **_kwargs) -> bool:
        self.calls.append("mark_draining")
        if self.mark_draining_error is not None:
            raise self.mark_draining_error
        return True

    async def remove(self, *_args, **_kwargs) -> bool:
        self.calls.append("remove")
        return True


def _claim(index: int) -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token=f"lease-{index}",
        job_type="retention_purge",
        scope=JobScope(uuid.uuid4(), None),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )


def _config(**updates) -> WorkerConfig:
    return WorkerConfig().model_copy(update=updates)


@pytest.mark.asyncio
async def test_after_claim_commit_hook_runs_outside_claim_transaction() -> None:
    backend = _FakeBackend(job_count=0)
    observations: list[tuple[bool, int]] = []

    async def after_claim_commit() -> None:
        observations.append((backend.transaction_active, backend.claim_calls))

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {},
        _config(),
        repository_builder=_FakeRepository,
        after_claim_commit=after_claim_commit,
    )

    assert await service._claim_next() is None
    assert observations == [(False, 1)]


@pytest.mark.parametrize(
    "unsafe_code",
    [
        "private provider detail",
        "provider@example.com",
        "TOKEN_abcd1234",
        "LINE_ONE\nLINE_TWO",
        "lowercase_code",
    ],
)
def test_job_outcome_rejects_non_public_error_text(unsafe_code: str) -> None:
    with pytest.raises(ValueError, match="public error code"):
        JobOutcome.failed(unsafe_code)
    assert JobOutcome.failed("WORKER_HANDLER_FAILED").public_error_code == "WORKER_HANDLER_FAILED"


@pytest.mark.asyncio
async def test_worker_service_never_exceeds_configured_concurrency() -> None:
    backend = _FakeBackend(job_count=6)
    registry = _FakeRegistry()
    active = 0
    peak = 0

    async def handler(_claim, _authority):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(max_concurrent_jobs=2, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert peak == 2
    assert len(backend.succeeded) == 6
    assert registry.calls[0] == "register"
    assert registry.calls[-2:] == ["mark_draining", "remove"]


@pytest.mark.asyncio
async def test_worker_service_heartbeats_active_job_and_honors_late_cancel() -> None:
    backend = _FakeBackend(job_count=1)
    backend.heartbeat_result = JobHeartbeat(cancel_requested=True)
    cancel_seen = asyncio.Event()

    async def handler(_claim, authority: JobLeaseAuthority):
        while not authority.cancel_requested:
            await asyncio.sleep(0)
        cancel_seen.set()
        return JobOutcome.cancelled()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert cancel_seen.is_set()
    assert backend.heartbeats >= 1
    assert len(backend.cancelled) == 1


@pytest.mark.asyncio
async def test_private_claim_trace_wraps_mark_running_heartbeat_handler_and_settlement() -> None:
    backend = _FakeBackend(job_count=0)
    trace_id = "worker-service-private-trace"
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="private-trace-lease",
        job_type="private_run",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=str(uuid.uuid4()),
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=trace_id,
    )
    observations: list[tuple[str, str | None]] = []

    class ObservingRepository(_FakeRepository):
        async def mark_running(self, job_id, **kwargs):
            observations.append(("mark_running", get_current_trace_id()))
            return await super().mark_running(job_id, **kwargs)

        async def heartbeat(self, job_id, **kwargs):
            observations.append(("heartbeat", get_current_trace_id()))
            return await super().heartbeat(job_id, **kwargs)

        async def settle_success(self, job_id, **kwargs):
            observations.append(("settle_success", get_current_trace_id()))
            return await super().settle_success(job_id, **kwargs)

    async def handler(_claim, _authority):
        observations.append(("handler", get_current_trace_id()))
        while backend.heartbeats == 0:
            await asyncio.sleep(0)
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"private_run": handler},
        _config(heartbeat_seconds=0.001),
        repository_builder=ObservingRepository,
    )

    assert get_current_trace_id() is None
    await service._execute_claim(claim)
    assert get_current_trace_id() is None
    assert {name for name, _trace in observations} == {
        "mark_running",
        "heartbeat",
        "handler",
        "settle_success",
    }
    assert {trace for _name, trace in observations} == {trace_id}


@pytest.mark.asyncio
async def test_private_claim_with_invalid_trace_fails_before_mark_running() -> None:
    backend = _FakeBackend(job_count=0)
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="missing-trace-lease",
        job_type="private_run",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=str(uuid.uuid4()),
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=None,
    )
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"private_run": lambda _claim, _authority: JobOutcome.succeeded()},
        _config(),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(LeaseLost):
        await service._execute_claim(claim)

    assert backend.marked_running == []


@pytest.mark.asyncio
async def test_retention_claim_without_trace_keeps_existing_worker_path() -> None:
    backend = _FakeBackend(job_count=0)
    observations: list[str | None] = []

    async def handler(_claim, _authority):
        observations.append(get_current_trace_id())
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(),
        repository_builder=_FakeRepository,
    )
    await service._execute_claim(_claim(1))

    assert observations == [None]
    assert len(backend.succeeded) == 1


@pytest.mark.asyncio
async def test_late_cancel_invokes_bound_cooperative_stop_callback() -> None:
    backend = _FakeBackend(job_count=0)
    backend.heartbeat_result = JobHeartbeat(cancel_requested=True)
    authority = JobLeaseAuthority(
        _Factory(backend),
        _claim(1),
        lease_seconds=90,
        repository_builder=_FakeRepository,
    )
    cooperative_stop = asyncio.Event()
    authority.bind_cancel_callback(cooperative_stop.set)

    await authority.heartbeat()

    assert cooperative_stop.is_set()


@pytest.mark.asyncio
async def test_handler_owned_settlement_runs_after_job_heartbeat_stops() -> None:
    backend = _FakeBackend(job_count=1)

    async def handler(_claim, _authority):
        while backend.heartbeats == 0:
            await asyncio.sleep(0)

        async def commit() -> None:
            stopped_at = backend.heartbeats
            await asyncio.sleep(0.005)
            assert backend.heartbeats == stopped_at
            backend.succeeded.append(_claim.job_id)

        return JobSettlement(JobOutcome.succeeded(), commit)

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(
            heartbeat_seconds=0.001,
            poll_interval_seconds=0.001,
        ),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert len(backend.succeeded) == 1


@pytest.mark.asyncio
async def test_handler_exception_settles_with_public_code_only() -> None:
    backend = _FakeBackend(job_count=1)

    async def handler(_claim, _authority):
        raise RuntimeError("private provider detail")

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert len(backend.failed) == 1
    assert backend.failed[0][1] == "WORKER_HANDLER_FAILED"
    assert "private provider detail" not in repr(backend.failed)


@pytest.mark.asyncio
async def test_drain_timeout_cancels_local_task_without_forging_terminal_state() -> None:
    backend = _FakeBackend(job_count=1)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_claim, _authority):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    registry = _FakeRegistry()
    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(
            heartbeat_seconds=0.01,
            poll_interval_seconds=0.001,
            shutdown_grace_seconds=0.01,
        ),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(running, timeout=1)

    assert cancelled.is_set()
    assert backend.succeeded == []
    assert backend.cancelled == []
    assert backend.failed == []
    assert registry.calls[-2:] == ["mark_draining", "remove"]


@pytest.mark.asyncio
async def test_drain_grace_is_bounded_when_handler_suppresses_first_cancel() -> None:
    backend = _FakeBackend(job_count=1)
    started = asyncio.Event()
    cancel_suppressed = asyncio.Event()
    release = asyncio.Event()

    async def handler(_claim, _authority):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_suppressed.set()
            await release.wait()
            return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001, shutdown_grace_seconds=0.01),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    done, _pending = await asyncio.wait({running}, timeout=0.1)
    try:
        assert running in done
        assert cancel_suppressed.is_set()
        assert backend.succeeded == []
        assert backend.cancelled == []
        assert backend.failed == []
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=1)


@pytest.mark.asyncio
async def test_detached_handler_cannot_extend_lease_after_worker_shutdown() -> None:
    backend = _FakeBackend(job_count=1)
    started = asyncio.Event()
    cancel_suppressed = asyncio.Event()
    retry_heartbeat = asyncio.Event()
    heartbeat_rejected = asyncio.Event()
    heartbeat_succeeded = asyncio.Event()

    async def handler(_claim, authority: JobLeaseAuthority):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_suppressed.set()
            await retry_heartbeat.wait()
            try:
                await authority.heartbeat()
            except LeaseLost:
                heartbeat_rejected.set()
            else:
                heartbeat_succeeded.set()
            return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001, shutdown_grace_seconds=0.01),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(running, timeout=1)
    assert cancel_suppressed.is_set()
    heartbeat_count = backend.heartbeats

    retry_heartbeat.set()
    await asyncio.wait_for(heartbeat_rejected.wait(), timeout=1)
    assert heartbeat_rejected.is_set()
    assert not heartbeat_succeeded.is_set()
    assert backend.heartbeats == heartbeat_count
    assert backend.succeeded == []


@pytest.mark.asyncio
async def test_registry_drain_failure_still_cancels_local_handler_and_removes_node() -> None:
    backend = _FakeBackend(job_count=1)
    started = asyncio.Event()
    handler_cancelled = asyncio.Event()
    handler_task: asyncio.Task | None = None

    async def handler(_claim, _authority):
        nonlocal handler_task
        handler_task = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()

    registry = _FakeRegistry()
    registry.mark_draining_error = RuntimeError("registry unavailable")
    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001, shutdown_grace_seconds=0.01),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    try:
        with pytest.raises(RuntimeError, match="registry unavailable"):
            await asyncio.wait_for(running, timeout=1)
        assert handler_cancelled.is_set()
        assert registry.calls[-1] == "remove"
    finally:
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            await asyncio.gather(handler_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_graceful_drain_allows_inflight_job_to_settle() -> None:
    backend = _FakeBackend(job_count=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_claim, _authority):
        started.set()
        await release.wait()
        return JobOutcome.succeeded()

    registry = _FakeRegistry()
    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001, shutdown_grace_seconds=1),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    release.set()
    await asyncio.wait_for(running, timeout=1)

    assert len(backend.succeeded) == 1
    assert registry.calls[-2:] == ["mark_draining", "remove"]


@pytest.mark.asyncio
async def test_shutdown_flushes_initialized_project_memory_after_inflight_and_before_registry_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents.memory import queue as memory_queue_module

    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    inflight_finished = asyncio.Event()

    async def finish_inflight() -> None:
        await asyncio.sleep(0)
        registry.calls.append("inflight_finished")
        inflight_finished.set()

    class MemoryQueue:
        async def flush_all(self) -> list[bool]:
            assert inflight_finished.is_set()
            registry.calls.append("memory_flush")
            return [True]

    monkeypatch.setattr(memory_queue_module, "_project_memory_queue", MemoryQueue())
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(shutdown_grace_seconds=1),
        repository_builder=_FakeRepository,
    )
    service._inflight.add(asyncio.create_task(finish_inflight()))

    await service._shutdown()

    assert registry.calls == [
        "mark_draining",
        "inflight_finished",
        "memory_flush",
        "remove",
    ]


@pytest.mark.asyncio
async def test_shutdown_bounds_project_memory_flush_that_suppresses_cancel(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deerflow.agents.memory import queue as memory_queue_module

    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    started = asyncio.Event()
    cancel_suppressed = asyncio.Event()
    release = asyncio.Event()

    class MemoryQueue:
        async def flush_all(self) -> list[bool]:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_suppressed.set()
                await release.wait()
            return [False]

    monkeypatch.setattr(memory_queue_module, "_project_memory_queue", MemoryQueue())
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(shutdown_grace_seconds=0.01),
        repository_builder=_FakeRepository,
    )

    try:
        with caplog.at_level("WARNING", logger="app.worker.service"):
            await asyncio.wait_for(service._shutdown(), timeout=0.2)
        assert started.is_set()
        assert cancel_suppressed.is_set()
        assert registry.calls[-1] == "remove"
        assert "project memory queue shutdown flush timed out" in caplog.text.lower()
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_shutdown_observes_project_memory_flush_failure_and_still_removes_registry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deerflow.agents.memory import queue as memory_queue_module

    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()

    class MemoryQueue:
        async def flush_all(self) -> list[bool]:
            registry.calls.append("memory_flush")
            raise RuntimeError("private memory backend detail")

    monkeypatch.setattr(memory_queue_module, "_project_memory_queue", MemoryQueue())
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(shutdown_grace_seconds=1),
        repository_builder=_FakeRepository,
    )

    with caplog.at_level("WARNING", logger="app.worker.service"):
        await service._shutdown()

    assert registry.calls == ["mark_draining", "memory_flush", "remove"]
    assert "project memory queue shutdown flush failed: runtimeerror" in caplog.text.lower()
    assert "private memory backend detail" not in caplog.text


@pytest.mark.asyncio
async def test_lease_authority_raises_when_current_token_loses_ownership() -> None:
    backend = _FakeBackend(job_count=0)
    backend.heartbeat_result = False
    authority = JobLeaseAuthority(
        _Factory(backend),
        _claim(1),
        lease_seconds=90,
        repository_builder=_FakeRepository,
    )
    with pytest.raises(LeaseLost):
        await authority.heartbeat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("heartbeat_result", "heartbeat_error"),
    [
        (False, None),
        (JobHeartbeat(cancel_requested=False), RuntimeError("database unavailable")),
    ],
)
async def test_handler_observed_authority_loss_never_settles_job(
    heartbeat_result,
    heartbeat_error,
) -> None:
    backend = _FakeBackend(job_count=1)
    backend.heartbeat_result = heartbeat_result
    backend.heartbeat_error = heartbeat_error

    async def handler(_claim, authority: JobLeaseAuthority):
        await authority.heartbeat()
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert backend.succeeded == []
    assert backend.cancelled == []
    assert backend.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("heartbeat_result", "heartbeat_error"),
    [
        (False, None),
        (JobHeartbeat(cancel_requested=False), RuntimeError("database unavailable")),
    ],
)
async def test_heartbeat_authority_failure_cancels_handler_without_settlement(
    heartbeat_result,
    heartbeat_error,
) -> None:
    backend = _FakeBackend(job_count=1)
    backend.heartbeat_result = heartbeat_result
    backend.heartbeat_error = heartbeat_error
    handler_cancelled = asyncio.Event()

    async def handler(_claim, _authority):
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert handler_cancelled.is_set()
    assert backend.succeeded == []
    assert backend.cancelled == []
    assert backend.failed == []


@pytest.mark.asyncio
async def test_uncooperative_handler_after_lease_loss_fail_stops_worker_capacity() -> None:
    backend = _FakeBackend(job_count=2)
    backend.heartbeat_result = False
    first_started = asyncio.Event()
    first_cancel_suppressed = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    calls = 0

    async def handler(_claim, _authority):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancel_suppressed.set()
                await release_first.wait()
                return JobOutcome.succeeded()
        second_started.set()
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(
            heartbeat_seconds=0.001,
            max_concurrent_jobs=1,
            poll_interval_seconds=0.001,
        ),
        repository_builder=_FakeRepository,
    )
    running = asyncio.create_task(service.run_until_idle())
    await asyncio.wait_for(first_started.wait(), timeout=1)
    try:
        with pytest.raises(RuntimeError, match="did not stop after lease loss"):
            await asyncio.wait_for(running, timeout=1)
        assert first_cancel_suppressed.is_set()
        assert not second_started.is_set()
        assert backend.succeeded == []
        assert backend.cancelled == []
        assert backend.failed == []
    finally:
        release_first.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_during_claim_does_not_start_claimed_handler() -> None:
    backend = _FakeBackend(job_count=1)
    backend.claim_gate = asyncio.Event()
    backend.claim_started = asyncio.Event()
    handler_started = asyncio.Event()

    async def handler(_claim, _authority):
        handler_started.set()
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(backend.claim_started.wait(), timeout=1)
    stop_event.set()
    backend.claim_gate.set()
    await asyncio.wait_for(running, timeout=1)

    assert not handler_started.is_set()
    assert backend.marked_running == []


@pytest.mark.asyncio
async def test_fleet_registration_loss_stops_claiming_and_drains() -> None:
    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    registry.heartbeat_result = False
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(RuntimeError, match="registry ownership was lost"):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert registry.calls[-2:] == ["mark_draining", "remove"]


@pytest.mark.asyncio
async def test_fleet_loss_during_claim_never_starts_claimed_handler() -> None:
    backend = _FakeBackend(job_count=1)
    backend.claim_gate = asyncio.Event()
    backend.claim_started = asyncio.Event()
    registry = _FakeRegistry()
    registry.heartbeat_result = False
    registry.heartbeat_called = asyncio.Event()
    handler_started = asyncio.Event()

    async def handler(_claim, _authority):
        handler_started.set()
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    running = asyncio.create_task(service.run(asyncio.Event()))
    await asyncio.wait_for(backend.claim_started.wait(), timeout=1)
    await asyncio.wait_for(registry.heartbeat_called.wait(), timeout=1)
    backend.claim_gate.set()

    with pytest.raises(RuntimeError, match="registry ownership was lost"):
        await asyncio.wait_for(running, timeout=1)
    assert not handler_started.is_set()
    assert backend.marked_running == []


@pytest.mark.asyncio
async def test_stop_does_not_wait_unbounded_for_uncooperative_fleet_heartbeat() -> None:
    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    registry.heartbeat_called = asyncio.Event()
    registry.heartbeat_gate = asyncio.Event()
    registry.heartbeat_cancel_suppressed = asyncio.Event()
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))
    await asyncio.wait_for(registry.heartbeat_called.wait(), timeout=1)
    stop_event.set()
    done, _pending = await asyncio.wait({running}, timeout=0.1)
    try:
        assert running in done
        assert registry.heartbeat_cancel_suppressed.is_set()
    finally:
        registry.heartbeat_gate.set()
        await asyncio.wait_for(running, timeout=1)


@pytest.mark.asyncio
async def test_job_task_names_use_truncated_hash_not_raw_job_id() -> None:
    backend = _FakeBackend(job_count=1)
    job_id = backend.claims[0].job_id
    observed_name = ""

    async def handler(_claim, _authority):
        nonlocal observed_name
        current = asyncio.current_task()
        assert current is not None
        observed_name = current.get_name()
        return JobOutcome.succeeded()

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )
    await service.run_until_idle()

    assert str(job_id) not in observed_name
    assert re.fullmatch(r"job-handler-[0-9a-f]{12}", observed_name)


def test_independent_worker_modules_do_not_import_gateway() -> None:
    worker_root = Path(__file__).resolve().parents[1] / "app" / "worker"
    offenders: list[str] = []
    for path in worker_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.gateway"):
                offenders.append(path.name)
            elif isinstance(node, ast.Import) and any(alias.name.startswith("app.gateway") for alias in node.names):
                offenders.append(path.name)
    assert offenders == []
