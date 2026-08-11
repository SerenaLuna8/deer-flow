from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.workflows.contracts import WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER
from app.workflows.http_effect_store import (
    PostgresWorkflowHttpEffectStore,
    WorkflowHttpDispatchFailure,
    WorkflowHttpEffectAlreadyDispatching,
    WorkflowHttpEffectConflict,
    WorkflowHttpEffectExecutor,
    WorkflowHttpExecutionAuthorityLost,
    WorkflowHttpPreDispatchAuthorityDenied,
    WorkflowHttpSafeFailure,
    WorkflowHttpSideEffectUnknown,
)
from app.workflows.http_effects import (
    WorkflowHttpEffectIdentityV1,
    WorkflowHttpJobExecutionFence,
    derive_workflow_http_idempotency_key,
    derive_workflow_http_operation_key,
    derive_workflow_http_request_fingerprint,
)

pytestmark = pytest.mark.postgres

RUN_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
VERSION_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")
NODE_ID = uuid.UUID("20000000-0000-4000-8000-000000000003")
PROJECT_ID = uuid.UUID("20000000-0000-4000-8000-000000000004")
JOB_ID = uuid.UUID("20000000-0000-4000-8000-000000000005")
WORKER_A = uuid.UUID("20000000-0000-4000-8000-000000000006")
WORKER_B = uuid.UUID("20000000-0000-4000-8000-000000000007")
OWNER_USER_ID = "20000000-0000-4000-8000-000000000008"
ORIGIN_TRACE_ID = "workflow-http-effect-test-trace"
LEASE_A = "lease-a-raw-token-32-bytes-minimum-value"
LEASE_B = "lease-b-raw-token-32-bytes-minimum-value"
EFFECT_HMAC_KEY = b"workflow-http-effect-test-key!!" * 2
EFFECT_DDL = Path(__file__).parent / "fixtures" / "workflow_node_effects_g04.sql"

_AUTHORITY_DDL = """
CREATE TABLE workflow_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    workflow_version_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL,
    execution_epoch BIGINT NOT NULL,
    current_job_id UUID,
    origin_trace_id VARCHAR(512) NOT NULL,
    UNIQUE (id, project_id, owner_user_id),
    UNIQUE (id, project_id, owner_user_id, origin_trace_id)
);
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    job_type VARCHAR(32) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    workflow_run_id UUID NOT NULL,
    workflow_epoch BIGINT NOT NULL,
    origin_trace_id VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count BIGINT NOT NULL,
    lease_owner_id UUID,
    lease_token_hash CHAR(64),
    lease_expires_at TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    UNIQUE (id, project_id, owner_user_id, workflow_run_id, workflow_epoch)
);
CREATE TABLE workflow_run_jobs (
    workflow_run_id UUID NOT NULL,
    execution_epoch BIGINT NOT NULL,
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    PRIMARY KEY (workflow_run_id, execution_epoch),
    UNIQUE (job_id)
);
"""


