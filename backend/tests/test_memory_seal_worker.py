"""Memory seal contract: discovery, admission, drain, preemption, settlement."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest

import app.private_work.memory_seal_service as seal_service_module
import app.worker.memory_seal as seal_worker_module
from app.audit.models import (
    AUDIT_ACTION_CONTRACTS,
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditProcess,
    AuditTargetKind,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict, PrivateWorkUnavailable
from app.private_work.memory_seal_service import (
    MemorySealAdmissionService,
    MemorySealSchedulerService,
    compute_seal_idempotency_key,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectNotFound
from app.projects.models import ProjectRole
from app.worker.memory_seal import MemorySealJobHandler
from app.worker.service import JobSettlement, LeaseLost
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.runtime.context_compaction import ThreadCompactionResult

NOW = datetime(2026, 8, 6, 10, 20, 30, tzinfo=UTC)
PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OWNER_USER_ID = "22222222-2222-4222-8222-222222222222"
THREAD_ID = "33333333-3333-4333-8333-333333333333"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(
        self,
        *,
        execute_results: list[object] | None = None,
        scalar_results: list[object] | None = None,
    ) -> None:
        self.execute_results = execute_results or []
        self.scalar_results = scalar_results or []
        self.executed: list[object] = []
        self.scalar_statements: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement):
        self.executed.append(statement)
        if self.execute_results:
            return self.execute_results.pop(0)
        return _Rows(())

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None


class _Rows:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Jobs:
    def __init__(self) -> None:
        self.enqueued: list[object] = []
        self.settlements: list[dict[str, object]] = []
        self.settle_result = True

    async def enqueue(self, job):
        self.enqueued.append(job)
        return uuid.UUID("44444444-4444-4444-8444-444444444444")

    async def settle_success(self, job_id, *, lease_token):
        self.settlements.append({"job_id": job_id, "lease_token": lease_token})
        return self.settle_result


class _Personalization:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.reads: list[str] = []

    async def read_memory(self, owner_user_id, *, for_update: bool = False):
        self.reads.append(owner_user_id)

        class _Preference:
            memory_enabled = self.enabled

        return _Preference()


class _AdmissionAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def memory_seal_admitted(self, session, **kwargs):
        self.calls.append({"session": session, **kwargs})


class _SettlementAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def memory_seal_settled(self, session, **kwargs):
        self.calls.append({"session": session, **kwargs})


def _project_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID(OWNER_USER_ID),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=3,
        request_id="memory-seal-test",
    )


def _private_context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(_project_context())


def _claim(**overrides) -> JobClaim:
    values: dict[str, object] = {
        "job_id": uuid.uuid4(),
        "attempt_id": uuid.uuid4(),
        "lease_token": "seal-lease",
        "job_type": "memory_seal",
        "scope": JobScope(PROJECT_ID, OWNER_USER_ID),
        "run_id": None,
        "occurrence_id": None,
        "retry_safety": "safe",
        "cancel_requested": False,
        "namespace": THREAD_ID,
    }
    values.update(overrides)
    return JobClaim(**values)


class _Authority:
    def __init__(self, *, cancel_requested: bool = False) -> None:
        self.cancel_requested = cancel_requested
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1


class _Barrier:
    def __init__(
        self,
        *,
        compact_results: list[object] | None = None,
        verify_ready: bool = True,
    ) -> None:
        self.compact_results = compact_results or []
        self.verify_ready = verify_ready
        self.compact_calls: list[dict[str, object]] = []
        self.verify_calls: list[dict[str, object]] = []

    async def compact(self, context, thread_id, *, force, keep, app_config):
        self.compact_calls.append(
            {
                "context": context,
                "thread_id": thread_id,
                "force": force,
                "keep": keep,
            }
        )
        result = self.compact_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def lock_and_verify_dream_archive_ready(
        self,
        session,
        context,
        thread_id,
        *,
        app_config,
    ):
        self.verify_calls.append(
            {
                "session": session,
                "context": context,
                "thread_id": thread_id,
            }
        )
        if isinstance(self.verify_ready, Exception):
            raise self.verify_ready
        return self.verify_ready


def _compacted(checkpoint_id: str, *, removed: int = 3) -> ThreadCompactionResult:
    return ThreadCompactionResult(
        thread_id=THREAD_ID,
        compacted=True,
        removed_message_count=removed,
        checkpoint_id=checkpoint_id,
    )


def _drained() -> ThreadCompactionResult:
    return ThreadCompactionResult(
        thread_id=THREAD_ID,
        compacted=False,
        reason="not_enough_messages",
    )


def _handler(
    barrier: _Barrier,
    *,
    session: _Session | None = None,
    jobs: _Jobs | None = None,
    audit: _SettlementAudit | None = None,
    personalization: _Personalization | None = None,
) -> MemorySealJobHandler:
    active_session = session or _Session()
    return MemorySealJobHandler(
        lambda: active_session,
        app_config=None,
        barrier=barrier,
        job_repository_builder=lambda _session: jobs or _Jobs(),
        personalization_repository_builder=lambda _session: personalization or _Personalization(),
        audit=audit,
    )


def _policy(*, enabled: bool = True, idle_seal_minutes: int = 60):
    from app.system_runtime_settings.models import AgentRuntimePolicyValue

    return AgentRuntimePolicyValue.model_validate(
        {
            "memory": {
                "enabled": enabled,
                "idle_seal_minutes": idle_seal_minutes,
            }
        }
    )


def _patch_platform_policy(monkeypatch: pytest.MonkeyPatch, policy) -> list[object]:
    calls: list[object] = []

    async def materialize(session, section, *, for_update=False):
        calls.append((session, section, for_update))
        return policy, 23

    monkeypatch.setattr(
        seal_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    return calls


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def test_seal_idempotency_key_is_canonical_and_ordinal_separated() -> None:
    key = compute_seal_idempotency_key(
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_USER_ID,
        thread_id=THREAD_ID,
        ordinal=1,
    )
    expected_payload = json.dumps(
        {
            "domain": "actweave.memory.seal.v1",
            "ordinal": 1,
            "owner_user_id": OWNER_USER_ID,
            "project_id": str(PROJECT_ID),
            "thread_id": THREAD_ID,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert key == hashlib.sha256(expected_payload).hexdigest()

    second = compute_seal_idempotency_key(
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_USER_ID,
        thread_id=THREAD_ID,
        ordinal=2,
    )
    assert second != key


# ---------------------------------------------------------------------------
# Scheduler discovery and admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_is_disabled_when_platform_seal_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()
    service = MemorySealAdmissionService()

    _patch_platform_policy(monkeypatch, _policy(enabled=False))
    assert await service.list_due_threads(session, now=NOW) == ()

    _patch_platform_policy(monkeypatch, _policy(idle_seal_minutes=0))
    assert await service.list_due_threads(session, now=NOW) == ()

    assert session.executed == []


@pytest.mark.asyncio
async def test_discovery_rejects_out_of_contract_batches() -> None:
    service = MemorySealAdmissionService()
    with pytest.raises(ValueError, match="batch is invalid"):
        await service.list_due_threads(_Session(), now=NOW, max_jobs=0)
    with pytest.raises(ValueError, match="batch is invalid"):
        await service.list_due_threads(_Session(), now=NOW, max_jobs=21)


@pytest.mark.asyncio
async def test_admission_enqueues_one_job_with_thread_coordinate_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_policy(monkeypatch, _policy(idle_seal_minutes=45))
    resolved: list[dict[str, object]] = []

    async def resolve(session, user_id, project_identifier, request_id, *, lock):
        resolved.append(
            {
                "user_id": user_id,
                "project": project_identifier,
                "request_id": request_id,
                "lock": lock,
            }
        )
        return _project_context()

    monkeypatch.setattr(
        seal_service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    jobs = _Jobs()
    audit = _AdmissionAudit()
    personalization = _Personalization()
    session = _Session(
        execute_results=[_Rows((object(),))],
        scalar_results=[True, 3],
    )
    service = MemorySealAdmissionService(
        job_repository_builder=lambda _session: jobs,
        personalization_repository_builder=lambda _session: personalization,
        audit=audit,
    )

    job_id = await service.admit_thread(
        session,
        project_id=PROJECT_ID,
        owner_user_id=OWNER_USER_ID,
        thread_id=THREAD_ID,
        now=NOW,
    )

    assert job_id == uuid.UUID("44444444-4444-4444-8444-444444444444")
    assert resolved == [
        {
            "user_id": uuid.UUID(OWNER_USER_ID),
            "project": PROJECT_ID,
            "request_id": "memory-seal-scheduler",
            "lock": True,
        }
    ]
    assert personalization.reads == [OWNER_USER_ID]
    (job,) = jobs.enqueued
    assert job.job_type == "memory_seal"
    assert job.scope.project_id == PROJECT_ID
    assert job.scope.owner_user_id == OWNER_USER_ID
    assert job.namespace == THREAD_ID
    assert job.run_id is None
    assert job.occurrence_id is None
    assert job.max_attempts == 5
    assert job.retry_safety == "safe"
    assert job.idempotency_key == compute_seal_idempotency_key(
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_USER_ID,
        thread_id=THREAD_ID,
        ordinal=4,
    )
    (audited,) = audit.calls
    assert audited["project_id"] == PROJECT_ID
    assert audited["job_id"] == job_id
    assert audited["request_id"] == "memory-seal-scheduler"


@pytest.mark.asyncio
async def test_admission_skips_when_the_locked_recheck_is_no_longer_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_policy(monkeypatch, _policy())

    async def resolve(*_args, **_kwargs):
        return _project_context()

    monkeypatch.setattr(
        seal_service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    jobs = _Jobs()
    audit = _AdmissionAudit()
    session = _Session(
        execute_results=[_Rows((object(),))],
        scalar_results=[None],
    )
    service = MemorySealAdmissionService(
        job_repository_builder=lambda _session: jobs,
        personalization_repository_builder=lambda _session: _Personalization(),
        audit=audit,
    )

    assert (
        await service.admit_thread(
            session,
            project_id=PROJECT_ID,
            owner_user_id=OWNER_USER_ID,
            thread_id=THREAD_ID,
            now=NOW,
        )
        is None
    )
    assert jobs.enqueued == []
    assert audit.calls == []


@pytest.mark.asyncio
async def test_admission_skips_owners_who_disabled_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_platform_policy(monkeypatch, _policy())

    async def resolve(*_args, **_kwargs):
        return _project_context()

    monkeypatch.setattr(
        seal_service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    jobs = _Jobs()
    service = MemorySealAdmissionService(
        job_repository_builder=lambda _session: jobs,
        personalization_repository_builder=lambda _session: _Personalization(enabled=False),
    )

    assert (
        await service.admit_thread(
            _Session(),
            project_id=PROJECT_ID,
            owner_user_id=OWNER_USER_ID,
            thread_id=THREAD_ID,
            now=NOW,
        )
        is None
    )
    assert jobs.enqueued == []


@pytest.mark.asyncio
async def test_scheduler_isolates_per_thread_failures_and_counts_admissions() -> None:
    admitted_calls: list[str] = []

    class _Admission:
        async def list_due_threads(self, _session, *, now, max_jobs):
            assert now == NOW
            assert max_jobs == 20
            return (
                (PROJECT_ID, OWNER_USER_ID, "thread-a"),
                (PROJECT_ID, OWNER_USER_ID, "thread-b"),
                (PROJECT_ID, OWNER_USER_ID, "thread-c"),
            )

        async def admit_thread(self, _session, *, project_id, owner_user_id, thread_id, now):
            admitted_calls.append(thread_id)
            if thread_id == "thread-a":
                raise ProjectNotFound()
            if thread_id == "thread-b":
                return None
            return uuid.uuid4()

    scheduler = MemorySealSchedulerService(
        lambda: _Session(),
        admission=_Admission(),
    )

    assert await scheduler.admit_due(now=NOW) == 1
    assert admitted_calls == ["thread-a", "thread-b", "thread-c"]


def test_seal_service_constructors_reject_invalid_ports() -> None:
    with pytest.raises(ValueError, match="audit port is invalid"):
        MemorySealAdmissionService(audit=object())
    with pytest.raises(ValueError, match="configuration is invalid"):
        MemorySealSchedulerService(lambda: _Session(), max_jobs_per_poll=0)


# ---------------------------------------------------------------------------
# Worker handler: claim shape and authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_cancels_claims_with_the_wrong_shape() -> None:
    handler = _handler(_Barrier())

    for claim in (
        _claim(job_type="private_run", run_id="run-1"),
        _claim(namespace=None),
        _claim(run_id="run-1"),
        _claim(scope=JobScope(PROJECT_ID, None)),
    ):
        outcome = await handler(claim, _Authority())
        assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_handler_cancels_when_authorization_says_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _handler(_Barrier())

    async def authorize(_claim, _thread_id):
        return None

    monkeypatch.setattr(handler, "_authorize", authorize)
    outcome = await handler(_claim(), _Authority())
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_handler_fails_closed_when_authorization_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _handler(_Barrier())

    async def authorize(_claim, _thread_id):
        raise RuntimeError("database down")

    monkeypatch.setattr(handler, "_authorize", authorize)
    outcome = await handler(_claim(), _Authority())
    assert outcome.status == "failed"
    assert outcome.public_error_code == "MEMORY_SEAL_AUTHORITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_authorize_rechecks_platform_policy_owner_preference_and_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(session, user_id, project_identifier, request_id, *, lock):
        assert lock is False
        assert request_id == "memory-seal-worker"
        return _project_context()

    monkeypatch.setattr(
        seal_worker_module,
        "resolve_project_context_in_transaction",
        resolve,
    )

    async def materialize(session, section, *, for_update=False):
        return _policy(), 23

    monkeypatch.setattr(
        seal_worker_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )

    live_session = _Session(scalar_results=[True])
    handler = _handler(_Barrier(), session=live_session)
    context = await handler._authorize(_claim(), THREAD_ID)
    assert isinstance(context, PrivateWorkContext)
    assert context.project_id == PROJECT_ID

    gone_session = _Session(scalar_results=[None])
    handler = _handler(_Barrier(), session=gone_session)
    assert await handler._authorize(_claim(), THREAD_ID) is None

    async def materialize_disabled(session, section, *, for_update=False):
        return _policy(idle_seal_minutes=0), 23

    monkeypatch.setattr(
        seal_worker_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize_disabled),
    )
    handler = _handler(_Barrier(), session=_Session(scalar_results=[True]))
    assert await handler._authorize(_claim(), THREAD_ID) is None


# ---------------------------------------------------------------------------
# Worker handler: drain loop and settlement
# ---------------------------------------------------------------------------


async def _run_to_settlement(
    handler: MemorySealJobHandler,
    claim: JobClaim,
    monkeypatch: pytest.MonkeyPatch,
) -> JobSettlement:
    async def authorize(_claim, _thread_id):
        return _private_context()

    monkeypatch.setattr(handler, "_authorize", authorize)
    outcome = await handler(claim, _Authority())
    assert isinstance(outcome, JobSettlement)
    assert outcome.outcome.status == "succeeded"
    return outcome


@pytest.mark.asyncio
async def test_handler_drains_batches_then_seals_and_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(
        compact_results=[
            _compacted("cp-1"),
            _compacted("cp-2", removed=2),
            _drained(),
        ],
    )
    session = _Session()
    jobs = _Jobs()
    audit = _SettlementAudit()
    handler = _handler(barrier, session=session, jobs=jobs, audit=audit)
    claim = _claim()

    settlement = await _run_to_settlement(handler, claim, monkeypatch)
    await settlement.commit()

    assert [call["keep"] for call in barrier.compact_calls] == [("messages", 0)] * 3
    assert all(call["force"] is True for call in barrier.compact_calls)
    assert all(call["thread_id"] == THREAD_ID for call in barrier.compact_calls)
    assert len(barrier.verify_calls) == 1
    (update_statement,) = session.executed
    compiled = str(update_statement)
    assert "UPDATE threads_meta" in compiled
    assert "memory_sealed_at=:memory_sealed_at" in compiled
    # The stamp must not surface the thread as recently active.
    assert "updated_at=threads_meta.updated_at" in compiled
    assert jobs.settlements == [{"job_id": claim.job_id, "lease_token": "seal-lease"}]
    (audited,) = audit.calls
    assert audited["disposition"] == "sealed"
    assert audited["job_id"] == claim.job_id
    assert audited["request_id"] == "memory-seal-worker"


@pytest.mark.asyncio
async def test_handler_yields_noop_when_a_live_run_preempts_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(
        compact_results=[
            _compacted("cp-1"),
            PrivateWorkConflict("a live Run owns the thread"),
        ],
    )
    session = _Session()
    jobs = _Jobs()
    audit = _SettlementAudit()
    handler = _handler(barrier, session=session, jobs=jobs, audit=audit)
    claim = _claim()

    settlement = await _run_to_settlement(handler, claim, monkeypatch)
    await settlement.commit()

    assert barrier.verify_calls == []
    assert session.executed == []
    assert jobs.settlements == [{"job_id": claim.job_id, "lease_token": "seal-lease"}]
    (audited,) = audit.calls
    assert audited["disposition"] == "noop"


@pytest.mark.asyncio
async def test_handler_downgrades_to_noop_when_the_head_moves_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(compact_results=[_drained()], verify_ready=False)
    session = _Session()
    jobs = _Jobs()
    audit = _SettlementAudit()
    handler = _handler(barrier, session=session, jobs=jobs, audit=audit)

    settlement = await _run_to_settlement(handler, _claim(), monkeypatch)
    await settlement.commit()

    assert len(barrier.verify_calls) == 1
    assert session.executed == []
    assert len(jobs.settlements) == 1
    (audited,) = audit.calls
    assert audited["disposition"] == "noop"


@pytest.mark.asyncio
async def test_handler_fails_closed_on_drain_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(
        compact_results=[PrivateWorkUnavailable("memory-seal-test")],
    )
    handler = _handler(barrier)

    async def authorize(_claim, _thread_id):
        return _private_context()

    monkeypatch.setattr(handler, "_authorize", authorize)
    outcome = await handler(_claim(), _Authority())
    assert outcome.status == "failed"
    assert outcome.public_error_code == "MEMORY_SEAL_DRAIN_FAILED"


@pytest.mark.asyncio
async def test_handler_rejects_stalled_drain_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(
        compact_results=[_compacted("cp-1"), _compacted("cp-1")],
    )
    handler = _handler(barrier)

    async def authorize(_claim, _thread_id):
        return _private_context()

    monkeypatch.setattr(handler, "_authorize", authorize)
    outcome = await handler(_claim(), _Authority())
    assert outcome.status == "failed"
    assert outcome.public_error_code == "MEMORY_SEAL_PROGRESS_STALLED"


@pytest.mark.asyncio
async def test_settlement_raises_lease_lost_when_the_lease_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _Barrier(compact_results=[_drained()])
    jobs = _Jobs()
    jobs.settle_result = False
    handler = _handler(barrier, jobs=jobs)

    settlement = await _run_to_settlement(handler, _claim(), monkeypatch)
    with pytest.raises(LeaseLost):
        await settlement.commit()


def test_seal_worker_constructor_rejects_invalid_ports() -> None:
    with pytest.raises(ValueError, match="configuration is invalid"):
        MemorySealJobHandler(
            lambda: _Session(),
            app_config=None,
            barrier=object(),
        )
    with pytest.raises(ValueError, match="audit port is invalid"):
        MemorySealJobHandler(
            lambda: _Session(),
            app_config=None,
            barrier=_Barrier(),
            audit=object(),
        )


# ---------------------------------------------------------------------------
# Audit contracts
# ---------------------------------------------------------------------------


def test_memory_seal_audit_actions_bind_processes_and_metadata() -> None:
    admitted = AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_SEAL_ADMITTED]
    assert AuditAction.MEMORY_SEAL_ADMITTED.value == "memory.seal.admitted"
    assert admitted.target_kind is AuditTargetKind.JOB
    assert admitted.variants[0].actor == "process"
    assert admitted.variants[0].processes == frozenset({AuditProcess.SCHEDULER})

    settled = AUDIT_ACTION_CONTRACTS[AuditAction.MEMORY_SEAL_SETTLED]
    assert AuditAction.MEMORY_SEAL_SETTLED.value == "memory.seal.settled"
    assert settled.target_kind is AuditTargetKind.JOB
    assert settled.variants[0].actor == "process"
    assert settled.variants[0].processes == frozenset({AuditProcess.WORKER})

    metadata_model = AUDIT_METADATA_MODELS[AuditAction.MEMORY_SEAL_SETTLED]
    assert metadata_model.model_validate({"disposition": "sealed"})
    assert metadata_model.model_validate({"disposition": "noop"})
    with pytest.raises(ValueError):
        metadata_model.model_validate({"disposition": "forged"})
