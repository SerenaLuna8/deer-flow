from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr
from support.private_thread_seed import seed_private_thread_database

from app.automations.dispatcher import (
    AutomationDispatcher,
    _DispatchCoordinates,
)
from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work import _run_response
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_metadata import (
    RUN_HOST_EXECUTION_SUSPENSION_KEY,
    RUN_TOKEN_BUDGET_USAGE_KEY,
    RUN_VISION_DISPATCH_BUDGET_KEY,
    RunHostExecutionSuspension,
    RunVisionDispatchBudget,
    RunVisionDispatchBudgetInvalid,
    run_token_budget_usage,
    run_vision_dispatch_budget,
)
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunCreate,
    PrivateRunExecutionLeaseLost,
    PrivateRunRecord,
    PrivateRunRepository,
    PrivateRunUsageSnapshot,
)
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.models.runtime import ModelRuntime, ModelRuntimeProfile
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    JobClaim,
    JobOwnerRef,
    JobRepository,
    JobScope,
)
from deerflow.persistence.run.model import RunRow
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.token_budget_usage import TokenBudgetUsageSnapshot
from deerflow.vision.contracts import VisionUsageReceipt
from deerflow.vision.dispatch import (
    MAX_VISION_CALLS_PER_RUN,
    MAX_VISION_NORMALIZED_BYTES_PER_RUN,
    MAX_VISION_NORMALIZED_PIXELS_PER_RUN,
    VisionDispatchDenied,
)


@dataclass(frozen=True, slots=True)
class _ActiveRun:
    run_id: str
    thread_id: str
    job_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_id: uuid.UUID
    lease_token: str
    origin_trace_id: str


async def _seed_active_run(seed) -> _ActiveRun:
    thread_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    run_id = str(uuid.uuid4())
    origin_trace_id = uuid.uuid4().hex
    lease_token = f"vision-budget-{uuid.uuid4().hex}"
    lease_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=2)
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="vision-budget-test",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )
        run = RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=str(seed.owner_a.user_id),
            status="running",
            model_name="test-model",
            multitask_strategy="reject",
            metadata_json={"visible": "kept"},
            kwargs_json={"input": {"messages": []}},
            origin_trace_id=origin_trace_id,
            project_id=seed.owner_a.project_id,
            finalization_status="pending",
            execution_lease_token_hash=lease_hash,
            execution_lease_expires_at=expires_at,
            execution_heartbeat_at=now,
            execution_started_at=now,
        )
        session.add(run)
        await session.flush()
        job = JobRow(
            job_type="private_run",
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=run_id,
            origin_trace_id=origin_trace_id,
            idempotency_key=hashlib.sha256(f"job:{run_id}".encode()).hexdigest(),
            status="running",
            max_attempts=3,
            attempt_count=1,
            lease_owner_id=worker_id,
            lease_token_hash=lease_hash,
            lease_expires_at=expires_at,
            heartbeat_at=now,
            retry_safety="safe",
            started_at=now,
        )
        session.add(job)
        await session.flush()
        run.job_id = job.id
        attempt = JobAttemptRow(
            job_id=job.id,
            attempt_number=1,
            worker_id=worker_id,
            lease_token_hash=lease_hash,
            started_at=now,
            heartbeat_at=now,
        )
        session.add(attempt)
        await session.flush()
        return _ActiveRun(
            run_id=run_id,
            thread_id=thread_id,
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            lease_token=lease_token,
            origin_trace_id=origin_trace_id,
        )


def _claim(seed, active: _ActiveRun) -> JobClaim:
    return JobClaim(
        job_id=active.job_id,
        attempt_id=active.attempt_id,
        lease_token=active.lease_token,
        job_type="private_run",
        scope=JobScope(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
        ),
        run_id=active.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=active.origin_trace_id,
    )


def _boundary(seed, active: _ActiveRun) -> PrivateRunExecutionBoundary:
    return PrivateRunExecutionBoundary(
        seed.factory,
        context=seed.owner_a,
        claim=_claim(seed, active),
    )


def _vision_model() -> ModelConfig:
    model = ModelConfig(
        name="vision-budget-model",
        display_name="Vision budget model",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="small-vlm",
        max_input_tokens=64_000,
        base_url="https://vision.example.test/v1",
        api_key=SecretStr("test-secret"),
        supports_vision=True,
    )
    model._system_model_config_id = uuid.UUID(
        "00000000-0000-0000-0000-000000000201",
    )
    model._system_provider_adapter = "openai"
    return model


