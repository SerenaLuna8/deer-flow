"""Core Worker service lifecycle tests."""

from __future__ import annotations

import ast
import asyncio
import re
import uuid
from collections import deque
from pathlib import Path

import pytest
from asyncpg.exceptions import CannotConnectNowError
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.reliability.workers import WorkerRegistry
from app.worker.service import (
    JobLeaseAuthority,
    JobOutcome,
    JobSettlement,
    LeaseLost,
    WorkerService,
)
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobHeartbeat,
    JobScope,
    JobUnstartedClaimRelease,
)
from deerflow.runtime.host_execution_domain import HostExecutionDomainSnapshot
from deerflow.trace_context import get_current_trace_id


class _Transaction:
    def __init__(self, backend: _FakeBackend) -> None:
        self.backend = backend

    async def __aenter__(self):
        self.backend.transaction_active = True
        return self

    async def __aexit__(self, *_args):
        self.backend.transaction_active = False
        if self.backend.transaction_exit_errors:
            raise self.backend.transaction_exit_errors.popleft()
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
        self.claim_kwargs: list[dict[str, object]] = []
        self.claim_errors: deque[BaseException] = deque()
        self.transaction_exit_errors: deque[BaseException] = deque()
        self.transaction_active = False
        self.released: list[tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]] = []
        self.claim_gate: asyncio.Event | None = None
        self.claim_started: asyncio.Event | None = None


class _FakeRepository:
    def __init__(self, session: _Session) -> None:
        self.backend = session.backend

    async def claim_next(self, **_kwargs):
        self.backend.claim_calls += 1
        self.backend.claim_kwargs.append(dict(_kwargs))
        if self.backend.claim_started is not None:
            self.backend.claim_started.set()
        if self.backend.claim_errors:
            raise self.backend.claim_errors.popleft()
        if self.backend.claim_gate is not None:
            await self.backend.claim_gate.wait()
        return self.backend.claims.popleft() if self.backend.claims else None

    async def mark_running(self, job_id, **_kwargs):
        self.backend.marked_running.append(job_id)
        return True

    async def release_unstarted_claim(
        self,
        job_id,
        *,
        lease_token,
        attempt_id,
        expected_worker_id,
        **_kwargs,
    ):
        self.backend.released.append(
            (job_id, lease_token, attempt_id, expected_worker_id),
        )
        return JobUnstartedClaimRelease(disposition="requeued")

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
        self.heartbeat_errors: deque[BaseException] = deque()
        self.heartbeat_called: asyncio.Event | None = None
        self.heartbeat_gate: asyncio.Event | None = None
        self.heartbeat_cancel_suppressed: asyncio.Event | None = None
        self.register_error: BaseException | None = None
        self.register_kwargs: list[dict[str, object]] = []
        self.mark_draining_error: Exception | None = None

    async def register(self, *_args, **_kwargs) -> None:
        self.calls.append("register")
        self.register_kwargs.append(dict(_kwargs))
        if self.register_error is not None:
            raise self.register_error

    async def heartbeat(self, *_args, **_kwargs) -> bool:
        self.calls.append("heartbeat")
        if self.heartbeat_called is not None:
            self.heartbeat_called.set()
        if self.heartbeat_errors:
            raise self.heartbeat_errors.popleft()
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


def _execution_domain() -> HostExecutionDomainSnapshot:
    return HostExecutionDomainSnapshot(
        configured_id="mac-primary",
        public_label="My Mac",
        os_name="posix",
        sys_platform="darwin",
        machine="arm64",
        device_fingerprint="d" * 64,
        environment_fingerprint="f" * 64,
        euid=501,
        egid=20,
        runtime_base_dir="/private/tmp/actweave",
    )


def test_worker_job_type_contract_accepts_dream_and_rejects_unknown() -> None:
    WorkerService(
        None,
        None,
        {"memory_dream": object()},
        WorkerConfig(),
    )
    assert WorkerRegistry._capabilities(frozenset({"memory_dream"})) == [
        "memory_dream",
    ]

    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerService(
            None,
            None,
            {"unknown_job": object()},
            WorkerConfig(),
        )
    with pytest.raises(ValueError, match="unsupported job type"):
        WorkerRegistry._capabilities(frozenset({"unknown_job"}))


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


