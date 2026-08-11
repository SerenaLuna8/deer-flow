"""Disposable-PostgreSQL contract for durable Workflow Code cleanup.

``bootstrap_schema`` installs the production ``full_schema.sql``; since
``full_schema_v10`` that baseline owns the Workflow Run/Job/lease authority
tables, so this test seeds production rows directly instead of re-applying the
retired Phase-0 prototype DDL on top of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.workflows.code_lease_store import (
    AesGcmWorkflowCodeCleanupCodec,
    PostgresWorkflowCodeLeaseStore,
    WorkflowCodeCleanupCoordinator,
    WorkflowCodeCleanupPending,
    WorkflowCodeExecutionCoordinator,
    WorkflowCodeExecutionFence,
    WorkflowCodeLeaseConflict,
    WorkflowCodeLeaseState,
    WorkflowCodeLocatorContext,
    WorkflowCodeLocatorDecryptFailed,
    WorkflowCodeLocatorKeyring,
    WorkflowCodeProvisioningCoordinator,
)
from deerflow.persistence.bootstrap import bootstrap_schema
from deerflow.workflows.code_execution import (
    CODE_NETWORK_POLICY,
    CODE_RUNTIME_CONTRACT,
    DEFAULT_CODE_LIMITS,
    CodeActivationIdentity,
    CodeCleanupReceipt,
    IsolatedCodeCleanupPending,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionRequest,
    IsolatedCodeExecutionResult,
)

pytestmark = pytest.mark.postgres

PROJECT_ID = uuid.UUID("31000000-0000-4000-8000-000000000001")
WORKFLOW_RUN_ID = uuid.UUID("31000000-0000-4000-8000-000000000002")
WORKFLOW_ID = uuid.UUID("31000000-0000-4000-8000-000000000003")
WORKFLOW_VERSION_ID = uuid.UUID("31000000-0000-4000-8000-000000000004")
JOB_ID = uuid.UUID("31000000-0000-4000-8000-000000000005")
JOB_ATTEMPT_ID = uuid.UUID("31000000-0000-4000-8000-000000000006")
WORKER_ID = uuid.UUID("31000000-0000-4000-8000-000000000007")
REAPER_ID = uuid.UUID("31000000-0000-4000-8000-000000000008")
NODE_ID = "31000000-0000-4000-8000-000000000009"
OWNER_ID = "31000000-0000-4000-8000-000000000010"
LEASE_TOKEN = "job-lease-token-with-at-least-thirty-two-bytes"
ORIGIN_TRACE_ID = "workflow-code-durable-cleanup-trace"
PROFILE_DIGEST = "a" * 64


def _cleanup_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _codec(*, key: bytes = b"k" * 32) -> AesGcmWorkflowCodeCleanupCodec:
    return AesGcmWorkflowCodeCleanupCodec(WorkflowCodeLocatorKeyring(active_key_id="phase0-k1", _keys={"phase0-k1": key}))


class _CleanupProvider:
    def __init__(self, *, pending_once: bool = False, fail_once: bool = False) -> None:
        self.pending_once = pending_once
        self.fail_once = fail_once
        self.cleaned: list[str] = []
        self.reconciled: list[str] = []

    def acquire_reserved(self, request, handle):
        del request, handle
        raise AssertionError("not used by cleanup-only tests")

    def execute(self, lease, request, control):
        del lease, request, control
        return IsolatedCodeExecutionResult(
            outcome="succeeded",
            exit_code=0,
            result={"ok": True},
            stdout_tail="",
            stderr_tail="",
            truncated=False,
            duration_ms=1,
        )

    def cleanup(
        self,
        lease: IsolatedCodeExecutionLease,
        *,
        reason: str,
    ) -> CodeCleanupReceipt:
        self.cleaned.append(lease.resource_id)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic provider cleanup failure")
        if self.pending_once:
            self.pending_once = False
            return CodeCleanupReceipt(
                lease_id=lease.lease_id,
                state="cleanup_pending",
                reason="cleanup_retry",
            )
        return CodeCleanupReceipt(
            lease_id=lease.lease_id,
            state="destroyed_confirmed",
            reason=reason,
        )

    def reconcile_provisioning(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> CodeCleanupReceipt:
        assert len(reconciliation_key_hash) == 64
        self.reconciled.append(lease_id)
        return CodeCleanupReceipt(
            lease_id=lease_id,
            state="destroyed_confirmed",
            reason="worker_crash_reconcile",
        )

    def release_provisioning_handle(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> None:
        assert lease_id
        assert len(reconciliation_key_hash) == 64


class _BlockingAcquireProvider(_CleanupProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.handle = None

    def acquire_reserved(self, request, handle):
        self.handle = handle
        self.started.set()
        assert self.release.wait(timeout=5)
        return IsolatedCodeExecutionLease(
            lease_id=handle.lease_id,
            activation_digest=request.activation.digest(),
            profile_digest=request.profile_digest,
            resource_id="provider-resource-private",
        )


class _InvalidAcquireProvider(_CleanupProvider):
    def __init__(self) -> None:
        super().__init__()
        self.handle = None

    def acquire_reserved(self, request, handle):
        del request
        self.handle = handle
        return object()


class _FailingCodec:
    def seal(self, lease, context):
        del lease, context
        raise RuntimeError("synthetic locator seal failure")

    def open(self, ciphertext, context):
        del ciphertext, context
        raise AssertionError("not used")


class _ExecuteErrorProvider(_BlockingAcquireProvider):
    def execute(self, lease, request, control):
        del lease, request, control
        raise RuntimeError("synthetic execute failure")


class _BlockingExecutionProvider(_BlockingAcquireProvider):
    def execute(self, lease, request, control):
        del lease, request
        while control.interruption() is None:
            time.sleep(0.005)
        interruption = control.interruption()
        return IsolatedCodeExecutionResult(
            outcome="cancelled",
            exit_code=None,
            result=None,
            stdout_tail="",
            stderr_tail="",
            truncated=False,
            duration_ms=1,
            interruption=interruption,
        )


class _ProbeFailStore(PostgresWorkflowCodeLeaseStore):
    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self.failed = False

    async def execution_fence_is_current(self, fence):
        if not self.failed:
            self.failed = True
            raise RuntimeError("synthetic authority probe failure")
        return await super().execution_fence_is_current(fence)


async def _setup(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url)
    await bootstrap_schema(engine)
    return engine


async def _seed_authority(engine: AsyncEngine) -> WorkflowCodeExecutionFence:
    token_hash = hashlib.sha256(LEASE_TOKEN.encode()).hexdigest()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                   (id,email,system_role,created_at,needs_setup,token_version)
                   VALUES (:id,:email,'user',now(),false,0)"""
            ),
            {"id": OWNER_ID, "email": f"{OWNER_ID}@example.com"},
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                   (id,slug,display_name,created_by_user_id)
                   VALUES (:id,'workflow-code-cleanup','Code cleanup',:owner)"""
            ),
            {"id": PROJECT_ID, "owner": OWNER_ID},
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                   (id,project_id,user_id,role,status,version)
                   VALUES (:id,:project,:owner,'admin','active',1)"""
            ),
            {"id": uuid.uuid4(), "project": PROJECT_ID, "owner": OWNER_ID},
        )
        await connection.execute(
            text(
                """INSERT INTO workflow_definitions
                   (id,project_id,name,status,revision,created_by,updated_by)
                   VALUES (:id,:project,'Code cleanup workflow','active',1,
                           :owner,:owner)"""
            ),
            {"id": WORKFLOW_ID, "project": PROJECT_ID, "owner": OWNER_ID},
        )
        await connection.execute(
            text(
                """INSERT INTO workflow_versions
                   (id,workflow_id,project_id,version_number,spec_json,canvas_json,
                    semantic_checksum,compiler_contract_version,published_by)
                   VALUES (:id,:workflow,:project,1,'{}','{}',:checksum,1,:owner)"""
            ),
            {
                "id": WORKFLOW_VERSION_ID,
                "workflow": WORKFLOW_ID,
                "project": PROJECT_ID,
                "checksum": "c" * 64,
                "owner": OWNER_ID,
            },
        )
        for worker_id in (WORKER_ID, REAPER_ID):
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,max_concurrent_jobs,heartbeat_at,
                        runtime_profile_digests_json)
                       VALUES (:id,'test','[]',1,clock_timestamp(),:profiles)"""
                ),
                {
                    "id": worker_id,
                    "profiles": f'["{PROFILE_DIGEST}"]',
                },
            )
        started_at = await connection.scalar(text("SELECT clock_timestamp()"))
        await connection.execute(
            text(
                """INSERT INTO workflow_runs
                   (id,project_id,owner_user_id,workflow_id,workflow_version_id,status,
                    input_json,input_digest,idempotency_hash,admission_request_digest,
                    trigger_kind,origin_trace_id,required_worker_profile_digest,
                    worker_profile_key,execution_epoch,started_at)
                   VALUES (:id,:project,:owner,:workflow,:version,'running','{}',
                           :input_digest,:idempotency_hash,:admission_digest,'manual',
                           :trace,:profile,:profile,1,:started_at)"""
            ),
            {
                "id": WORKFLOW_RUN_ID,
                "project": PROJECT_ID,
                "owner": OWNER_ID,
                "workflow": WORKFLOW_ID,
                "version": WORKFLOW_VERSION_ID,
                "input_digest": "1" * 64,
                "idempotency_hash": "2" * 64,
                "admission_digest": "3" * 64,
                "trace": ORIGIN_TRACE_ID,
                "profile": PROFILE_DIGEST,
                "started_at": started_at,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO jobs
                   (id,job_type,project_id,owner_user_id,workflow_run_id,workflow_epoch,
                    required_worker_profile_digest,workflow_profile_key,origin_trace_id,
                    idempotency_key,status,max_attempts,attempt_count,lease_owner_id,
                    lease_token_hash,lease_expires_at,heartbeat_at)
                   VALUES (:id,'workflow_run',:project,:owner,:run,1,:profile,:profile,
                           :trace,:key,'running',3,1,:worker,:token_hash,
                           CURRENT_TIMESTAMP + interval '5 minutes',clock_timestamp())"""
            ),
            {
                "id": JOB_ID,
                "project": PROJECT_ID,
                "owner": OWNER_ID,
                "run": WORKFLOW_RUN_ID,
                "profile": PROFILE_DIGEST,
                "trace": ORIGIN_TRACE_ID,
                "key": "b" * 64,
                "worker": WORKER_ID,
                "token_hash": token_hash,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO job_attempts
                   (id,job_id,attempt_number,worker_id,lease_token_hash,
                    started_at,heartbeat_at)
                   VALUES (:id,:job,1,:worker,:token_hash,
                           clock_timestamp(),clock_timestamp())"""
            ),
            {
                "id": JOB_ATTEMPT_ID,
                "job": JOB_ID,
                "worker": WORKER_ID,
                "token_hash": token_hash,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO workflow_run_jobs
                   (workflow_run_id,execution_epoch,job_id,project_id,owner_user_id,
                    worker_profile_key,cause)
                   VALUES (:run,1,:job,:project,:owner,:profile,'initial')"""
            ),
            {
                "run": WORKFLOW_RUN_ID,
                "job": JOB_ID,
                "project": PROJECT_ID,
                "owner": OWNER_ID,
                "profile": PROFILE_DIGEST,
            },
        )
        await connection.execute(
            text("UPDATE workflow_runs SET current_job_id=:job WHERE id=:run"),
            {"job": JOB_ID, "run": WORKFLOW_RUN_ID},
        )
    return WorkflowCodeExecutionFence(
        workflow_run_id=WORKFLOW_RUN_ID,
        project_id=PROJECT_ID,
        owner_user_id=OWNER_ID,
        origin_trace_id=ORIGIN_TRACE_ID,
        job_id=JOB_ID,
        workflow_epoch=1,
        job_attempt_number=1,
        worker_id=WORKER_ID,
        profile_digest=PROFILE_DIGEST,
        raw_job_lease_token=LEASE_TOKEN,
    )