def _job_owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(
        key_id="vision-budget-test",
        hmac_hex="f" * 64,
    )


async def _rotate_attempt_authority_for_persistence_test(
    seed,
    active: _ActiveRun,
) -> _ActiveRun:
    """Install a second attempt without claiming it through production.

    A committed Vision reserve marks the Job retry-unsafe, so production
    ``claim_next`` must refuse automatic takeover. This helper exists only to
    prove that the Run-owned aggregate itself survives an explicit lease and
    attempt authority rotation.
    """

    changed_at = datetime.now(UTC)
    expires_at = changed_at + timedelta(minutes=2)
    lease_token = f"vision-budget-rotated-{uuid.uuid4().hex}"
    lease_hash = hashlib.sha256(lease_token.encode()).hexdigest()
    attempt_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        job = await session.get(JobRow, active.job_id, with_for_update=True)
        run = await session.get(RunRow, active.run_id, with_for_update=True)
        previous_attempt = await session.get(
            JobAttemptRow,
            active.attempt_id,
            with_for_update=True,
        )
        assert job is not None and run is not None and previous_attempt is not None
        assert job.retry_safety == "unknown"
        previous_attempt.finished_at = changed_at
        previous_attempt.outcome = "lease_lost"
        previous_attempt.public_error_code = "LEASE_EXPIRED"
        job.attempt_count += 1
        job.status = "running"
        job.lease_owner_id = active.worker_id
        job.lease_token_hash = lease_hash
        job.lease_expires_at = expires_at
        job.heartbeat_at = changed_at
        job.updated_at = changed_at
        run.status = "running"
        run.execution_lease_token_hash = lease_hash
        run.execution_lease_expires_at = expires_at
        run.execution_heartbeat_at = changed_at
        session.add(
            JobAttemptRow(
                id=attempt_id,
                job_id=job.id,
                attempt_number=job.attempt_count,
                worker_id=active.worker_id,
                lease_token_hash=lease_hash,
                started_at=changed_at,
                heartbeat_at=changed_at,
            )
        )
    return replace(
        active,
        attempt_id=attempt_id,
        lease_token=lease_token,
    )


def _record(
    *,
    run_id: str = "run-1",
    thread_id: str = "thread-1",
    metadata: dict[str, object] | None = None,
) -> PrivateRunRecord:
    now = datetime.now(UTC)
    return PrivateRunRecord(
        run_id=run_id,
        thread_id=thread_id,
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata=metadata or {},
        kwargs={"input": {"messages": []}},
        origin_trace_id=uuid.uuid4().hex,
        error=None,
        model_name="test-model",
        created_at=now,
        updated_at=now,
    )


