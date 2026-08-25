from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.worker.memory_dream_prepare as prepare_worker_module
from app.private_work.errors import (
    PrivateWorkCompactionDisabled,
    PrivateWorkConflict,
    PrivateWorkThreadBusy,
)
from app.private_work.memory_dream_service import MemoryDreamModelUnavailable
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.system_runtime_settings.models import AgentRuntimePolicyValue
from app.worker.memory_dream_prepare import MemoryDreamPrepareJobHandler, _PrepareWork
from app.worker.service import JobSettlement, LeaseLost
from deerflow.config.app_config import AppConfig
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentScope,
)
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareNotFound,
)
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
from deerflow.runtime.context_compaction import ThreadCompactionResult


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    async def scalar(self, _statement):
        return True


class _Authority:
    cancel_requested = False

    def __init__(self) -> None:
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None

    async def set_phase(self, _scope, **kwargs):
        self.calls.append(("phase", kwargs))
        if self.error is not None:
            raise self.error

    async def record_pass(self, _scope, **kwargs):
        self.calls.append(("pass", kwargs))
        if self.error is not None:
            raise self.error

    async def retry_or_dead(self, _scope, **kwargs):
        self.calls.append(("retry", kwargs))
        if self.error is not None:
            raise self.error

    async def settle_cancelled(self, _scope, **kwargs):
        self.calls.append(("cancel", kwargs))
        if self.error is not None:
            raise self.error

    async def read_execution(self, _scope, **_kwargs):
        return SimpleNamespace(
            thread_id="thread-prepare",
            request_id="memory-dream-prepare-worker",
        )

    async def link_dream(self, _scope, **kwargs):
        self.calls.append(("link", kwargs))

    async def settle_success(self, _scope, **kwargs):
        self.calls.append(("success", kwargs))


class _Jobs:
    def __init__(self, *, settled: bool = True) -> None:
        self.settled = settled
        self.calls: list[dict[str, object]] = []

    async def settle_cancelled(self, job_id, **kwargs):
        self.calls.append({"job_id": job_id, **kwargs})
        return self.settled


class _Barrier:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def compact(self, _context, thread_id, **kwargs):
        self.calls.append({"thread_id": thread_id, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def lock_and_verify_dream_archive_ready(self, *_args, **_kwargs):
        return True


class _PolicySensitiveBarrier(_Barrier):
    async def compact(self, context, thread_id, **kwargs):
        if not kwargs["app_config"].summarization.enabled:
            return ThreadCompactionResult(
                thread_id=thread_id,
                compacted=False,
                reason="compaction_failed",
            )
        return await super().compact(context, thread_id, **kwargs)

    async def lock_and_verify_dream_archive_ready(self, *_args, **kwargs):
        return bool(kwargs["app_config"].summarization.enabled)


class _Personalization:
    async def read_memory(self, _owner_user_id):
        return SimpleNamespace(memory_enabled=True)


class _Admission:
    async def require_account_private_generation_after_membership(
        self,
        _session,
        scope,
    ):
        return AccountPrivateGeneration(
            owner_user_id=scope.owner_user_id,
            generation=1,
        )

    async def admit(self, *_args, **_kwargs):
        return SimpleNamespace(
            disposition="nothing_pending",
            job_id=None,
            admission_kind="memory_dream",
            history_count=0,
        )


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="prepare-lease",
        job_type="memory_dream_prepare",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        namespace="user-private",
        origin_trace_id=None,
    )


def _handler(
    repository: _Repository,
    *,
    barrier: _Barrier | None = None,
    jobs: _Jobs | None = None,
) -> MemoryDreamPrepareJobHandler:
    value = object.__new__(MemoryDreamPrepareJobHandler)
    value._sessions = lambda: _Session()
    value._app_config = object()
    value._barrier = barrier or _Barrier([])
    value._admission = object()
    value._repository_builder = lambda _session, *, jobs: repository
    value._job_repository_builder = lambda _session: jobs or _Jobs()
    value._personalization_repository_builder = object()
    value._retry_initial_seconds = 5
    value._retry_max_seconds = 30
    value._audit = None

    async def lock_authority(_session, _scope):
        repository.calls.append(("authority", {}))

    value._lock_settlement_authority = lock_authority
    return value


def _work(claim: JobClaim) -> _PrepareWork:
    scope = MemoryDocumentScope(
        project_id=claim.scope.project_id,
        owner_user_id=claim.scope.owner_user_id or "",
        namespace=claim.namespace or "",
    )
    return _PrepareWork(
        context=SimpleNamespace(),  # type: ignore[arg-type]
        scope=scope,
        thread_id="thread-prepare",
        request_id="memory-dream-prepare-worker",
        app_config=AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
            }
        ),
    )