async def _expire_cleanup_claim(engine: AsyncEngine, lease_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE workflow_code_sandbox_leases
                   SET cleanup_lease_expires_at=CURRENT_TIMESTAMP - interval '1 second'
                   WHERE id=:id"""
            ),
            {"id": lease_id},
        )


def _activation(*, attempt: int = 1) -> CodeActivationIdentity:
    return CodeActivationIdentity(
        project_id=str(PROJECT_ID),
        owner_user_id=OWNER_ID,
        workflow_run_id=str(WORKFLOW_RUN_ID),
        node_id=NODE_ID,
        activation_id="root.python",
        attempt=attempt,
    )


def _provider_lease(activation: CodeActivationIdentity) -> IsolatedCodeExecutionLease:
    return IsolatedCodeExecutionLease(
        lease_id="provider-lease-private",
        activation_digest=activation.digest(),
        profile_digest=PROFILE_DIGEST,
        resource_id="provider-resource-private",
    )


def _execution_request(activation: CodeActivationIdentity) -> IsolatedCodeExecutionRequest:
    source = "def main(inputs):\n    return {'ok': True}\n"
    return IsolatedCodeExecutionRequest(
        runtime_contract=CODE_RUNTIME_CONTRACT,
        activation=activation,
        profile_digest=PROFILE_DIGEST,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        inputs={},
        limits=DEFAULT_CODE_LIMITS,
        network_policy=CODE_NETWORK_POLICY,
    )


def test_locator_is_authenticated_and_bound_to_full_scope() -> None:
    activation = _activation()
    context = WorkflowCodeLocatorContext(
        lease_row_id=uuid.UUID("31000000-0000-4000-8000-000000000011"),
        project_id=PROJECT_ID,
        owner_user_id=OWNER_ID,
        workflow_run_id=WORKFLOW_RUN_ID,
        node_id=NODE_ID,
        activation_id=activation.activation_id,
        activation_attempt=activation.attempt,
        profile_digest=PROFILE_DIGEST,
    )
    lease = _provider_lease(activation)
    codec = _codec()
    ciphertext = codec.seal(lease, context)
    assert ciphertext.startswith(b"AWCL\x01")
    assert lease.resource_id.encode() not in ciphertext
    assert codec.open(ciphertext, context) == lease

    with pytest.raises(WorkflowCodeLocatorDecryptFailed):
        _codec(key=b"z" * 32).open(ciphertext, context)
    with pytest.raises(WorkflowCodeLocatorDecryptFailed):
        codec.open(
            ciphertext,
            WorkflowCodeLocatorContext(
                lease_row_id=context.lease_row_id,
                project_id=uuid.UUID("31000000-0000-4000-8000-000000000099"),
                owner_user_id=context.owner_user_id,
                workflow_run_id=context.workflow_run_id,
                node_id=context.node_id,
                activation_id=context.activation_id,
                activation_attempt=context.activation_attempt,
                profile_digest=context.profile_digest,
            ),
        )


@pytest.mark.asyncio
async def test_provisioning_precedes_locator_and_activation_attempt_is_distinct(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        lease = await store.begin_provisioning(
            fence,
            _activation(attempt=7),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        assert lease.state is WorkflowCodeLeaseState.PROVISIONING
        assert lease.activation_attempt == 7
        assert lease.job_attempt_number == 1
        assert lease.cleanup_locator_ciphertext is None
        assert "LEASE_TOKEN" not in repr(lease)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_column", "wrong_value"),
    [
        ("project_id", uuid.UUID("31000000-0000-4000-8000-000000000091")),
        ("workflow_run_id", uuid.UUID("31000000-0000-4000-8000-000000000092")),
        ("workflow_epoch", 2),
    ],
)
async def test_lease_scope_cannot_drift_from_job_project_run_or_epoch(
    postgres_database_url: str,
    scope_column: str,
    wrong_value: uuid.UUID | int,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        lease = await store.begin_provisioning(
            fence,
            _activation(),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        assert scope_column in {"project_id", "workflow_run_id", "workflow_epoch"}
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"UPDATE workflow_code_sandbox_leases SET {scope_column}=:wrong_value WHERE id=:lease_id"),
                    {"wrong_value": wrong_value, "lease_id": lease.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_lease_restricts_deletion_of_its_run_job_mapping(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        await store.begin_provisioning(
            fence,
            _activation(),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """DELETE FROM workflow_run_jobs
                           WHERE workflow_run_id=:run
                             AND execution_epoch=:epoch
                             AND job_id=:job"""
                    ),
                    {"run": WORKFLOW_RUN_ID, "epoch": 1, "job": JOB_ID},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_coordinator_commits_provisioning_before_provider_acquire(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    provider = _BlockingAcquireProvider()
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        coordinator = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        activation = _activation()
        task = asyncio.create_task(
            coordinator.reserve_and_acquire(
                fence,
                _execution_request(activation),
                cleanup_deadline=_cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        )
        assert await asyncio.to_thread(provider.started.wait, 5)
        assert provider.handle is not None
        journaled = await store.get(uuid.UUID(provider.handle.lease_id))
        assert journaled is not None
        assert journaled.state is WorkflowCodeLeaseState.PROVISIONING
        assert journaled.cleanup_locator_ciphertext is None
        assert provider.handle.reconciliation_key_hash == journaled.reconciliation_key_hash
        provider.release.set()
        acquired = await task
        assert acquired.record.state is WorkflowCodeLeaseState.RUNNING
        assert acquired.record.cleanup_locator_ciphertext is not None
        assert acquired.provider_lease.resource_id == "provider-resource-private"
    finally:
        provider.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_acquire_crossing_database_job_expiry_cannot_activate(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    provider = _BlockingAcquireProvider()
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        coordinator = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        task = asyncio.create_task(
            coordinator.reserve_and_acquire(
                fence,
                _execution_request(_activation()),
                cleanup_deadline=_cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        )
        assert await asyncio.to_thread(provider.started.wait, 5)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE jobs SET lease_expires_at=
                       CURRENT_TIMESTAMP - interval '1 second' WHERE id=:job"""
                ),
                {"job": JOB_ID},
            )
        provider.release.set()
        with pytest.raises(WorkflowCodeLeaseConflict):
            await task
        assert provider.handle is not None
        row = await store.get(uuid.UUID(provider.handle.lease_id))
        assert row is not None
        assert row.state is WorkflowCodeLeaseState.DESTROYED
    finally:
        provider.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_wrong_frozen_profile_is_rejected_before_reservation(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        with pytest.raises(WorkflowCodeLeaseConflict):
            await store.begin_provisioning(
                replace(fence, profile_digest="b" * 64),
                _activation(),
                profile_digest="b" * 64,
                cleanup_deadline=_cleanup_deadline(),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["lease", "codec"])
async def test_invalid_acquire_or_locator_seal_is_durably_reconciled(
    postgres_database_url: str,
    invalid_kind: str,
) -> None:
    engine = await _setup(postgres_database_url)
    provider = _InvalidAcquireProvider() if invalid_kind == "lease" else _BlockingAcquireProvider()
    if isinstance(provider, _BlockingAcquireProvider):
        provider.release.set()
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        coordinator = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_FailingCodec() if invalid_kind == "codec" else _codec(),
        )
        expected = TypeError if invalid_kind == "lease" else RuntimeError
        with pytest.raises(expected):
            await coordinator.reserve_and_acquire(
                fence,
                _execution_request(_activation()),
                cleanup_deadline=_cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        assert provider.handle is not None
        row = await store.get(uuid.UUID(provider.handle.lease_id))
        assert row is not None
        assert row.state is WorkflowCodeLeaseState.DESTROYED
    finally:
        if isinstance(provider, _BlockingAcquireProvider):
            provider.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_current_execution_fence_is_required_for_activate_and_cleanup(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        activation = _activation()
        row = await store.begin_provisioning(
            fence,
            activation,
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        invalid = [
            fence.with_raw_job_lease_token("wrong-token-with-at-least-thirty-two-bytes"),
            fence.with_workflow_epoch(2),
            fence.with_job_attempt_number(2),
            fence.with_worker_id(REAPER_ID),
        ]
        for stale in invalid:
            with pytest.raises(WorkflowCodeLeaseConflict):
                await store.activate(
                    row.id,
                    stale,
                    cleanup_locator_ciphertext=b"private-ciphertext",
                )

        running = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=b"private-ciphertext",
        )
        assert running.state is WorkflowCodeLeaseState.RUNNING

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET cancel_requested_at=clock_timestamp() WHERE id=:job"),
                {"job": JOB_ID},
            )
        with pytest.raises(WorkflowCodeLeaseConflict):
            await store.begin_cleanup(running.id, fence)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_pending_uses_independent_fence_and_destroy_scrubs_locator(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        activation = _activation()
        row = await store.begin_provisioning(
            fence,
            activation,
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        row = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=b"private-ciphertext",
        )
        claim = await store.begin_cleanup(
            row.id,
            fence,
            cleanup_worker_id=WORKER_ID,
            cleanup_lease_seconds=30,
        )
        assert claim.record.state is WorkflowCodeLeaseState.CLEANUP_PENDING
        assert claim.record.execution_lease_token_hash is None
        assert claim.record.cleanup_locator_ciphertext == b"private-ciphertext"
        assert claim.raw_cleanup_token not in repr(claim)

        with pytest.raises(WorkflowCodeLeaseConflict):
            await store.confirm_destroyed(
                row.id,
                cleanup_worker_id=WORKER_ID,
                raw_cleanup_token="wrong-cleanup-token-with-thirty-two-bytes",
            )
        destroyed = await store.confirm_destroyed(
            row.id,
            cleanup_worker_id=WORKER_ID,
            raw_cleanup_token=claim.raw_cleanup_token,
        )
        assert destroyed.state is WorkflowCodeLeaseState.DESTROYED
        assert destroyed.cleanup_locator_ciphertext is None
        assert destroyed.destroyed_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_cleanup_claim_is_taken_over_and_old_owner_is_fenced(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        row = await store.begin_provisioning(
            fence,
            _activation(),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        row = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=b"private-ciphertext",
        )
        first = await store.begin_cleanup(
            row.id,
            fence,
            cleanup_worker_id=WORKER_ID,
            cleanup_lease_seconds=5,
        )
        await _expire_cleanup_claim(engine, row.id)
        second = await store.claim_cleanup_pending(
            cleanup_worker_id=REAPER_ID,
            cleanup_lease_seconds=30,
        )
        assert second is not None
        assert second.record.id == row.id
        assert second.record.cleanup_owner_worker_id == REAPER_ID

        with pytest.raises(WorkflowCodeLeaseConflict):
            await store.confirm_destroyed(
                row.id,
                cleanup_worker_id=WORKER_ID,
                raw_cleanup_token=first.raw_cleanup_token,
            )
        await store.confirm_destroyed(
            row.id,
            cleanup_worker_id=REAPER_ID,
            raw_cleanup_token=second.raw_cleanup_token,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reaper_coordinator_decodes_private_locator_and_retries_cleanup(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        codec = _codec()
        activation = _activation()
        provider_lease = _provider_lease(activation)
        row = await store.begin_provisioning(
            fence,
            activation,
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        row = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=codec.seal(provider_lease, row.locator_context),
        )
        await store.begin_cleanup(
            row.id,
            fence,
            cleanup_worker_id=WORKER_ID,
            cleanup_lease_seconds=5,
        )
        await _expire_cleanup_claim(engine, row.id)

        provider = _CleanupProvider(pending_once=True)
        coordinator = WorkflowCodeCleanupCoordinator(
            store=store,
            provider=provider,
            codec=codec,
        )
        pending = await coordinator.reap_one(
            cleanup_worker_id=REAPER_ID,
            cleanup_lease_seconds=30,
        )
        assert isinstance(pending, WorkflowCodeCleanupPending)
        assert provider.cleaned == ["provider-resource-private"]

        completed = await coordinator.reap_one(
            cleanup_worker_id=REAPER_ID,
            cleanup_lease_seconds=30,
        )
        assert completed is not None
        assert completed.state is WorkflowCodeLeaseState.DESTROYED
        assert provider.cleaned == [
            "provider-resource-private",
            "provider-resource-private",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_activation_unique_is_safe_under_real_concurrency(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)

        async def reserve(attempt: int):
            return await store.begin_provisioning(
                fence,
                _activation(attempt=attempt),
                profile_digest=PROFILE_DIGEST,
                cleanup_deadline=_cleanup_deadline(),
            )

        outcomes = await asyncio.gather(reserve(1), reserve(2), return_exceptions=True)
        assert sum(isinstance(item, WorkflowCodeLeaseConflict) for item in outcomes) == 1
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_reapers_cannot_hold_the_same_cleanup_claim(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        row = await store.begin_provisioning(
            fence,
            _activation(),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        row = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=b"private-ciphertext",
        )
        await store.begin_cleanup(
            row.id,
            fence,
            cleanup_worker_id=WORKER_ID,
            cleanup_lease_seconds=5,
        )
        await _expire_cleanup_claim(engine, row.id)
        claims = await asyncio.gather(
            store.claim_cleanup_pending(
                cleanup_worker_id=REAPER_ID,
                cleanup_lease_seconds=30,
            ),
            store.claim_cleanup_pending(
                cleanup_worker_id=uuid.uuid4(),
                cleanup_lease_seconds=30,
            ),
        )
        assert sum(claim is not None for claim in claims) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_provisioning_is_immediately_claimed_for_exact_reconciliation(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        reservation = await store.reserve_provisioning(
            fence,
            _activation(),
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        assert reservation.provider_handle.reconciliation_key not in repr(reservation)
        assert reservation.provider_handle.reconciliation_key_hash == reservation.record.reconciliation_key_hash
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET cancel_requested_at=clock_timestamp() WHERE id=:job"),
                {"job": JOB_ID},
            )
        provider = _CleanupProvider()
        coordinator = WorkflowCodeCleanupCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        destroyed = await coordinator.reap_one(
            cleanup_worker_id=REAPER_ID,
            cleanup_lease_seconds=30,
        )
        assert destroyed is not None
        assert destroyed.state is WorkflowCodeLeaseState.DESTROYED
        assert provider.reconciled == [str(reservation.record.id)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_failure_remains_durable_and_retryable(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        activation = _activation()
        row = await store.begin_provisioning(
            fence,
            activation,
            profile_digest=PROFILE_DIGEST,
            cleanup_deadline=_cleanup_deadline(),
        )
        codec = _codec()
        row = await store.activate(
            row.id,
            fence,
            cleanup_locator_ciphertext=codec.seal(_provider_lease(activation), row.locator_context),
        )
        await store.begin_cleanup(
            row.id,
            fence,
            cleanup_worker_id=WORKER_ID,
            cleanup_lease_seconds=5,
        )
        await _expire_cleanup_claim(engine, row.id)
        provider = _CleanupProvider(fail_once=True)
        coordinator = WorkflowCodeCleanupCoordinator(
            store=store,
            provider=provider,
            codec=codec,
        )
        with pytest.raises(RuntimeError, match="synthetic provider cleanup failure"):
            await coordinator.reap_one(
                cleanup_worker_id=REAPER_ID,
                cleanup_lease_seconds=30,
            )
        pending = await store.get(row.id)
        assert pending is not None
        assert pending.state is WorkflowCodeLeaseState.CLEANUP_PENDING
        assert pending.destroyed_at is None
        destroyed = await coordinator.reap_one(
            cleanup_worker_id=REAPER_ID,
            cleanup_lease_seconds=30,
        )
        assert destroyed is not None
        assert destroyed.state is WorkflowCodeLeaseState.DESTROYED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "execute_error", "cleanup_pending"])
async def test_execution_coordinator_never_returns_before_durable_cleanup(
    postgres_database_url: str,
    mode: str,
) -> None:
    engine = await _setup(postgres_database_url)
    if mode == "execute_error":
        provider = _ExecuteErrorProvider()
    else:
        provider = _BlockingAcquireProvider()
    provider.release.set()
    provider.pending_once = mode == "cleanup_pending"
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        provisioning = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        coordinator = WorkflowCodeExecutionCoordinator(
            provisioning=provisioning,
            store=store,
            provider=provider,
        )
        call = coordinator.execute(
            fence,
            _execution_request(_activation()),
            cleanup_deadline=_cleanup_deadline(),
            cleanup_lease_seconds=30,
        )
        if mode == "execute_error":
            with pytest.raises(RuntimeError, match="synthetic execute failure"):
                await call
        elif mode == "cleanup_pending":
            with pytest.raises(IsolatedCodeCleanupPending):
                await call
        else:
            completion = await call
            assert completion.result.outcome == "succeeded"
            assert completion.cleanup.state == "destroyed_confirmed"
        assert provider.handle is not None
        row = await store.get(uuid.UUID(provider.handle.lease_id))
        assert row is not None
        expected_state = WorkflowCodeLeaseState.CLEANUP_PENDING if mode == "cleanup_pending" else WorkflowCodeLeaseState.DESTROYED
        assert row.state is expected_state
    finally:
        provider.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_failure_joins_execution_and_destroys_before_propagating(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    provider = _BlockingExecutionProvider()
    provider.release.set()
    try:
        fence = await _seed_authority(engine)
        store = _ProbeFailStore(engine)
        provisioning = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        coordinator = WorkflowCodeExecutionCoordinator(
            provisioning=provisioning,
            store=store,
            provider=provider,
        )
        with pytest.raises(RuntimeError, match="synthetic authority probe failure"):
            await coordinator.execute(
                fence,
                _execution_request(_activation()),
                cleanup_deadline=_cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        assert provider.handle is not None
        row = await store.get(uuid.UUID(provider.handle.lease_id))
        assert row is not None
        assert row.state is WorkflowCodeLeaseState.DESTROYED
    finally:
        provider.release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_cancellation_waits_for_destroyed_barrier(
    postgres_database_url: str,
) -> None:
    engine = await _setup(postgres_database_url)
    provider = _BlockingExecutionProvider()
    provider.release.set()
    try:
        fence = await _seed_authority(engine)
        store = PostgresWorkflowCodeLeaseStore(engine)
        provisioning = WorkflowCodeProvisioningCoordinator(
            store=store,
            provider=provider,
            codec=_codec(),
        )
        coordinator = WorkflowCodeExecutionCoordinator(
            provisioning=provisioning,
            store=store,
            provider=provider,
        )
        task = asyncio.create_task(
            coordinator.execute(
                fence,
                _execution_request(_activation()),
                cleanup_deadline=_cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        )
        assert await asyncio.to_thread(provider.started.wait, 5)
        while provider.handle is None:
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await store.get(uuid.UUID(provider.handle.lease_id))
        assert row is not None
        assert row.state is WorkflowCodeLeaseState.DESTROYED
    finally:
        provider.release.set()
        await engine.dispose()


def test_codec_and_callbacks_require_strict_contracts() -> None:
    with pytest.raises(TypeError):
        WorkflowCodeCleanupCoordinator(  # type: ignore[arg-type]
            store=object(), provider=_CleanupProvider(), codec=_codec()
        )
    with pytest.raises(TypeError):
        WorkflowCodeCleanupCoordinator(  # type: ignore[arg-type]
            store=object(), provider=_CleanupProvider(), codec=True
        )
