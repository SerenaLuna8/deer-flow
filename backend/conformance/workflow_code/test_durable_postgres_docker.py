"""Combined real-PostgreSQL + real-Docker process-death cleanup gate."""

# ruff: noqa: E402, I001 -- bootstrap backend imports before loading app modules.

from __future__ import annotations

import asyncio
import importlib.util
import multiprocessing
import os
import signal
import sys
import time
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = BACKEND_ROOT / "tests"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest
from conformance.workflow_code.conftest import docker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.workflows.code_lease_store import (
    AesGcmWorkflowCodeCleanupCodec,
    PostgresWorkflowCodeLeaseStore,
    WorkflowCodeCleanupCoordinator,
    WorkflowCodeExecutionFence,
    WorkflowCodeLeaseState,
    WorkflowCodeLocatorKeyring,
    WorkflowCodeProvisioningCoordinator,
)
from deerflow.workflows.code_execution import IsolatedCodeExecutionRequest
from deerflow.workflows.code_execution.docker_provider import (
    DockerIsolatedCodeExecutionProvider,
    _provisioning_lease_path,
)


def _load_g03_test_support():
    path = TESTS_ROOT / "test_workflow_code_lease_store_postgres.py"
    spec = importlib.util.spec_from_file_location("g03_pg_support", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load G03 PostgreSQL support")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_postgres_utils():
    sys.path.insert(0, str(TESTS_ROOT))
    try:
        import postgres_utils

        return postgres_utils
    finally:
        sys.path.remove(str(TESTS_ROOT))


def _crash_after_reserved_docker_acquire(
    database_url: str,
    image_id: str,
    runner_digest: str,
    fence_payload: dict,
    request_payload: dict,
    connection,
) -> None:
    class _CrashWindowProvider(DockerIsolatedCodeExecutionProvider):
        def acquire_reserved(self, request, handle):
            lease = super().acquire_reserved(request, handle)
            connection.send(lease.model_dump(mode="json"))
            while True:
                time.sleep(1)

    async def run() -> None:
        engine = create_async_engine(database_url)
        provider = _CrashWindowProvider(
            image_id=image_id,
            runner_digest=runner_digest,
        )
        try:
            store = PostgresWorkflowCodeLeaseStore(engine)
            codec = AesGcmWorkflowCodeCleanupCodec(
                WorkflowCodeLocatorKeyring(
                    active_key_id="combined-k1",
                    _keys={"combined-k1": b"c" * 32},
                )
            )
            coordinator = WorkflowCodeProvisioningCoordinator(
                store=store,
                provider=provider,
                codec=codec,
            )
            await coordinator.reserve_and_acquire(
                WorkflowCodeExecutionFence(**fence_payload),
                IsolatedCodeExecutionRequest.model_validate(request_payload, strict=True),
                cleanup_deadline=_load_g03_test_support()._cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _crash_after_running_commit(
    database_url: str,
    image_id: str,
    runner_digest: str,
    fence_payload: dict,
    request_payload: dict,
    connection,
) -> None:
    async def run() -> None:
        engine = create_async_engine(database_url)
        provider = DockerIsolatedCodeExecutionProvider(
            image_id=image_id,
            runner_digest=runner_digest,
        )
        try:
            store = PostgresWorkflowCodeLeaseStore(engine)
            codec = AesGcmWorkflowCodeCleanupCodec(
                WorkflowCodeLocatorKeyring(
                    active_key_id="combined-k1",
                    _keys={"combined-k1": b"c" * 32},
                )
            )
            coordinator = WorkflowCodeProvisioningCoordinator(
                store=store,
                provider=provider,
                codec=codec,
            )
            provisioned = await coordinator.reserve_and_acquire(
                WorkflowCodeExecutionFence(**fence_payload),
                IsolatedCodeExecutionRequest.model_validate(request_payload, strict=True),
                cleanup_deadline=_load_g03_test_support()._cleanup_deadline(),
                cleanup_lease_seconds=30,
            )
            connection.send(provisioned.provider_lease.model_dump(mode="json"))
            while True:
                time.sleep(1)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_window", "expected_state"),
    [
        ("before_running", WorkflowCodeLeaseState.PROVISIONING),
        ("after_running", WorkflowCodeLeaseState.RUNNING),
    ],
)
async def test_sigkill_is_reaped_exactly_from_durable_journal(
    runner_image_id: str,
    runner_digest: str,
    crash_window: str,
    expected_state: WorkflowCodeLeaseState,
) -> None:
    database_url = os.environ.get("DATABASE_URL")
    assert database_url, "DATABASE_URL is required for combined G03 conformance"
    postgres_utils = _load_postgres_utils()
    async with postgres_utils.temporary_postgres_database(postgres_utils.replace_database(database_url, "postgres")) as disposable_url:
        support = _load_g03_test_support()
        original_profile_digest = support.PROFILE_DIGEST
        provider = DockerIsolatedCodeExecutionProvider(
            image_id=runner_image_id,
            runner_digest=runner_digest,
        )
        profile = provider.attest().profile_digest
        support.PROFILE_DIGEST = profile
        engine = None
        worker = None
        parent_connection = None
        child_connection = None
        try:
            engine = await support._setup(disposable_url)
            fence = await support._seed_authority(engine)
            request = support._execution_request(support._activation())
            fence_payload = {
                "workflow_run_id": fence.workflow_run_id,
                "project_id": fence.project_id,
                "owner_user_id": fence.owner_user_id,
                "origin_trace_id": fence.origin_trace_id,
                "job_id": fence.job_id,
                "workflow_epoch": fence.workflow_epoch,
                "job_attempt_number": fence.job_attempt_number,
                "worker_id": fence.worker_id,
                "profile_digest": fence.profile_digest,
                "raw_job_lease_token": fence.raw_job_lease_token,
            }
            context = multiprocessing.get_context("spawn")
            parent_connection, child_connection = context.Pipe(duplex=False)
            child_target = _crash_after_reserved_docker_acquire if crash_window == "before_running" else _crash_after_running_commit
            worker = context.Process(
                target=child_target,
                args=(
                    disposable_url,
                    runner_image_id,
                    runner_digest,
                    fence_payload,
                    request.model_dump(mode="json"),
                    child_connection,
                ),
            )
            worker.start()
            child_connection.close()
            assert parent_connection.poll(20)
            lease_payload = parent_connection.recv()
            parent_connection.close()
            lease_id = lease_payload["lease_id"]
            store = PostgresWorkflowCodeLeaseStore(engine)
            journal = await store.get(uuid.UUID(lease_id))
            assert journal is not None
            assert journal.state is expected_state
            assert (journal.cleanup_locator_ciphertext is None) is (expected_state is WorkflowCodeLeaseState.PROVISIONING)

            os.kill(worker.pid, signal.SIGKILL)
            worker.join(timeout=10)
            assert worker.exitcode == -signal.SIGKILL
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE jobs SET lease_expires_at=
                           CURRENT_TIMESTAMP - interval '1 second' WHERE id=:job"""
                    ),
                    {"job": fence.job_id},
                )

            codec = AesGcmWorkflowCodeCleanupCodec(
                WorkflowCodeLocatorKeyring(
                    active_key_id="combined-k1",
                    _keys={"combined-k1": b"c" * 32},
                )
            )
            reaper = WorkflowCodeCleanupCoordinator(
                store=store,
                provider=provider,
                codec=codec,
            )
            destroyed = await reaper.reap_one(
                cleanup_worker_id=support.REAPER_ID,
                cleanup_lease_seconds=30,
            )
            assert destroyed is not None
            assert destroyed.state is WorkflowCodeLeaseState.DESTROYED
            assert docker("container", "inspect", lease_payload["resource_id"], check=False).returncode != 0
            assert (await store.get(journal.id)).state is WorkflowCodeLeaseState.DESTROYED
            lock_path, _ = _provisioning_lease_path(
                lease_id,
                journal.reconciliation_key_hash,
            )
            assert not lock_path.exists()
        finally:
            if child_connection is not None:
                child_connection.close()
            if parent_connection is not None:
                parent_connection.close()
            if worker is not None and worker.is_alive():
                os.kill(worker.pid, signal.SIGKILL)
            if worker is not None:
                worker.join(timeout=10)
            provider.reconcile_orphans()
            if not provider._active_resources:
                provider.close()
            if engine is not None:
                await engine.dispose()
            support.PROFILE_DIGEST = original_profile_digest