@pytest.mark.asyncio
async def test_prepare_worker_compacts_pass_by_pass_before_final_settlement() -> None:
    repository = _Repository()
    barrier = _Barrier(
        [
            ThreadCompactionResult(
                thread_id="thread-prepare",
                compacted=True,
                removed_message_count=2,
                preserved_message_count=0,
                summary_updated=True,
                checkpoint_id="checkpoint-1",
            ),
            ThreadCompactionResult(
                thread_id="thread-prepare",
                compacted=False,
                reason="not_enough_messages",
            ),
        ]
    )
    handler = _handler(repository, barrier=barrier)
    claim = _claim()
    handler._authorize = lambda _claim: _async_value(_work(claim))

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    assert [name for name, _kwargs in repository.calls] == [
        "phase",
        "pass",
        "phase",
    ]
    assert len(barrier.calls) == 2
    assert all(call["keep"] == ("messages", 0) for call in barrier.calls)


@pytest.mark.asyncio
async def test_prepare_worker_applies_current_database_policy_to_base_compaction_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    owner_user_id = claim.scope.owner_user_id or ""

    async def resolve(*_args, **_kwargs):
        return ProjectContext(
            user_id=uuid.UUID(owner_user_id),
            project_id=claim.scope.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-prepare-worker",
        )

    async def materialize(*_args, **_kwargs):
        return AgentRuntimePolicyValue(), 23

    monkeypatch.setattr(
        prepare_worker_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        prepare_worker_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    repository = _Repository()
    barrier = _PolicySensitiveBarrier(
        [
            ThreadCompactionResult(
                thread_id="thread-prepare",
                compacted=False,
                reason="not_enough_messages",
            )
        ]
    )
    handler = MemoryDreamPrepareJobHandler(
        lambda: _Session(),
        app_config=AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
                "summarization": {"enabled": False},
            }
        ),
        barrier=barrier,
        admission=_Admission(),  # type: ignore[arg-type]
        repository_builder=lambda _session, *, jobs: repository,
        job_repository_builder=lambda _session: _Jobs(),
        personalization_repository_builder=lambda _session: _Personalization(),
    )

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    await settlement.commit()
    assert [name for name, _kwargs in repository.calls][-2:] == [
        "link",
        "success",
    ]