@pytest.mark.asyncio
async def test_run_recovers_from_postgres_recovery_before_claim_returns() -> None:
    backend = _FakeBackend(job_count=1)
    backend.claim_errors.append(
        CannotConnectNowError("database recovery detail must stay private"),
    )
    stop_event = asyncio.Event()

    async def handler(_claim, _authority):
        stop_event.set()
        return JobOutcome.succeeded()

    registry = _FakeRegistry()
    service = WorkerService(
        _Factory(backend),
        registry,
        {"retention_purge": handler},
        _config(max_concurrent_jobs=1, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    await asyncio.wait_for(service.run(stop_event), timeout=1)

    assert backend.claim_calls == 2
    assert len(backend.succeeded) == 1
    assert registry.calls == ["register", "mark_draining", "remove"]


@pytest.mark.asyncio
async def test_claim_commit_ack_failure_is_not_retried() -> None:
    backend = _FakeBackend(job_count=1)
    backend.transaction_exit_errors.append(
        DBAPIError(
            "COMMIT",
            {},
            ConnectionError("commit acknowledgement detail must stay private"),
            connection_invalidated=True,
        ),
    )
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": object()},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(DBAPIError):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert backend.claim_calls == 1
    assert backend.marked_running == []


@pytest.mark.asyncio
async def test_after_claim_commit_database_failure_is_not_retried() -> None:
    backend = _FakeBackend(job_count=1)

    async def after_claim_commit() -> None:
        raise CannotConnectNowError("reconciliation detail must stay private")

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": object()},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
        after_claim_commit=after_claim_commit,
    )

    with pytest.raises(CannotConnectNowError):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert backend.claim_calls == 1
    assert backend.marked_running == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_error",
    [
        RuntimeError("claim invariant failed"),
        PermissionError("claim authority failed"),
        ProgrammingError(
            "SELECT broken",
            {},
            Exception("database programming detail"),
        ),
    ],
)
async def test_non_transient_claim_failure_is_not_retried(
    claim_error: BaseException,
) -> None:
    backend = _FakeBackend(job_count=0)
    backend.claim_errors.append(claim_error)
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(type(claim_error)):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert backend.claim_calls == 1


@pytest.mark.asyncio
async def test_authority_failure_with_database_context_is_not_retried() -> None:
    backend = _FakeBackend(job_count=0)
    authority_error = PermissionError("claim authority failed")
    authority_error.__cause__ = CannotConnectNowError(
        "prior database detail must not redefine authority failure",
    )
    backend.claim_errors.append(authority_error)
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(PermissionError, match="claim authority failed"):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=0.2)

    assert backend.claim_calls == 1


@pytest.mark.asyncio
async def test_register_database_failure_remains_a_startup_failure() -> None:
    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    registry.register_error = CannotConnectNowError(
        "startup database recovery detail must stay private",
    )
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(CannotConnectNowError):
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert backend.claim_calls == 0
    assert registry.calls == ["register"]


@pytest.mark.asyncio
async def test_stop_interrupts_claim_reconnect_backoff() -> None:
    backend = _FakeBackend(job_count=0)
    backend.claim_started = asyncio.Event()
    backend.claim_errors.append(
        CannotConnectNowError("database recovery detail must stay private"),
    )
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {},
        _config(poll_interval_seconds=30),
        repository_builder=_FakeRepository,
    )
    stop_event = asyncio.Event()
    running = asyncio.create_task(service.run(stop_event))

    await asyncio.wait_for(backend.claim_started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(running, timeout=0.1)

    assert backend.claim_calls == 1


@pytest.mark.asyncio
async def test_primary_claim_failure_survives_shutdown_database_failure() -> None:
    backend = _FakeBackend(job_count=0)
    backend.claim_errors.append(RuntimeError("claim invariant failed"))
    registry = _FakeRegistry()
    registry.mark_draining_error = CannotConnectNowError(
        "shutdown database recovery detail must stay private",
    )
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(RuntimeError, match="claim invariant failed") as caught:
        await asyncio.wait_for(service.run(asyncio.Event()), timeout=1)

    assert caught.value.__notes__ == [
        "worker shutdown also failed: CannotConnectNowError",
    ]
    assert registry.calls == ["register", "mark_draining", "remove"]


@pytest.mark.asyncio
async def test_worker_passes_stable_execution_domain_affinity_to_claim_sql() -> None:
    backend = _FakeBackend(job_count=0)
    execution_domain = _execution_domain()
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {},
        _config(),
        repository_builder=_FakeRepository,
        execution_domain=execution_domain,
    )

    assert await service._claim_next() is None
    assert backend.claim_kwargs[0]["execution_domain_affinity"] == execution_domain.affinity


@pytest.mark.asyncio
async def test_worker_registers_its_stable_execution_domain_affinity() -> None:
    backend = _FakeBackend(job_count=0)
    registry = _FakeRegistry()
    execution_domain = _execution_domain()
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(),
        repository_builder=_FakeRepository,
        execution_domain=execution_domain,
    )

    await service._register()

    assert registry.register_kwargs == [
        {"execution_domain_affinity": execution_domain.affinity},
    ]