def _token_budget_usage(
    run_id: str,
    input_tokens: int,
    output_tokens: int,
) -> TokenBudgetUsageSnapshot:
    return TokenBudgetUsageSnapshot(
        run_id=run_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def test_vision_dispatch_budget_metadata_is_strict_and_client_cannot_forge_it() -> None:
    assert run_vision_dispatch_budget({}) == RunVisionDispatchBudget()
    for malformed in (
        None,
        {},
        {
            "schema_version": "vision.dispatch.budget.v1",
            "call_count": True,
            "normalized_bytes": 1,
            "normalized_pixels": 1,
        },
        {
            "schema_version": "vision.dispatch.budget.v1",
            "call_count": 1,
            "normalized_bytes": 1,
            "normalized_pixels": 1,
            "extra": 1,
        },
    ):
        with pytest.raises(RunVisionDispatchBudgetInvalid):
            run_vision_dispatch_budget({RUN_VISION_DISPATCH_BUDGET_KEY: malformed})

    body = PrivateRunCreateRequest.model_validate(
        {
            "metadata": {
                RUN_VISION_DISPATCH_BUDGET_KEY: {
                    "schema_version": "vision.dispatch.budget.v1",
                    "call_count": MAX_VISION_CALLS_PER_RUN,
                    "normalized_bytes": 1,
                    "normalized_pixels": 1,
                },
                "nested": {"__server_only": "forged", "visible": True},
            }
        }
    )
    assert body.metadata == {"nested": {"visible": True}}


def test_server_run_metadata_is_hidden_and_ignored_by_idempotency_and_automation() -> None:
    internal = {
        "schema_version": "vision.dispatch.budget.v1",
        "call_count": 1,
        "normalized_bytes": 10,
        "normalized_pixels": 20,
    }
    suspension = RunHostExecutionSuspension(
        approval_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        source_job_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        producing_attempt_id=uuid.UUID(
            "33333333-3333-4333-8333-333333333333",
        ),
    ).as_dict()
    record = _record(
        metadata={
            "visible": "kept",
            RUN_VISION_DISPATCH_BUDGET_KEY: internal,
            RUN_HOST_EXECUTION_SUSPENSION_KEY: suspension,
            "nested": {"shown": 1, "__hidden": 2},
        }
    )
    response = _run_response(record)
    assert response.metadata == {
        "visible": "kept",
        "nested": {"shown": 1},
    }
    assert PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=record.thread_id,
        request=PrivateRunCreate(
            run_id=record.run_id,
            metadata={"visible": "kept", "nested": {"shown": 1}},
            kwargs={"input": {"messages": []}},
        ),
    )

    coordinates = _DispatchCoordinates(
        occurrence_id="occurrence-1",
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        task_id="task-1",
        trigger="scheduled",
        context_mode="fresh_thread_per_run",
        reuse_thread_id=None,
    )
    automation_record = _record(
        run_id=coordinates.expected_run_id,
        thread_id=coordinates.expected_thread_id,
        metadata={
            **coordinates.run_metadata,
            RUN_VISION_DISPATCH_BUDGET_KEY: internal,
            RUN_HOST_EXECUTION_SUSPENSION_KEY: suspension,
        },
    )
    AutomationDispatcher._require_matching_run(coordinates, automation_record)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_budget_survives_explicit_attempt_authority_rotation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        first = _boundary(seed, active)
        await first.before_vision_dispatch(
            normalized_bytes=11,
            normalized_pixels=101,
        )
        assert first.ambiguous_side_effect is True

        rotated = await _rotate_attempt_authority_for_persistence_test(
            seed,
            active,
        )
        second = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=replace(
                _claim(seed, rotated),
                retry_safety="unknown",
            ),
        )
        await second.before_vision_dispatch(
            normalized_bytes=13,
            normalized_pixels=103,
        )

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=2,
                    normalized_bytes=24,
                    normalized_pixels=204,
                )
            )
            assert job.retry_safety == "unknown"
            assert job.attempt_count == 2
            current_attempt = await session.get(
                JobAttemptRow,
                rotated.attempt_id,
            )
            assert current_attempt is not None
            assert current_attempt.outcome is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_retry_unsafe_expired_job_refuses_automatic_takeover(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        await _boundary(seed, active).before_vision_dispatch(
            normalized_bytes=17,
            normalized_pixels=19,
        )
        claimed_at = datetime.now(UTC)
        expired_at = claimed_at - timedelta(seconds=1)
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            run = await session.get(RunRow, active.run_id, with_for_update=True)
            assert job is not None and run is not None
            assert job.retry_safety == "unknown"
            job.lease_expires_at = expired_at
            run.execution_lease_expires_at = expired_at

        async with seed.factory() as session, session.begin():
            takeover = await JobRepository(
                session,
                owner_ref_hasher=_job_owner_ref,
            ).claim_next(
                worker_id=active.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=claimed_at,
            )
            assert takeover is None

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            attempt = await session.get(JobAttemptRow, active.attempt_id)
            assert run is not None and job is not None and attempt is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=1,
                    normalized_bytes=17,
                    normalized_pixels=19,
                )
            )
            assert job.status == "dead"
            assert job.retry_safety == "unknown"
            assert job.attempt_count == 1
            assert attempt.outcome == "dead"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_token_budget_settlement_is_absolute_and_lease_gated(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        settled_at = datetime.now(UTC)
        prior = _token_budget_usage(active.run_id, 450, 450)
        async with seed.factory() as session, session.begin():
            settlement = await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a.resource_scope,
                run_id=active.run_id,
                job_id=active.job_id,
                lease_token=active.lease_token,
                outcome="failed",
                public_error_code="LLM_PROVIDER_UNAVAILABLE",
                retryable_failure=True,
                attempt_usage=PrivateRunUsageSnapshot(
                    token_budget_usage=prior,
                ),
                now=settled_at,
            )
            assert settlement.run.status == "pending"

        stale = _token_budget_usage(active.run_id, 600, 600)
        with pytest.raises(PrivateRunExecutionLeaseLost):
            async with seed.factory() as session, session.begin():
                await PrivateRunRepository(session).settle_execution(
                    scope=seed.owner_a.resource_scope,
                    run_id=active.run_id,
                    job_id=active.job_id,
                    lease_token=active.lease_token,
                    outcome="failed",
                    public_error_code="LLM_PROVIDER_UNAVAILABLE",
                    attempt_usage=PrivateRunUsageSnapshot(
                        token_budget_usage=stale,
                    ),
                    now=settled_at + timedelta(seconds=1),
                )

        claimed_at = settled_at + timedelta(seconds=5)
        async with seed.factory() as session, session.begin():
            claim = await JobRepository(
                session,
                owner_ref_hasher=_job_owner_ref,
            ).claim_next(
                worker_id=active.worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=60,
                now=claimed_at,
            )
            assert claim is not None
            assert await JobRepository(session).mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=claimed_at,
            )
            state = await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a.resource_scope,
                run_id=active.run_id,
                job_id=active.job_id,
                lease_token=claim.lease_token,
                now=claimed_at,
            )
            assert (
                run_token_budget_usage(
                    state.run.metadata,
                    run_id=active.run_id,
                )
                == prior
            )

        with pytest.raises(PrivateRunConflict):
            async with seed.factory() as session, session.begin():
                await PrivateRunRepository(session).settle_execution(
                    scope=seed.owner_a.resource_scope,
                    run_id=active.run_id,
                    job_id=active.job_id,
                    lease_token=claim.lease_token,
                    outcome="succeeded",
                    attempt_usage=PrivateRunUsageSnapshot(
                        token_budget_usage=_token_budget_usage(
                            active.run_id,
                            400,
                            400,
                        ),
                    ),
                    now=claimed_at + timedelta(seconds=1),
                )

        current = _token_budget_usage(active.run_id, 550, 550)
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a.resource_scope,
                run_id=active.run_id,
                job_id=active.job_id,
                lease_token=claim.lease_token,
                outcome="succeeded",
                attempt_usage=PrivateRunUsageSnapshot(
                    token_budget_usage=current,
                ),
                now=claimed_at + timedelta(seconds=2),
            )

        # A duplicate terminal ACK cannot use its stale lease to advance usage.
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a.resource_scope,
                run_id=active.run_id,
                job_id=active.job_id,
                lease_token=claim.lease_token,
                outcome="succeeded",
                attempt_usage=PrivateRunUsageSnapshot(
                    token_budget_usage=_token_budget_usage(
                        active.run_id,
                        700,
                        700,
                    ),
                ),
                now=claimed_at + timedelta(seconds=3),
            )

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            assert run is not None
            assert (
                run_token_budget_usage(
                    run.metadata_json,
                    run_id=active.run_id,
                )
                == current
            )
            assert RUN_TOKEN_BUDGET_USAGE_KEY in run.metadata_json
            # Public tracking stayed disabled: the private enforcement channel
            # remains independent from the public Run usage aggregates.
            assert run.total_input_tokens == 0
            assert run.total_output_tokens == 0
            assert run.total_tokens == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_concurrent_reserve_never_crosses_call_limit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        async with seed.factory() as session, session.begin():
            run = await session.get(RunRow, active.run_id, with_for_update=True)
            assert run is not None
            run.metadata_json = {
                "visible": "kept",
                RUN_VISION_DISPATCH_BUDGET_KEY: RunVisionDispatchBudget(
                    call_count=MAX_VISION_CALLS_PER_RUN - 1,
                    normalized_bytes=MAX_VISION_CALLS_PER_RUN - 1,
                    normalized_pixels=MAX_VISION_CALLS_PER_RUN - 1,
                ).as_dict(),
            }

        boundaries = (_boundary(seed, active), _boundary(seed, active))

        async def reserve(boundary: PrivateRunExecutionBoundary):
            try:
                await boundary.before_vision_dispatch(
                    normalized_bytes=1,
                    normalized_pixels=1,
                )
            except Exception as error:  # exact result asserted below
                return error
            return None

        results = await asyncio.gather(*(reserve(item) for item in boundaries))
        assert sum(result is None for result in results) == 1
        denied = next(result for result in results if result is not None)
        assert isinstance(denied, VisionDispatchDenied)
        assert denied.code == "VISION_RATE_LIMITED"
        denied_boundary = boundaries[results.index(denied)]
        assert denied_boundary.lease_lost is False
        assert denied_boundary.cancel_requested is False
        assert denied_boundary.ambiguous_side_effect is False

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            assert run is not None
            budget = run_vision_dispatch_budget(run.metadata_json)
            assert budget.call_count == MAX_VISION_CALLS_PER_RUN
            assert budget.normalized_bytes == MAX_VISION_CALLS_PER_RUN
            assert budget.normalized_pixels == MAX_VISION_CALLS_PER_RUN
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize(
    "initial_budget",
    (
        RunVisionDispatchBudget(
            call_count=MAX_VISION_CALLS_PER_RUN - 1,
        ),
        RunVisionDispatchBudget(
            normalized_bytes=MAX_VISION_NORMALIZED_BYTES_PER_RUN - 1,
        ),
        RunVisionDispatchBudget(
            normalized_pixels=MAX_VISION_NORMALIZED_PIXELS_PER_RUN - 1,
        ),
    ),
    ids=("calls", "normalized-bytes", "normalized-pixels"),
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_all_budget_dimensions_allow_exact_limit_then_deny_one_over(
    migrated_postgres_database_url: str,
    initial_budget: RunVisionDispatchBudget,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        async with seed.factory() as session, session.begin():
            run = await session.get(RunRow, active.run_id, with_for_update=True)
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            assert run is not None and job is not None
            run.metadata_json = {
                "visible": "kept",
                RUN_VISION_DISPATCH_BUDGET_KEY: initial_budget.as_dict(),
            }
            # A nonzero durable aggregate can only follow a dispatch fence.
            job.retry_safety = "unknown"

        exact = _boundary(seed, active)
        await exact.before_vision_dispatch(
            normalized_bytes=1,
            normalized_pixels=1,
        )
        assert exact.ambiguous_side_effect is True

        denied = _boundary(seed, active)
        with pytest.raises(VisionDispatchDenied) as caught:
            await denied.before_vision_dispatch(
                normalized_bytes=1,
                normalized_pixels=1,
            )
        assert caught.value.code == "VISION_RATE_LIMITED"
        assert denied.lease_lost is False
        assert denied.ambiguous_side_effect is False

        expected = RunVisionDispatchBudget(
            call_count=initial_budget.call_count + 1,
            normalized_bytes=initial_budget.normalized_bytes + 1,
            normalized_pixels=initial_budget.normalized_pixels + 1,
        )
        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == expected
            assert job.retry_safety == "unknown"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_rate_limit_and_lease_denial_do_not_reserve(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        limited = _boundary(seed, active)
        with pytest.raises(VisionDispatchDenied) as caught:
            await limited.before_vision_dispatch(
                normalized_bytes=MAX_VISION_NORMALIZED_BYTES_PER_RUN + 1,
                normalized_pixels=1,
            )
        assert caught.value.code == "VISION_RATE_LIMITED"
        assert limited.lease_lost is False
        assert limited.ambiguous_side_effect is False

        wrong_claim = replace(
            _claim(seed, active),
            lease_token="wrong-lease",
        )
        lost = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=wrong_claim,
        )
        with pytest.raises(AuthorizationRevoked):
            await lost.before_vision_dispatch(
                normalized_bytes=1,
                normalized_pixels=1,
            )
        assert lost.lease_lost is True

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (RunVisionDispatchBudget())
            assert job.retry_safety == "safe"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_committed_reserve_survives_inflight_provider_cancellation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    task: asyncio.Task[object] | None = None
    try:
        active = await _seed_active_run(seed)
        model = _vision_model()
        authority = PrivateRunVisionDispatchAuthority(
            boundary=_boundary(seed, active),
        )
        provider_started = asyncio.Event()

        class BlockingChatModel:
            async def ainvoke(
                self,
                _messages: object,
                *,
                config: object,
            ) -> AIMessage:
                del config
                provider_started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

        runtime = ModelRuntime(
            app_config=AppConfig(
                models=[model],
                sandbox=SandboxConfig(
                    use="deerflow.sandbox.local:LocalSandboxProvider",
                ),
            ),
            model_factory=lambda **_kwargs: BlockingChatModel(),
        )

        async def invoke() -> None:
            attempt = await authority.before_attempt(
                normalized_bytes=5,
                normalized_pixels=23,
            )
            try:
                await runtime.ainvoke(
                    [HumanMessage(content="image request")],
                    profile=ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
                    model_name=model.name,
                )
            finally:
                await asyncio.shield(
                    authority.after_attempt(
                        attempt=attempt,
                        usage_receipt=VisionUsageReceipt(
                            call_count=1,
                            request_dispatched=True,
                            usage_unknown=True,
                        ),
                        error_code="VISION_AUTH_FAILED",
                    )
                )

        task = asyncio.create_task(invoke())
        await asyncio.wait_for(provider_started.wait(), timeout=2)

        # Reaching the provider proves before_attempt returned. Read through a
        # new session to prove the reserve committed before HTTP dispatch.
        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=1,
                    normalized_bytes=5,
                    normalized_pixels=23,
                )
            )
            assert job.retry_safety == "unknown"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=1,
                    normalized_bytes=5,
                    normalized_pixels=23,
                )
            )
            assert job.retry_safety == "unknown"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_concurrent_reserve_and_cancel_complete_without_deadlock(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        boundary = _boundary(seed, active)

        async def reserve() -> bool:
            try:
                await boundary.before_vision_dispatch(
                    normalized_bytes=29,
                    normalized_pixels=31,
                )
            except AuthorizationRevoked:
                return False
            return True

        async def cancel() -> str:
            async with seed.factory() as session, session.begin():
                return await PrivateRunRepository(session).request_cancel(
                    scope=seed.owner_a.resource_scope,
                    thread_id=active.thread_id,
                    run_id=active.run_id,
                    job_id=active.job_id,
                    reason="vision_budget_race",
                )

        reserved, cancel_result = await asyncio.wait_for(
            asyncio.gather(reserve(), cancel()),
            timeout=5,
        )
        assert cancel_result == "requested"

        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run.cancel_requested_at is not None
            assert job.cancel_requested_at is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=int(reserved),
                    normalized_bytes=29 if reserved else 0,
                    normalized_pixels=31 if reserved else 0,
                )
            )
            assert job.retry_safety == ("unknown" if reserved else "safe")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_cancel_and_malformed_state_never_reset_prior_reserve(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        active = await _seed_active_run(seed)
        await _boundary(seed, active).before_vision_dispatch(
            normalized_bytes=5,
            normalized_pixels=7,
        )
        async with seed.factory() as session, session.begin():
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            assert job is not None
            job.cancel_requested_at = datetime.now(UTC)

        cancelled = _boundary(seed, active)
        with pytest.raises(AuthorizationRevoked):
            await cancelled.before_vision_dispatch(
                normalized_bytes=11,
                normalized_pixels=13,
            )
        assert cancelled.cancel_requested is True

        async with seed.factory() as session, session.begin():
            run = await session.get(RunRow, active.run_id, with_for_update=True)
            job = await session.get(JobRow, active.job_id, with_for_update=True)
            assert run is not None and job is not None
            assert run_vision_dispatch_budget(run.metadata_json) == (
                RunVisionDispatchBudget(
                    call_count=1,
                    normalized_bytes=5,
                    normalized_pixels=7,
                )
            )
            job.cancel_requested_at = None
            run.metadata_json = {
                "visible": "kept",
                RUN_VISION_DISPATCH_BUDGET_KEY: {
                    "schema_version": "vision.dispatch.budget.v1",
                    "call_count": "1",
                    "normalized_bytes": 5,
                    "normalized_pixels": 7,
                },
            }
            job.retry_safety = "safe"

        malformed = _boundary(seed, active)
        with pytest.raises(AuthorizationRevoked):
            await malformed.before_vision_dispatch(
                normalized_bytes=1,
                normalized_pixels=1,
            )
        assert malformed.lease_lost is True
        async with seed.factory() as session:
            run = await session.get(RunRow, active.run_id)
            job = await session.get(JobRow, active.job_id)
            assert run is not None and job is not None
            assert run.metadata_json[RUN_VISION_DISPATCH_BUDGET_KEY]["call_count"] == "1"
            assert job.retry_safety == "safe"
    finally:
        await seed.engine.dispose()