@pytest.mark.asyncio
async def test_prepare_final_settlement_retries_when_dream_model_becomes_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    owner_user_id = claim.scope.owner_user_id or ""

    async def resolve(*_args, **_kwargs):
        return ProjectContext(
            user_id=uuid.UUID(owner_user_id),
            project_id=claim.scope.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-prepare-worker",
        )

    class Admission:
        async def require_account_private_generation_after_membership(
            self,
            _session,
            scope,
        ):
            return AccountPrivateGeneration(
                owner_user_id=scope.owner_user_id,
                generation=1,
            )

        async def admit(self, *_args, **_kwargs):
            raise MemoryDreamModelUnavailable

    monkeypatch.setattr(
        prepare_worker_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    repository = _Repository()
    handler = _handler(repository, barrier=_Barrier([]))
    handler._admission = Admission()

    await handler._final_settlement(claim, _work(claim)).commit()

    assert [name for name, _kwargs in repository.calls] == [
        "phase",
        "retry",
    ]
    assert repository.calls[-1][1]["public_error_code"] == ("MEMORY_DREAM_MODEL_UNAVAILABLE")


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_prepare_worker_stalled_compaction_returns_failure_settlement() -> None:
    repository = _Repository()
    barrier = _Barrier(
        [
            ThreadCompactionResult(
                thread_id="thread-prepare",
                compacted=True,
                removed_message_count=0,
                preserved_message_count=1,
                summary_updated=True,
                checkpoint_id="checkpoint-1",
            )
        ]
    )
    handler = _handler(repository, barrier=barrier)
    claim = _claim()
    handler._authorize = lambda _claim: _async_value(_work(claim))

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == "MEMORY_DREAM_PREPARE_PROGRESS_STALLED"
    await settlement.commit()
    assert [name for name, _kwargs in repository.calls][-2:] == [
        "authority",
        "retry",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        (
            "source_too_large",
            "MEMORY_DREAM_PREPARE_SOURCE_TOO_LARGE",
        ),
        (
            "prompt_budget_too_small",
            "MEMORY_DREAM_PREPARE_PROMPT_BUDGET_TOO_SMALL",
        ),
        ("compaction_failed", "MEMORY_DREAM_PREPARE_DRAIN_FAILED"),
    ],
)
async def test_prepare_worker_preserves_permanent_compaction_failure_reason(
    reason: str,
    expected_code: str,
) -> None:
    repository = _Repository()
    barrier = _Barrier(
        [
            ThreadCompactionResult(
                thread_id="thread-prepare",
                compacted=False,
                reason=reason,
            )
        ]
    )
    handler = _handler(repository, barrier=barrier)
    claim = _claim()
    handler._authorize = lambda _claim: _async_value(_work(claim))

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            PrivateWorkThreadBusy("memory-dream-prepare-worker"),
            "MEMORY_DREAM_PREPARE_THREAD_BUSY",
        ),
        (
            PrivateWorkCompactionDisabled("memory-dream-prepare-worker"),
            "MEMORY_DREAM_PREPARE_COMPACTION_DISABLED",
        ),
        (
            PrivateWorkConflict("memory-dream-prepare-worker"),
            "MEMORY_DREAM_PREPARE_HEAD_CHANGED",
        ),
    ],
)
async def test_prepare_worker_preserves_machine_compaction_outcomes(
    error: Exception,
    expected_code: str,
) -> None:
    repository = _Repository()
    barrier = _Barrier([error])
    handler = _handler(repository, barrier=barrier)
    claim = _claim()
    handler._authorize = lambda _claim: _async_value(_work(claim))

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == expected_code


@pytest.mark.asyncio
async def test_prepare_worker_failure_and_cancel_settlements_lock_authority_first() -> None:
    repository = _Repository()
    claim = _claim()
    handler = _handler(repository)

    failed = handler._failure_settlement(claim, "MEMORY_DREAM_PREPARE_FAILED")
    cancelled = handler._cancel_settlement(claim)
    await failed.commit()
    await cancelled.commit()

    assert [name for name, _kwargs in repository.calls] == [
        "authority",
        "retry",
        "authority",
        "cancel",
    ]


@pytest.mark.asyncio
async def test_prepare_worker_cancel_falls_back_after_reset_deleted_row() -> None:
    repository = _Repository()
    repository.error = MemoryDreamPrepareNotFound()
    jobs = _Jobs()
    claim = _claim()
    settlement = _handler(repository, jobs=jobs)._cancel_settlement(claim)

    await settlement.commit()

    assert jobs.calls[0]["job_id"] == claim.job_id
    assert jobs.calls[0]["lease_token"] == claim.lease_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [MemoryDreamPrepareConflict(), MemoryDreamPrepareNotFound()],
)
async def test_prepare_worker_lost_lease_fails_settlement(
    error: Exception,
) -> None:
    repository = _Repository()
    repository.error = error
    claim = _claim()
    settlement = _handler(repository)._failure_settlement(
        claim,
        "MEMORY_DREAM_PREPARE_FAILED",
    )

    with pytest.raises(LeaseLost):
        await settlement.commit()


@pytest.mark.asyncio
async def test_prepare_worker_authority_loss_selects_cooperative_cancel() -> None:
    repository = _Repository()
    claim = _claim()
    handler = _handler(repository)
    handler._authorize = lambda _claim: _async_value(None)

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "cancelled"