def test_worker_rejects_malformed_execution_domain_affinity() -> None:
    with pytest.raises(TypeError, match="execution_domain"):
        WorkerService(
            None,
            None,
            {},
            WorkerConfig(),
            execution_domain="not-a-domain",  # type: ignore[arg-type]
        )


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
async def test_settlement_connectivity_failure_abandons_lease_without_worker_failure() -> None:
    """An unknown settlement outcome must not escalate into a process failure.

    A database connectivity failure during the settlement transaction cannot
    prove whether the commit landed (the ACK may have been lost), so the
    Worker abandons the lease for exact-scope durable recovery instead of
    retrying in-process or killing every other in-flight Run on this process.
    """

    backend = _FakeBackend(job_count=0)
    claim = _claim(1)

    async def handler(_claim, _authority):
        async def commit() -> None:
            raise SQLAlchemyTimeoutError()

        return JobSettlement(JobOutcome.succeeded(), commit)

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(heartbeat_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    await service._execute_claim(claim)

    assert backend.succeeded == []
    assert backend.failed == []
    assert backend.cancelled == []


@pytest.mark.asyncio
async def test_settlement_outcome_unknown_does_not_stop_a_sibling_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One uncertain Run Settlement remains scoped to its exact Job Attempt."""

    backend = _FakeBackend(job_count=2)
    uncertain_job_id, sibling_job_id = tuple(claim.job_id for claim in backend.claims)

    async def handler(claim, _authority):
        async def commit() -> None:
            if claim.job_id == uncertain_job_id:
                raise SQLAlchemyTimeoutError()
            backend.succeeded.append(claim.job_id)

        return JobSettlement(JobOutcome.succeeded(), commit)

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(
            heartbeat_seconds=0.001,
            max_concurrent_jobs=2,
            poll_interval_seconds=0.001,
        ),
        repository_builder=_FakeRepository,
    )

    await service.run_until_idle()

    assert backend.succeeded == [sibling_job_id]
    assert backend.failed == []
    assert str(uncertain_job_id) not in caplog.text
    assert "Job settlement outcome unknown" in caplog.text


@pytest.mark.asyncio
async def test_settlement_invariant_failure_keeps_process_level_error() -> None:
    """Programming invariants inside settlement stay loud."""

    backend = _FakeBackend(job_count=0)
    claim = _claim(1)

    async def handler(_claim, _authority):
        async def commit() -> None:
            raise ValueError("settlement invariant violated")

        return JobSettlement(JobOutcome.succeeded(), commit)

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": handler},
        _config(heartbeat_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(ValueError):
        await service._execute_claim(claim)


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

        joined = asyncio.create_task(service.join_detached())
        await asyncio.sleep(0.02)
        assert not joined.done()
        joined.cancel("shutdown caller cancelled once")
        await asyncio.sleep(0.02)
        assert not joined.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(joined, timeout=1)
        assert service._detached == set()
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
    claim = backend.claims[0]
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
    assert backend.released == [
        (
            claim.job_id,
            claim.lease_token,
            claim.attempt_id,
            service.worker_id,
        )
    ]


@pytest.mark.asyncio
async def test_post_claim_hook_failure_releases_before_returning_primary_error() -> None:
    backend = _FakeBackend(job_count=1)
    claim = backend.claims[0]

    async def fail_after_claim_commit() -> None:
        raise RuntimeError("post-claim dispatch failed")

    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": object()},
        _config(),
        repository_builder=_FakeRepository,
        after_claim_commit=fail_after_claim_commit,
    )

    with pytest.raises(RuntimeError, match="post-claim dispatch failed"):
        await service._claim_next()

    assert backend.released == [
        (
            claim.job_id,
            claim.lease_token,
            claim.attempt_id,
            service.worker_id,
        )
    ]


@pytest.mark.asyncio
async def test_release_commit_unknown_is_attempted_once_without_retry() -> None:
    backend = _FakeBackend(job_count=1)
    claim = backend.claims[0]
    backend.transaction_exit_errors.append(
        CannotConnectNowError("release commit acknowledgement unknown"),
    )
    service = WorkerService(
        _Factory(backend),
        _FakeRegistry(),
        {"retention_purge": object()},
        _config(),
        repository_builder=_FakeRepository,
    )

    with pytest.raises(CannotConnectNowError):
        await service._release_unstarted_claim(claim)

    assert backend.released == [
        (
            claim.job_id,
            claim.lease_token,
            claim.attempt_id,
            service.worker_id,
        )
    ]


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
async def test_fleet_heartbeat_recovers_from_postgres_recovery() -> None:
    backend = _FakeBackend(job_count=0)
    stop_event = asyncio.Event()

    class RecoveringRegistry(_FakeRegistry):
        async def heartbeat(self, *args, **kwargs) -> bool:
            result = await super().heartbeat(*args, **kwargs)
            if self.calls.count("heartbeat") == 2:
                stop_event.set()
            return result

    registry = RecoveringRegistry()
    registry.heartbeat_errors.append(
        CannotConnectNowError("fleet recovery detail must stay private"),
    )
    service = WorkerService(
        _Factory(backend),
        registry,
        {},
        _config(heartbeat_seconds=0.001, poll_interval_seconds=0.001),
        repository_builder=_FakeRepository,
    )

    await asyncio.wait_for(service.run(stop_event), timeout=1)

    assert registry.calls.count("heartbeat") == 2
    assert registry.calls[-2:] == ["mark_draining", "remove"]


@pytest.mark.asyncio
async def test_fleet_loss_during_claim_never_starts_claimed_handler() -> None:
    backend = _FakeBackend(job_count=1)
    claim = backend.claims[0]
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
    assert backend.released == [
        (
            claim.job_id,
            claim.lease_token,
            claim.attempt_id,
            service.worker_id,
        )
    ]


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