@pytest_asyncio.fixture()
async def effect_engine(postgres_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(_AUTHORITY_DDL)
            await connection.execute(
                text(
                    """INSERT INTO workflow_runs
                       (id,project_id,owner_user_id,workflow_version_id,status,
                        execution_epoch,origin_trace_id)
                       VALUES (:run,:project,:owner,:version,'running',1,:trace)"""
                ),
                {
                    "run": RUN_ID,
                    "project": PROJECT_ID,
                    "owner": OWNER_USER_ID,
                    "version": VERSION_ID,
                    "trace": ORIGIN_TRACE_ID,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO jobs
                       (id,job_type,project_id,owner_user_id,workflow_run_id,
                        workflow_epoch,origin_trace_id,status,attempt_count,
                        lease_owner_id,lease_token_hash,lease_expires_at)
                       VALUES (:job,'workflow_run',:project,:owner,:run,1,:trace,
                               'running',1,:worker,:lease_hash,
                               now() + interval '10 minutes')"""
                ),
                {
                    "job": JOB_ID,
                    "project": PROJECT_ID,
                    "owner": OWNER_USER_ID,
                    "run": RUN_ID,
                    "trace": ORIGIN_TRACE_ID,
                    "worker": WORKER_A,
                    "lease_hash": hashlib.sha256(LEASE_A.encode()).hexdigest(),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO workflow_run_jobs
                       (workflow_run_id,execution_epoch,job_id,project_id,owner_user_id)
                       VALUES (:run,1,:job,:project,:owner)"""
                ),
                {
                    "run": RUN_ID,
                    "job": JOB_ID,
                    "project": PROJECT_ID,
                    "owner": OWNER_USER_ID,
                },
            )
            await connection.execute(
                text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
                {"job": JOB_ID, "run": RUN_ID},
            )
            await raw_connection.driver_connection.execute(EFFECT_DDL.read_text(encoding="utf-8"))
        yield engine
    finally:
        await engine.dispose()


def _fence(
    *,
    worker_id: uuid.UUID = WORKER_A,
    attempt: int = 1,
    lease_token: str = LEASE_A,
) -> WorkflowHttpJobExecutionFence:
    return WorkflowHttpJobExecutionFence(
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        owner_user_id=OWNER_USER_ID,
        origin_trace_id=ORIGIN_TRACE_ID,
        job_id=JOB_ID,
        execution_epoch=1,
        attempt=attempt,
        worker_id=worker_id,
        lease_token=lease_token,
    )


async def _take_over(effect_engine: AsyncEngine) -> WorkflowHttpJobExecutionFence:
    async with effect_engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE jobs
                      SET attempt_count=2, lease_owner_id=:worker,
                          lease_token_hash=:lease_hash,
                          lease_expires_at=now() + interval '10 minutes'
                    WHERE id=:job"""
            ),
            {
                "job": JOB_ID,
                "worker": WORKER_B,
                "lease_hash": hashlib.sha256(LEASE_B.encode()).hexdigest(),
            },
        )
    return _fence(worker_id=WORKER_B, attempt=2, lease_token=LEASE_B)


def _identity(*, activation_key: str, effect_tail: int) -> WorkflowHttpEffectIdentityV1:
    request_fingerprint = derive_workflow_http_request_fingerprint(
        hmac_key=EFFECT_HMAC_KEY,
        canonical_request_material=f"POST:{activation_key}".encode(),
    )
    operation_key = derive_workflow_http_operation_key(
        hmac_key=EFFECT_HMAC_KEY,
        run_id=RUN_ID,
        node_id=NODE_ID,
        activation_key=activation_key,
        request_fingerprint=request_fingerprint,
    )
    payload = {
        "schema_version": 1,
        "effect_id": f"20000000-0000-4000-8000-{effect_tail:012d}",
        "run_id": str(RUN_ID),
        "workflow_version_id": str(VERSION_ID),
        "node_id": str(NODE_ID),
        "activation_key": activation_key,
        "operation_key": operation_key,
        "method": "POST",
        "request_fingerprint": request_fingerprint,
        "idempotency_key": derive_workflow_http_idempotency_key(
            hmac_key=EFFECT_HMAC_KEY,
            operation_key=operation_key,
        ),
    }
    return WorkflowHttpEffectIdentityV1.model_validate_json(json.dumps(payload))


def _outcome(kind: str):
    if kind == "response_invalid":
        payload = {
            "kind": "response_invalid",
            "status_code": 200,
            "duration_ms": 6,
            "wire_byte_count": {"value": 32, "relation": "exact"},
            "decoded_byte_count": {"value": 32, "relation": "exact"},
            "error": {
                "code": "WORKFLOW_HTTP_RESPONSE_INVALID",
                "safe_message": "Response did not match the declared schema.",
            },
        }
    else:
        status = {"success": 200, "http_error_4xx": 404, "http_error_5xx": 503}[kind]
        payload = {
            "kind": "success" if kind == "success" else "http_error",
            "response": {
                "status_code": status,
                "headers": [],
                "body": {"kind": "empty"},
                "duration_ms": 6,
                "wire_byte_count": {"value": 0, "relation": "exact"},
                "decoded_byte_count": {"value": 0, "relation": "exact"},
                "retained_body_byte_count": 0,
            },
        }
    return WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(json.dumps(payload))


async def _authorize() -> bool:
    return True


@pytest.mark.parametrize("flag", [0, 1, "false", None])
def test_dispatch_origin_reachability_flag_requires_a_real_boolean(flag: object) -> None:
    with pytest.raises(ValueError, match="real boolean"):
        WorkflowHttpDispatchFailure(
            "WORKFLOW_HTTP_TRANSPORT_ERROR",
            may_have_reached_origin=flag,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("safe_error_code", ["", "lowercase", "HAS-DASH", 1])
def test_dispatch_failure_requires_a_stable_safe_error_code(
    safe_error_code: object,
) -> None:
    with pytest.raises(ValueError, match="safe error code"):
        WorkflowHttpDispatchFailure(
            safe_error_code,  # type: ignore[arg-type]
            may_have_reached_origin=True,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "effect_tail"),
    [
        ("success", 11),
        ("http_error_4xx", 12),
        ("http_error_5xx", 13),
        ("response_invalid", 14),
    ],
)
async def test_settled_response_recovers_after_checkpoint_fault_without_redispatch(
    effect_engine: AsyncEngine,
    kind: str,
    effect_tail: int,
) -> None:
    executor = WorkflowHttpEffectExecutor(PostgresWorkflowHttpEffectStore(effect_engine))
    identity = _identity(activation_key=f"write-{effect_tail}", effect_tail=effect_tail)
    calls: list[str | None] = []

    async def dispatch(idempotency_key: str | None):
        calls.append(idempotency_key)
        return _outcome(kind)

    async def crash_after_settle() -> None:
        raise RuntimeError("checkpoint fault")

    with pytest.raises(RuntimeError, match="checkpoint fault"):
        await executor.execute(
            identity,
            fence=_fence(),
            authorize_dispatch=_authorize,
            dispatch=dispatch,
            after_settle_before_checkpoint=crash_after_settle,
        )

    current_fence = await _take_over(effect_engine)

    async def must_not_dispatch(_idempotency_key: str | None):
        raise AssertionError("a settled HTTP outcome must never be sent again")

    recovered = await executor.execute(
        identity.model_copy(update={"effect_id": uuid.uuid4()}),
        fence=current_fence,
        authorize_dispatch=_authorize,
        dispatch=must_not_dispatch,
    )
    assert recovered.kind == ("http_error" if kind.startswith("http_error") else kind)
    assert len(calls) == 1


@pytest.mark.anyio
async def test_concurrent_duplicate_execution_lanes_dispatch_once(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    first = _identity(activation_key="concurrent-write", effect_tail=50)
    second = first.model_copy(update={"effect_id": uuid.uuid4()})
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    calls = 0

    async def first_dispatch(_idempotency_key: str | None):
        nonlocal calls
        calls += 1
        dispatch_started.set()
        await release_dispatch.wait()
        return _outcome("success")

    async def must_not_dispatch(_idempotency_key: str | None):
        raise AssertionError("the second lane must not dispatch the operation")

    async def first_lane():
        return await executor.execute(
            first,
            fence=_fence(),
            authorize_dispatch=_authorize,
            dispatch=first_dispatch,
        )

    async def second_lane():
        await dispatch_started.wait()
        try:
            await executor.execute(
                second,
                fence=_fence(),
                authorize_dispatch=_authorize,
                dispatch=must_not_dispatch,
            )
        except WorkflowHttpEffectAlreadyDispatching:
            release_dispatch.set()
            return "busy"
        raise AssertionError("the concurrent lane must observe dispatching")

    settled, competing = await asyncio.gather(first_lane(), second_lane())
    assert settled.kind == "success"
    assert competing == "busy"
    assert (
        await executor.execute(
            second,
            fence=_fence(),
            authorize_dispatch=_authorize,
            dispatch=must_not_dispatch,
        )
    ).kind == "success"
    assert calls == 1
    async with effect_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM workflow_node_effects")) == 1


@pytest.mark.anyio
async def test_two_workers_takeover_race_dispatches_once_and_old_worker_cannot_settle(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    first = _identity(activation_key="takeover-write", effect_tail=51)
    second = first.model_copy(update={"effect_id": uuid.uuid4()})
    dispatch_started = asyncio.Event()
    release_response = asyncio.Event()
    calls = 0
    current_fence: WorkflowHttpJobExecutionFence | None = None

    async def real_dispatch(_idempotency_key: str | None):
        nonlocal calls
        calls += 1
        dispatch_started.set()
        await release_response.wait()
        return _outcome("success")

    async def no_second_dispatch(_idempotency_key: str | None):
        raise AssertionError("takeover must not redispatch an uncertain write")

    async def old_worker():
        with pytest.raises(WorkflowHttpExecutionAuthorityLost):
            await executor.execute(
                first,
                fence=_fence(),
                authorize_dispatch=_authorize,
                dispatch=real_dispatch,
            )
        return "lost"

    async def new_worker():
        nonlocal current_fence
        await dispatch_started.wait()
        current_fence = await _take_over(effect_engine)
        with pytest.raises(WorkflowHttpEffectAlreadyDispatching):
            await executor.execute(
                second,
                fence=current_fence,
                authorize_dispatch=_authorize,
                dispatch=no_second_dispatch,
            )
        release_response.set()
        return "busy"

    assert await asyncio.gather(old_worker(), new_worker()) == ["lost", "busy"]
    assert current_fence is not None
    record = await store.recover_abandoned_dispatch(
        first.effect_id,
        recovery_fence=current_fence,
    )
    assert record.state == "unknown"
    assert calls == 1
    async with effect_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM workflow_node_effects")) == 1


@pytest.mark.anyio
async def test_stale_worker_cannot_settle_fail_unknown_or_recover_after_takeover(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    identity = _identity(activation_key="stale-write", effect_tail=60)
    prepared = await store.prepare(identity, fence=_fence())
    await store.claim_for_dispatch(prepared.identity.effect_id, fence=_fence())
    current_fence = await _take_over(effect_engine)

    stale_calls = (
        lambda: store.settle(
            prepared.identity.effect_id,
            fence=_fence(),
            outcome=_outcome("success"),
        ),
        lambda: store.fail_safe(
            prepared.identity.effect_id,
            fence=_fence(),
            safe_error_code="WORKFLOW_HTTP_TRANSPORT_ERROR",
        ),
        lambda: store.mark_unknown(prepared.identity.effect_id, fence=_fence()),
        lambda: store.recover_abandoned_dispatch(
            prepared.identity.effect_id,
            recovery_fence=_fence(),
        ),
    )
    for operation in stale_calls:
        with pytest.raises(WorkflowHttpExecutionAuthorityLost):
            await operation()

    assert (await store.get(prepared.identity.effect_id)).state == "dispatching"
    assert (
        await store.recover_abandoned_dispatch(
            prepared.identity.effect_id,
            recovery_fence=current_fence,
        )
    ).state == "unknown"


@pytest.mark.anyio
async def test_settle_rejects_untyped_or_malformed_outcome_without_mutation(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    identity = _identity(activation_key="strict-outcome", effect_tail=61)
    await store.prepare(identity, fence=_fence())
    await store.claim_for_dispatch(identity.effect_id, fence=_fence())
    valid_dict = WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.dump_python(
        _outcome("success"),
        mode="json",
    )
    invalid_outcomes = [
        valid_dict,
        None,
        1,
        {**valid_dict, "extra": "not-allowed"},
        {
            "kind": "response_invalid",
            "status_code": "200",
            "duration_ms": 1,
        },
    ]
    for invalid_outcome in invalid_outcomes:
        with pytest.raises(TypeError, match="typed instance"):
            await store.settle(
                identity.effect_id,
                fence=_fence(),
                outcome=invalid_outcome,  # type: ignore[arg-type]
            )
        assert (await store.get(identity.effect_id)).state == "dispatching"


@pytest.mark.anyio
async def test_new_iteration_activation_key_creates_a_distinct_operation(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    first = _identity(activation_key="loop.1.http", effect_tail=70)
    second = _identity(activation_key="loop.2.http", effect_tail=71)
    assert first.operation_key != second.operation_key
    assert (await store.prepare(first, fence=_fence())).identity.effect_id == first.effect_id
    assert (await store.prepare(second, fence=_fence())).identity.effect_id == second.effect_id


@pytest.mark.anyio
async def test_same_activation_with_changed_request_material_is_rejected(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    first = _identity(activation_key="stable-activation", effect_tail=72)
    changed = _identity(activation_key="stable-activation", effect_tail=73)
    changed = changed.model_copy(
        update={
            "request_fingerprint": "f" * 64,
            "operation_key": "e" * 64,
            "idempotency_key": "d" * 64,
        }
    )
    await store.prepare(first, fence=_fence())
    with pytest.raises(WorkflowHttpEffectConflict):
        await store.prepare(changed, fence=_fence())
    async with effect_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM workflow_node_effects")) == 1


@pytest.mark.anyio
async def test_abandoned_write_becomes_unknown_and_has_no_retry(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    identity = _identity(activation_key="unknown-write", effect_tail=80)
    prepared = await store.prepare(identity, fence=_fence())
    await store.claim_for_dispatch(prepared.identity.effect_id, fence=_fence())
    current_fence = await _take_over(effect_engine)
    unknown = await store.recover_abandoned_dispatch(
        prepared.identity.effect_id,
        recovery_fence=current_fence,
    )
    assert unknown.state == "unknown"
    assert unknown.safe_error_code == "SIDE_EFFECT_STATE_UNKNOWN"

    async def must_not_dispatch(_idempotency_key: str | None):
        raise AssertionError("unknown write side effects are terminal")

    with pytest.raises(WorkflowHttpSideEffectUnknown):
        await executor.execute(
            identity,
            fence=current_fence,
            authorize_dispatch=_authorize,
            dispatch=must_not_dispatch,
        )


@pytest.mark.anyio
async def test_failed_before_dispatch_is_safe_but_uncertain_dispatch_is_unknown(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    safe_identity = _identity(activation_key="safe-write", effect_tail=90)
    unknown_identity = _identity(activation_key="unsafe-write", effect_tail=91)
    for identity in (safe_identity, unknown_identity):
        await store.prepare(identity, fence=_fence())
        await store.claim_for_dispatch(identity.effect_id, fence=_fence())

    safe = await store.fail_safe(
        safe_identity.effect_id,
        fence=_fence(),
        safe_error_code="WORKFLOW_HTTP_TRANSPORT_ERROR",
    )
    assert safe.state == "failed_safe"
    unknown = await store.mark_unknown(unknown_identity.effect_id, fence=_fence())
    assert unknown.state == "unknown"


@pytest.mark.anyio
async def test_raw_lease_never_appears_in_effect_row_or_fence_repr(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    identity = _identity(activation_key="raw-lease-private", effect_tail=92)
    await store.prepare(identity, fence=_fence())
    await store.claim_for_dispatch(identity.effect_id, fence=_fence())
    async with effect_engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """SELECT dispatch_lease_token_hash,
                              CAST(row_to_json(e) AS text) AS persisted
                         FROM workflow_node_effects AS e WHERE id=:effect"""
                    ),
                    {"effect": identity.effect_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["dispatch_lease_token_hash"] == hashlib.sha256(LEASE_A.encode()).hexdigest()
    assert LEASE_A not in row["persisted"]
    assert LEASE_A not in repr(_fence())


@pytest.mark.anyio
@pytest.mark.parametrize("authority_result", [False, 1, "true", "exception"])
async def test_live_pre_dispatch_authority_fails_closed_without_network(
    effect_engine: AsyncEngine,
    authority_result: object,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    identity = _identity(activation_key="authority-denied", effect_tail=93)
    dispatch_calls = 0

    async def authorize():
        if authority_result == "exception":
            raise RuntimeError("private authority backend failed")
        return authority_result

    async def dispatch(_idempotency_key: str | None):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return _outcome("success")

    with pytest.raises(WorkflowHttpPreDispatchAuthorityDenied):
        await executor.execute(
            identity,
            fence=_fence(),
            authorize_dispatch=authorize,
            dispatch=dispatch,
        )
    assert dispatch_calls == 0
    record = await store.get(identity.effect_id)
    assert record.state == "prepared"
    assert record.safe_error_code is None


@pytest.mark.anyio
async def test_takeover_during_live_authority_check_leaves_prepared_and_zero_dispatch(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    identity = _identity(activation_key="authority-takeover", effect_tail=94)
    dispatch_calls = 0

    async def authorize_then_lose_lease() -> bool:
        await _take_over(effect_engine)
        return True

    async def dispatch(_idempotency_key: str | None):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return _outcome("success")

    with pytest.raises(WorkflowHttpExecutionAuthorityLost):
        await executor.execute(
            identity,
            fence=_fence(),
            authorize_dispatch=authorize_then_lose_lease,
            dispatch=dispatch,
        )
    assert dispatch_calls == 0
    assert (await store.get(identity.effect_id)).state == "prepared"


@pytest.mark.anyio
async def test_failed_safe_is_recovered_without_automatic_write_retry(
    effect_engine: AsyncEngine,
) -> None:
    store = PostgresWorkflowHttpEffectStore(effect_engine)
    executor = WorkflowHttpEffectExecutor(store)
    identity = _identity(activation_key="safe-terminal", effect_tail=95)
    dispatch_calls = 0

    async def fail_before_origin(_idempotency_key: str | None):
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise WorkflowHttpDispatchFailure(
            "WORKFLOW_HTTP_TRANSPORT_ERROR",
            may_have_reached_origin=False,
        )

    with pytest.raises(WorkflowHttpDispatchFailure):
        await executor.execute(
            identity,
            fence=_fence(),
            authorize_dispatch=_authorize,
            dispatch=fail_before_origin,
        )
    assert dispatch_calls == 1

    async def must_not_dispatch(_idempotency_key: str | None):
        raise AssertionError("failed_safe must not auto-redispatch a write")

    with pytest.raises(WorkflowHttpSafeFailure) as recovered:
        await executor.execute(
            identity.model_copy(update={"effect_id": uuid.uuid4()}),
            fence=_fence(),
            authorize_dispatch=_authorize,
            dispatch=must_not_dispatch,
        )
    assert recovered.value.safe_error_code == "WORKFLOW_HTTP_TRANSPORT_ERROR"
    assert dispatch_calls == 1
