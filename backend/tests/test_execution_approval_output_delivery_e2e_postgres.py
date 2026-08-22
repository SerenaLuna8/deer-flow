"""End-to-end PostgreSQL coverage for deferred approval output delivery.

The Local command sandbox below is deterministic and in-memory.  It exercises
the production frozen-command runner and final spawn authorization boundary,
but deliberately does not create an operating-system process.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from support.private_thread_seed import (
    TEST_MODEL_REF,
    PrivateThreadSeed,
    seed_private_thread_database,
)
from support.system_model_seed import seed_system_model_config

from app.private_work.execution_approval import (
    ExecutionApprovalService,
    HostExecutionProviderPolicySnapshot,
    WorkerHostExecutionApprovalPort,
    settle_staged_execution_approvals,
)
from app.private_work.file_finalizer import PrivateFileFinalizer
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.sandbox_files import PrivateFileRunScope
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.contracts import AgentExecutionResult
from app.reliability.run_execution.handler import PrivateRunJobHandler
from app.system_runtime_settings import SystemRuntimePolicyService
from app.system_settings import SystemModelCatalogService
from deerflow.config.app_config import AppConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.file_authority import AuthorityManifest, AuthorityManifestEntry
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.private_work import PrivateArtifactRow, PrivateFileRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.host_execution_approval import HostExecutionPlan
from deerflow.runtime.host_execution_domain import (
    HostExecutionDomainSnapshot,
    host_execution_environment_fingerprint,
)
from deerflow.runtime.host_execution_runner import (
    execute_frozen_host_execution_continuation,
)
from deerflow.sandbox.env_policy import build_sandbox_env


def _app_config(database_url: str) -> AppConfig:
    return AppConfig(
        database=DatabaseConfig(url=database_url),
        sandbox=SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            allow_host_bash=False,
            host_execution_approval={
                "mode": "approval_required",
                "execution_domain_id": "e2e-worker",
            },
            bash_command_timeout=60,
            bash_output_max_chars=2_000,
        ),
    )


def _execution_domain() -> HostExecutionDomainSnapshot:
    return HostExecutionDomainSnapshot(
        configured_id="e2e-worker",
        public_label="Deterministic test Worker",
        os_name="posix",
        sys_platform="darwin",
        machine="arm64",
        device_fingerprint="d" * 64,
        environment_fingerprint=host_execution_environment_fingerprint(
            build_sandbox_env(None),
        ),
        euid=501,
        egid=20,
        runtime_base_dir="/srv/actweave-e2e",
    )


async def _add_thread(
    seed: PrivateThreadSeed,
    thread_id: str,
) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


async def _claim_and_begin(
    seed: PrivateThreadSeed,
    *,
    worker_id: uuid.UUID,
    run_id: str,
    execution_domain_affinity: str | None = None,
) -> JobClaim:
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=300,
            execution_domain_affinity=execution_domain_affinity,
        )
        assert claim is not None
        assert claim.run_id == run_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a_scope,
            run_id=run_id,
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
        )
        return claim


async def _add_source_output(
    seed: PrivateThreadSeed,
    *,
    thread_id: str,
    source_run_id: str,
    logical_path: str,
    content: bytes,
) -> PrivateFileRow:
    async with seed.factory() as session, session.begin():
        row = PrivateFileRow(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            thread_id=thread_id,
            kind="output",
            logical_path=logical_path,
            media_type="text/x-python",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status="ready",
            version=1,
            created_by_run_id=source_run_id,
        )
        session.add(row)
        await session.flush()
        return row


async def _seed_active_model(seed: PrivateThreadSeed) -> None:
    model_id = uuid.UUID(TEST_MODEL_REF)
    async with seed.factory() as session, session.begin():
        await seed_system_model_config(
            session,
            model_id=model_id,
            owner_user_id=str(seed.owner_a.user_id),
            display_name="Approval output E2E model",
            provider_model="test-model",
            supports_vision=True,
        )


class _DeterministicLocalSandbox:
    id = "local-run:e2e:continuation"

    def __init__(self, *, output_path: str, output_content: bytes) -> None:
        self._output_path = output_path
        self._output_content = output_content
        self.executed: list[str] = []

    def resolve_command_for_execution(self, command: str) -> str:
        return command.replace(
            "/mnt/user-data/workspace",
            "/continuation/private/workspace",
        )

    def get_execution_shell(self) -> str:
        return "/bin/zsh"

    def execute_prepared_command_result(
        self,
        command: str,
        *,
        shell: str,
        env: dict[str, str] | None = None,
        prepared_base_env: dict[str, str] | None = None,
        timeout: float | None = None,
        spawn_deadline_monotonic: float | None = None,
        spawn_authorization_guard: Callable[[], float] | None = None,
    ) -> SimpleNamespace:
        del env, prepared_base_env, timeout
        if spawn_authorization_guard is not None:
            spawn_deadline_monotonic = spawn_authorization_guard()
        assert spawn_deadline_monotonic is not None
        assert shell == "/bin/zsh"
        self.executed.append(command)
        return SimpleNamespace(
            output="bubble sort verified\n",
            stdout="bubble sort verified\n",
            stderr="",
            exit_code=0,
        )

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
        excluded_root_names: tuple[str, ...] = (),
    ) -> list[SimpleNamespace]:
        del max_entries
        if root == "/mnt/user-data/workspace":
            # Reproduce the partial symlink tree left by a failed
            # ``python -m venv /mnt/user-data/workspace/.venv`` command.
            if ".venv" in excluded_root_names:
                return []
            return [
                SimpleNamespace(
                    file_type="directory",
                    path="/mnt/user-data/workspace/.venv",
                    size=96,
                ),
                SimpleNamespace(
                    file_type="symlink",
                    path="/mnt/user-data/workspace/.venv/bin/python3",
                    size=16,
                ),
            ]
        assert root == "/mnt/user-data/outputs"
        return [
            SimpleNamespace(
                file_type="regular",
                path=self._output_path,
                size=len(self._output_content),
            )
        ]

    def open_regular_file(self, path: str) -> io.BytesIO:
        assert path == self._output_path
        return io.BytesIO(self._output_content)

    @staticmethod
    def read_regular_file(handle: io.BytesIO, size: int) -> bytes:
        return handle.read(size)

    @staticmethod
    def close_regular_file(handle: io.BytesIO) -> None:
        handle.close()


class _DeterministicFileAuthority:
    sandbox_id = _DeterministicLocalSandbox.id

    @staticmethod
    def thread_data_paths() -> dict[str, str]:
        return {
            "workspace_path": "/mnt/user-data/workspace",
            "uploads_path": "/mnt/user-data/uploads",
            "outputs_path": "/mnt/user-data/outputs",
        }


class _LifecycleHooks:
    """No-op quota/audit hooks required at the continuation admission seam."""

    async def reserve_concurrent_run(self, *args, **kwargs) -> None:
        del args, kwargs

    async def release_concurrent_run(self, *args, **kwargs) -> None:
        del args, kwargs

    async def run_admitted(self, *args, **kwargs) -> None:
        del args, kwargs

    async def run_cancel_requested(self, *args, **kwargs) -> None:
        del args, kwargs

    async def run_terminal(self, *args, **kwargs) -> None:
        del args, kwargs


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deferred_output_reaches_artifact_and_success_after_one_frozen_execution(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    output_content = b"def bubble_sort(values):\n    return sorted(values)\n"
    logical_path = "outputs/bubble_sort.py"
    virtual_path = "/mnt/user-data/outputs/bubble_sort.py"
    config = _app_config(migrated_postgres_database_url)
    policy = HostExecutionProviderPolicySnapshot.from_app_config(config)
    domain = _execution_domain()
    hooks = _LifecycleHooks()
    try:
        await _seed_active_model(seed)
        await _add_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="approval-output-e2e",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

        admission = PrivateRunAdmissionService(
            seed.factory,
            model_catalog=SystemModelCatalogService(seed.factory),
            runtime_policy=SystemRuntimePolicyService,
            quota=hooks,
            audit=hooks,
        )
        source = await admission.admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=source_run_id,
                kwargs={"input": {"messages": []}},
            ),
        )
        source_claim = await _claim_and_begin(
            seed,
            worker_id=worker_id,
            run_id=source_run_id,
        )
        source_output = await _add_source_output(
            seed,
            thread_id=thread_id,
            source_run_id=source_run_id,
            logical_path=logical_path,
            content=output_content,
        )
        plan = HostExecutionPlan(
            source_tool_call_id="call-bubble-sort",
            source_run_id=source_run_id,
            source_thread_id=thread_id,
            description="run bubble sort verification",
            requested_command=("python /mnt/user-data/workspace/bubble_sort.py"),
            effective_command=("python /source/private/workspace/bubble_sort.py"),
            shell="/bin/zsh",
            cwd="/source/private/workspace",
            timeout_seconds=60,
            agent_path=("lead",),
        )
        source_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=source_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=policy,
            execution_domain=domain,
        )
        staged = await source_port.request_host_execution(plan)
        assert staged.status == "pending"
        assert staged.approval_id is not None
        approval_id = uuid.UUID(staged.approval_id)
        await source_port.seal_suspended_approval_marker(str(approval_id))

        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=source_run_id,
                job_id=source_claim.job_id,
                lease_token=source_claim.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=source_claim,
                succeeded=True,
                suspended_approval_id=str(approval_id),
                request_ttl_seconds=300,
            )

        async with seed.factory() as session:
            approval = await session.get(ExecutionApprovalRequestRow, approval_id)
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            source_run = await session.get(RunRow, source_run_id)
            source_job = await session.get(JobRow, source.job.job_id)
            assert approval is not None and approval.status == "pending"
            assert obligation is not None and obligation.status == "deferred"
            assert source_run is not None and source_run.status == "success"
            assert source_job is not None and source_job.status == "succeeded"

        decision = await ExecutionApprovalService(
            seed.factory,
            admission=admission,
            provider_policy=policy,
            quota=hooks,
            run_audit=hooks,
        ).decide(
            seed.owner_a,
            thread_id=thread_id,
            source_run_id=source_run_id,
            approval_id=approval_id,
            decision="allow_once",
            expected_version=2,
            idempotency_key=uuid.uuid4(),
        )
        assert decision.approval is not None
        continuation = decision.approval["continuation_run"]
        assert continuation is not None
        continuation_run_id = continuation["run_id"]
        continuation_claim = await _claim_and_begin(
            seed,
            worker_id=worker_id,
            run_id=continuation_run_id,
            execution_domain_affinity=domain.affinity,
        )
        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=continuation_claim,
        )
        continuation_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=continuation_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=policy,
            execution_domain=domain,
            continuation_approval_id=str(approval_id),
            retry_safety_boundary=boundary,
        )
        sandbox = _DeterministicLocalSandbox(
            output_path=virtual_path,
            output_content=output_content,
        )
        monkeypatch.setattr(
            "deerflow.runtime.host_execution_runner.get_sandbox_provider",
            lambda: SimpleNamespace(get=lambda sandbox_id: sandbox if sandbox_id == _DeterministicLocalSandbox.id else None),
        )
        runtime_context = {
            "app_config": config,
            "thread_id": thread_id,
            "run_id": continuation_run_id,
            "private_scope": seed.owner_a_scope,
            "__authorization_boundary": boundary,
            "__file_authority": _DeterministicFileAuthority(),
        }
        hidden_receipt = await execute_frozen_host_execution_continuation(
            approval_port=continuation_port,
            app_config=config,
            runtime_context=runtime_context,
            file_authority=_DeterministicFileAuthority(),
            graph_input={"messages": []},
            continuation_required=True,
        )
        assert len(sandbox.executed) == 1
        assert "already been executed once" in hidden_receipt["messages"][0]["content"]
        assert virtual_path in hidden_receipt["messages"][0]["content"]
        assert boundary.ambiguous_side_effect is False

        def provider_must_not_be_read() -> object:
            raise AssertionError("durable receipt replay must not access a provider")

        monkeypatch.setattr(
            "deerflow.runtime.host_execution_runner.get_sandbox_provider",
            provider_must_not_be_read,
        )
        replay_port = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=continuation_claim,
            thread_id=thread_id,
            request_ttl_seconds=300,
            provider_policy=policy,
            execution_domain=domain,
            continuation_approval_id=str(approval_id),
        )
        replayed_receipt = await execute_frozen_host_execution_continuation(
            approval_port=replay_port,
            app_config=config,
            runtime_context={
                "thread_id": thread_id,
                "run_id": continuation_run_id,
            },
            file_authority=None,
            graph_input={"messages": []},
            continuation_required=True,
        )
        assert "bubble sort verified" in replayed_receipt["messages"][0]["content"]
        assert len(sandbox.executed) == 1

        await replay_port.record_output_delivery_intent(
            (virtual_path,),
            tool_call_id="present-bubble-sort",
        )
        async with seed.factory() as session:
            recorded_intent = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            assert recorded_intent is not None
            assert recorded_intent.status == "intent_recorded"
            assert recorded_intent.intent_tool_call_id == "present-bubble-sort"
            assert recorded_intent.intent_private_json == {
                "schema_version": 1,
                "logical_paths": [logical_path],
            }
        manifest = AuthorityManifest(
            entries=(
                AuthorityManifestEntry(
                    file_id=source_output.id,
                    logical_path=source_output.logical_path,
                    kind=source_output.kind,
                    media_type=source_output.media_type,
                    size=source_output.size,
                    sha256=source_output.sha256,
                    version=source_output.version,
                ),
            ),
            run_id=continuation_run_id,
        )
        finalization = await PrivateFileFinalizer(
            seed.factory,
            output_delivery_port=replay_port,
        ).finalize(
            PrivateFileRunScope(
                seed.owner_a,
                thread_id=thread_id,
                run_id=continuation_run_id,
                authorization_boundary=boundary,
            ),
            manifest,
            sandbox,
            presented_paths=(virtual_path,),
        )
        assert len(finalization.artifacts) == 1

        settlement = PrivateRunJobHandler(
            seed.factory,
            executor=SimpleNamespace(),
        )._settlement(
            continuation_claim,
            AgentExecutionResult.succeeded(),
            scope=seed.owner_a_scope,
        )
        await settlement.commit()

        async with seed.factory() as session:
            approval = await session.get(ExecutionApprovalRequestRow, approval_id)
            obligation = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_id,
            )
            receipt_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ExecutionApprovalResultReceiptRow)
                .where(
                    ExecutionApprovalResultReceiptRow.approval_id == approval_id,
                )
            )
            artifact = await session.scalar(
                sa.select(PrivateArtifactRow).where(
                    PrivateArtifactRow.project_id == seed.owner_a.project_id,
                    PrivateArtifactRow.owner_user_id == str(seed.owner_a.user_id),
                    PrivateArtifactRow.thread_id == thread_id,
                    PrivateArtifactRow.run_id == continuation_run_id,
                    PrivateArtifactRow.file_id == source_output.id,
                    PrivateArtifactRow.deleted_at.is_(None),
                )
            )
            continuation_run = await session.get(RunRow, continuation_run_id)
            continuation_job = await session.get(
                JobRow,
                continuation_claim.job_id,
            )
            assert approval is not None and approval.status == "finished"
            assert receipt_count == 1
            assert artifact is not None
            assert obligation is not None and obligation.status == "delivered"
            assert obligation.satisfied_artifact_id == artifact.id
            assert obligation.intent_tool_call_id == "present-bubble-sort"
            assert obligation.intent_private_json == {
                "schema_version": 1,
                "logical_paths": [logical_path],
            }
            assert continuation_run is not None
            assert continuation_run.status == "success"
            assert continuation_job is not None
            assert continuation_job.status == "succeeded"
            assert continuation_job.retry_safety == "safe"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pending_approval_in_one_thread_does_not_block_another_thread_run(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_a = str(uuid.uuid4())
    thread_b = str(uuid.uuid4())
    run_a = str(uuid.uuid4())
    run_b = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    config = _app_config(migrated_postgres_database_url)
    policy = HostExecutionProviderPolicySnapshot.from_app_config(config)
    domain = _execution_domain()
    hooks = _LifecycleHooks()
    try:
        await _add_thread(seed, thread_a)
        await _add_thread(seed, thread_b)
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="approval-thread-isolation",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )

        source_a = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_a,
            PrivateRunCreate(
                run_id=run_a,
                kwargs={"input": {"messages": []}},
            ),
        )
        claim_a = await _claim_and_begin(
            seed,
            worker_id=worker_id,
            run_id=run_a,
        )
        await _add_source_output(
            seed,
            thread_id=thread_a,
            source_run_id=run_a,
            logical_path="outputs/thread-a.txt",
            content=b"thread A output\n",
        )
        port_a = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim_a,
            thread_id=thread_a,
            request_ttl_seconds=300,
            provider_policy=policy,
            execution_domain=domain,
        )
        staged = await port_a.request_host_execution(
            HostExecutionPlan(
                source_tool_call_id="call-thread-a",
                source_run_id=run_a,
                source_thread_id=thread_a,
                description="leave thread A awaiting approval",
                requested_command="python /mnt/user-data/workspace/a.py",
                effective_command="python /thread-a/private/workspace/a.py",
                shell="/bin/zsh",
                cwd="/thread-a/private/workspace",
                timeout_seconds=60,
                agent_path=("lead",),
            )
        )
        assert staged.approval_id is not None
        approval_a_id = uuid.UUID(staged.approval_id)
        await port_a.seal_suspended_approval_marker(str(approval_a_id))
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=run_a,
                job_id=claim_a.job_id,
                lease_token=claim_a.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=claim_a,
                succeeded=True,
                suspended_approval_id=str(approval_a_id),
                request_ttl_seconds=300,
            )

        admitted_b = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_b,
            PrivateRunCreate(
                run_id=run_b,
                kwargs={"input": {"messages": []}},
            ),
        )
        assert admitted_b.run.status == "pending"
        claim_b = await _claim_and_begin(
            seed,
            worker_id=worker_id,
            run_id=run_b,
        )
        ordinary_port_b = WorkerHostExecutionApprovalPort(
            seed.factory,
            context=seed.owner_a,
            claim=claim_b,
            thread_id=thread_b,
            request_ttl_seconds=300,
            provider_policy=policy,
            execution_domain=domain,
        )
        assert (await ordinary_port_b.claim_frozen_host_execution()).status == "not_applicable"
        async with seed.factory() as session, session.begin():
            await PrivateRunRepository(session).settle_execution(
                scope=seed.owner_a_scope,
                run_id=run_b,
                job_id=claim_b.job_id,
                lease_token=claim_b.lease_token,
                outcome="succeeded",
            )
            await settle_staged_execution_approvals(
                session,
                claim=claim_b,
                succeeded=True,
                suspended_approval_id=None,
                request_ttl_seconds=300,
            )

        service = ExecutionApprovalService(
            seed.factory,
            admission=PrivateRunAdmissionService(seed.factory),
            provider_policy=policy,
            quota=hooks,
            run_audit=hooks,
        )
        active_a = await service.active(seed.owner_a, thread_a)
        active_b = await service.active(seed.owner_a, thread_b)
        async with seed.factory() as session:
            approval_a = await session.get(
                ExecutionApprovalRequestRow,
                approval_a_id,
            )
            obligation_a = await session.get(
                ExecutionApprovalOutputDeliveryObligationRow,
                approval_a_id,
            )
            persisted_run_b = await session.get(RunRow, run_b)
            persisted_job_b = await session.get(JobRow, admitted_b.job.job_id)
            assert approval_a is not None and approval_a.status == "pending"
            assert obligation_a is not None
            assert obligation_a.status == "deferred"
            assert persisted_run_b is not None and persisted_run_b.status == "success"
            assert persisted_job_b is not None
            assert persisted_job_b.status == "succeeded"
        assert active_a.approval is not None
        assert active_a.approval["approval_id"] == str(approval_a_id)
        assert active_a.approval["status"] == "pending"
        assert active_b.approval is None
        assert source_a.job.job_id == claim_a.job_id
    finally:
        await seed.engine.dispose()
