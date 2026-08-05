"""Bounded, lease-authorized execution loop for an independent Worker."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.jobs.sql import JobClaim, JobHeartbeat, JobRepository
from deerflow.trace_context import normalize_trace_id, request_trace_context

_PUBLIC_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    """The current process no longer owns the job lease."""

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        super().__init__("job lease ownership was lost")


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: Literal["succeeded", "cancelled", "failed"]
    public_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "failed":
            if self.public_error_code is None or _PUBLIC_ERROR_CODE.fullmatch(self.public_error_code) is None:
                raise ValueError("failed job outcome requires a stable public error code")
        elif self.public_error_code is not None:
            raise ValueError("terminal success/cancel outcomes cannot carry an error code")

    @classmethod
    def succeeded(cls) -> JobOutcome:
        return cls("succeeded")

    @classmethod
    def cancelled(cls) -> JobOutcome:
        return cls("cancelled")

    @classmethod
    def failed(cls, public_error_code: str) -> JobOutcome:
        return cls("failed", public_error_code)

    @classmethod
    def retry(cls, public_error_code: str) -> JobOutcome:
        return cls.failed(public_error_code)


@dataclass(frozen=True, slots=True)
class JobSettlement:
    """Handler-owned atomic settlement committed after heartbeat quiescence."""

    outcome: JobOutcome
    _commit: Callable[[], Awaitable[None]] = field(repr=False)
    _commit_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _committed: bool = field(default=False, init=False, repr=False, compare=False)

    async def commit(self) -> None:
        async with self._commit_lock:
            if self._committed:
                return
            await self._commit()
            object.__setattr__(self, "_committed", True)


class JobHandler(Protocol):
    async def __call__(
        self,
        claim: JobClaim,
        authority: JobLeaseAuthority,
    ) -> JobOutcome | JobSettlement: ...


class WorkerRegistryPort(Protocol):
    async def register(
        self,
        worker_id: uuid.UUID,
        capabilities: frozenset[str],
        max_concurrent_jobs: int,
        **kwargs,
    ) -> None: ...

    async def heartbeat(self, worker_id: uuid.UUID, **kwargs) -> bool: ...

    async def mark_draining(self, worker_id: uuid.UUID, **kwargs) -> bool: ...

    async def remove(self, worker_id: uuid.UUID) -> bool: ...


RepositoryBuilder = Callable[[AsyncSession], JobRepository]
_CANCEL_DRAIN_SECONDS = 0.05


def _task_key(identifier: uuid.UUID) -> str:
    return hashlib.sha256(identifier.bytes).hexdigest()[:12]


class JobLeaseAuthority:
    """A handler's only authority to extend and observe its current lease."""

    def __init__(
        self,
        repository_factory,
        claim: JobClaim,
        *,
        lease_seconds: int,
        repository_builder: RepositoryBuilder = JobRepository,
    ) -> None:
        self._factory = repository_factory
        self._repository_builder = repository_builder
        self.claim = claim
        self._lease_seconds = lease_seconds
        self._cancel_requested = claim.cancel_requested
        self._invalidated = False
        self._heartbeat_callback: Callable[[], Awaitable[None]] | None = None
        self._cancel_callback: Callable[[], Awaitable[None] | None] | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def invalidate(self) -> None:
        self._invalidated = True

    def bind_heartbeat_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        if self._heartbeat_callback is not None:
            raise RuntimeError("job heartbeat callback is already bound")
        self._heartbeat_callback = callback

    def bind_cancel_callback(
        self,
        callback: Callable[[], Awaitable[None] | None],
    ) -> None:
        if self._cancel_callback is not None:
            raise RuntimeError("job cancel callback is already bound")
        self._cancel_callback = callback

    async def heartbeat(self) -> None:
        if self._invalidated:
            raise LeaseLost(self.claim.job_id)
        try:
            async with self._factory() as session, session.begin():
                result = await self._repository_builder(session).heartbeat(
                    self.claim.job_id,
                    lease_token=self.claim.lease_token,
                    lease_seconds=self._lease_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.invalidate()
            raise LeaseLost(self.claim.job_id) from error
        if result is False:
            self.invalidate()
            raise LeaseLost(self.claim.job_id)
        if not isinstance(result, JobHeartbeat):
            self.invalidate()
            raise LeaseLost(self.claim.job_id)
        if self._invalidated:
            raise LeaseLost(self.claim.job_id)
        self._cancel_requested = result.cancel_requested
        if self._heartbeat_callback is not None:
            try:
                await self._heartbeat_callback()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.invalidate()
                raise LeaseLost(self.claim.job_id) from error
        if self._cancel_requested and self._cancel_callback is not None:
            try:
                result = self._cancel_callback()
                if result is not None:
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.invalidate()
                raise LeaseLost(self.claim.job_id) from error


class WorkerService:
    """Claim jobs up to configured capacity and drain without forging state."""

    def __init__(
        self,
        repository_factory,
        registry: WorkerRegistryPort,
        handlers: dict[str, JobHandler],
        config: WorkerConfig,
        *,
        repository_builder: RepositoryBuilder = JobRepository,
        worker_id: uuid.UUID | None = None,
        after_claim_commit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(config, WorkerConfig):
            raise TypeError("WorkerConfig is required")
        if not set(handlers).issubset(
            {
                "private_run",
                "automation_run",
                "retention_purge",
                "mcp_discovery",
                "memory_extract",
                "memory_consolidate",
                "memory_retention_purge",
            }
        ):
            raise ValueError("worker handlers include an unsupported job type")
        self._factory = repository_factory
        self._repository_builder = repository_builder
        self._registry = registry
        self._handlers = dict(handlers)
        self._config = config
        self._after_claim_commit = after_claim_commit
        self.worker_id = worker_id or uuid.uuid4()
        self._inflight: set[asyncio.Task[None]] = set()
        self._detached: set[asyncio.Task] = set()
        self._accepting = False
        self._draining = False

    def _consume_detached(self, task: asyncio.Task) -> None:
        self._detached.discard(task)
        with suppress(asyncio.CancelledError):
            task.exception()

    def _detach(self, task: asyncio.Task) -> None:
        if task.done():
            self._consume_detached(task)
            return
        self._detached.add(task)
        task.add_done_callback(self._consume_detached)

    @asynccontextmanager
    async def _repository(self):
        async with self._factory() as session, session.begin():
            yield self._repository_builder(session)

    async def _claim_next(self) -> JobClaim | None:
        async with self._repository() as repository:
            claim = await repository.claim_next(
                worker_id=self.worker_id,
                capabilities=frozenset(self._handlers),
                lease_seconds=self._config.lease_seconds,
            )
        if self._after_claim_commit is not None:
            await self._after_claim_commit()
        return claim

    async def _mark_running(self, claim: JobClaim) -> bool:
        async with self._repository() as repository:
            return await repository.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

    async def _settle(self, claim: JobClaim, outcome: JobOutcome) -> None:
        async with self._repository() as repository:
            if outcome.status == "succeeded":
                await repository.settle_success(
                    claim.job_id,
                    lease_token=claim.lease_token,
                )
            elif outcome.status == "cancelled":
                await repository.settle_cancelled(
                    claim.job_id,
                    lease_token=claim.lease_token,
                )
            else:
                await repository.retry_or_dead(
                    claim.job_id,
                    lease_token=claim.lease_token,
                    public_error_code=outcome.public_error_code,
                    retry_initial_seconds=self._config.retry_initial_seconds,
                    retry_max_seconds=self._config.retry_max_seconds,
                )

    async def _heartbeat_claim(
        self,
        authority: JobLeaseAuthority,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._config.heartbeat_seconds,
                )
            except TimeoutError:
                await authority.heartbeat()

    async def _stop_handler_after_lease_loss(self, handler_task: asyncio.Task) -> None:
        handler_task.cancel()
        try:
            done, pending = await asyncio.wait(
                {handler_task},
                timeout=_CANCEL_DRAIN_SECONDS,
            )
        except asyncio.CancelledError:
            self._detach(handler_task)
            raise
        if done:
            await asyncio.gather(*done, return_exceptions=True)
            return
        for task in pending:
            self._detach(task)
        self._accepting = False
        raise RuntimeError("job handler did not stop after lease loss")

    async def _execute_claim(self, claim: JobClaim) -> None:
        if claim.job_type in {"private_run", "automation_run"}:
            origin_trace_id = normalize_trace_id(claim.origin_trace_id)
            if origin_trace_id is None:
                raise LeaseLost(claim.job_id)
            trace_context = request_trace_context(origin_trace_id)
        else:
            trace_context = nullcontext()
        with trace_context:
            await self._execute_claim_with_trace(claim)

    async def _execute_claim_with_trace(self, claim: JobClaim) -> None:
        if not await self._mark_running(claim):
            return
        authority = JobLeaseAuthority(
            self._factory,
            claim,
            lease_seconds=self._config.lease_seconds,
            repository_builder=self._repository_builder,
        )
        heartbeat_stop = asyncio.Event()
        task_key = _task_key(claim.job_id)
        heartbeat_task = asyncio.create_task(
            self._heartbeat_claim(authority, heartbeat_stop),
            name=f"job-heartbeat-{task_key}",
        )
        handler_task = asyncio.create_task(
            self._handlers[claim.job_type](claim, authority),
            name=f"job-handler-{task_key}",
        )
        try:
            done, _pending = await asyncio.wait(
                {handler_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("job heartbeat stopped before the handler")
            try:
                result = await handler_task
            except asyncio.CancelledError:
                raise
            except LeaseLost:
                raise
            except Exception:
                result = JobOutcome.failed("WORKER_HANDLER_FAILED")
            if isinstance(result, JobSettlement):
                outcome = result.outcome
            elif isinstance(result, JobOutcome):
                outcome = result
            else:
                result = JobOutcome.failed("INVALID_JOB_OUTCOME")
                outcome = result
            heartbeat_stop.set()
            await heartbeat_task
            if isinstance(result, JobSettlement):
                await result.commit()
            else:
                await self._settle(claim, outcome)
        except LeaseLost:
            authority.invalidate()
            await self._stop_handler_after_lease_loss(handler_task)
        except Exception:
            authority.invalidate()
            handler_task.cancel()
            self._detach(handler_task)
            raise
        except asyncio.CancelledError:
            authority.invalidate()
            handler_task.cancel()
            heartbeat_task.cancel()
            self._detach(handler_task)
            self._detach(heartbeat_task)
            raise
        finally:
            heartbeat_stop.set()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
                self._detach(heartbeat_task)

    async def _fill_capacity(self, stop_event: asyncio.Event | None = None) -> bool:
        claimed_any = False
        while self._accepting and len(self._inflight) < self._config.max_concurrent_jobs:
            if not self._accepting or (stop_event is not None and stop_event.is_set()):
                break
            claim = await self._claim_next()
            if claim is None:
                break
            if not self._accepting or (stop_event is not None and stop_event.is_set()):
                break
            task = asyncio.create_task(
                self._execute_claim(claim),
                name=f"worker-job-{_task_key(claim.job_id)}",
            )
            self._inflight.add(task)
            claimed_any = True
        return claimed_any

    async def _reap_completed(self) -> None:
        completed = {task for task in self._inflight if task.done()}
        for task in completed:
            self._inflight.discard(task)
            await task

    async def _cancel_task_bounded(self, task: asyncio.Task) -> None:
        task.cancel()
        done, pending = await asyncio.wait(
            {task},
            timeout=_CANCEL_DRAIN_SECONDS,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for item in pending:
            self._detach(item)

    async def _fleet_heartbeat(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._config.heartbeat_seconds,
                )
            except TimeoutError:
                try:
                    alive = await self._registry.heartbeat(self.worker_id)
                except Exception:
                    self._accepting = False
                    raise
                if not alive:
                    self._accepting = False
                    raise RuntimeError("worker registry ownership was lost")

    async def _register(self) -> None:
        await self._registry.register(
            self.worker_id,
            frozenset(self._handlers),
            self._config.max_concurrent_jobs,
        )
        self._accepting = True
        self._draining = False

    async def drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self._accepting = False
        try:
            await self._registry.mark_draining(self.worker_id)
        finally:
            await self._drain_inflight()

    async def _drain_inflight(self) -> None:
        if not self._inflight:
            return
        done, pending = await asyncio.wait(
            set(self._inflight),
            timeout=self._config.shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            cancelled_done, still_pending = await asyncio.wait(
                pending,
                timeout=_CANCEL_DRAIN_SECONDS,
            )
            done.update(cancelled_done)
            pending = still_pending
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        if pending:
            for task in pending:
                self._detach(task)
        self._inflight.clear()

    async def _shutdown(self) -> None:
        error: BaseException | None = None
        try:
            await self.drain()
        except BaseException as caught:
            error = caught
        try:
            await self._registry.remove(self.worker_id)
        except BaseException as caught:
            if error is None:
                error = caught
            else:
                error.add_note(f"worker registry removal also failed: {type(caught).__name__}")
        if error is not None:
            raise error

    async def run_until_idle(self) -> None:
        await self._register()
        try:
            while self._accepting:
                claimed = await self._fill_capacity()
                if not self._inflight and not claimed:
                    break
                if self._inflight:
                    done, _pending = await asyncio.wait(
                        set(self._inflight),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        self._inflight.discard(task)
                        await task
        finally:
            await self._shutdown()

    async def run(self, stop_event: asyncio.Event) -> None:
        await self._register()
        fleet_task = asyncio.create_task(
            self._fleet_heartbeat(stop_event),
            name=f"worker-fleet-heartbeat-{_task_key(self.worker_id)}",
        )
        try:
            while not stop_event.is_set() and self._accepting:
                if fleet_task.done():
                    await fleet_task
                await self._reap_completed()
                await self._fill_capacity(stop_event)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._config.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
            if fleet_task.done():
                await fleet_task
        finally:
            await self._cancel_task_bounded(fleet_task)
            await self._shutdown()


__all__ = [
    "JobHandler",
    "JobLeaseAuthority",
    "JobOutcome",
    "JobSettlement",
    "LeaseLost",
    "WorkerService",
]
