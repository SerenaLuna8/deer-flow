"""Real-PostgreSQL gates for the durable, Worker-owned Skill Builder Agent."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import text
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database

from app.gateway.routers.private_work import reconnect_private_run_stream
from app.private_work.errors import (
    PrivateWorkNotFound,
    PrivateWorkRunQuotaExceeded,
)
from app.private_work.retention_purge import purge_private_scope
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.context import ProjectContext
from app.reliability.execution import (
    AgentExecutionResult,
    PrivateRunJobHandler,
)
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.errors import AssetConflict, AssetRunQuotaExceeded
from app.shared_assets.skill_builder_contract import (
    SkillBuilderCandidateFileList,
    SkillBuilderCandidateFileUpsert,
    SkillBuilderCandidateFinalize,
)
from app.shared_assets.skill_builder_run_admission import (
    SkillBuilderRunAdmission,
    SkillBuilderRunAdmissionService,
)
from app.shared_assets.skill_design_generation import SkillBuilderDependencySnapshot
from app.shared_assets.skill_design_service import (
    CancelSkillDesignSession,
    CreateSkillDesignSession,
    SkillDesignMessageTurn,
    SkillDesignService,
    SubmitSkillDesignTurn,
)
from app.system_runtime_settings import SystemRuntimePolicyService
from app.system_settings import SystemModelCatalogService
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.events.stream import PostgresStreamBridge
from deerflow.sandbox.sandbox import AuthorizationRevoked


class _RecordingQuota:
    def __init__(self, *, reject_reservation: bool = False) -> None:
        self.reject_reservation = reject_reservation
        self.reserved: list[str] = []
        self.released: list[str] = []

    async def reserve_concurrent_run(self, _session, context, run) -> None:  # type: ignore[no-untyped-def]
        if self.reject_reservation:
            raise PrivateWorkRunQuotaExceeded(context.request_id)
        self.reserved.append(run.run_id)

    async def release_concurrent_run(
        self,
        _session,
        _scope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        assert request_id
        self.released.append(run_id)


class _RecordingAudit:
    def __init__(self) -> None:
        self.admitted: list[str] = []
        self.cancel_requested: list[str] = []
        self.terminal: list[tuple[str, str]] = []

    async def run_admitted(self, _session, _context, run, job) -> None:  # type: ignore[no-untyped-def]
        assert job.run_id == run.run_id
        self.admitted.append(run.run_id)

    async def run_cancel_requested(
        self,
        _session,
        _context,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None:
        assert isinstance(job_id, uuid.UUID)
        self.cancel_requested.append(run_id)

    async def run_terminal(
        self,
        _session,
        _scope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        assert isinstance(job_id, uuid.UUID)
        assert job_type == "private_run"
        assert public_error_code is None or public_error_code.isupper()
        assert request_id
        self.terminal.append((run_id, status))


class _NeverExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _execution, _authority):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("recovered Builder terminal must not rerun the graph")


def _project_context(seed: PrivateThreadSeed, *, request_id: str) -> ProjectContext:
    source = seed.owner_a
    return ProjectContext(
        user_id=source.user_id,
        project_id=source.project_id,
        membership_id=source.membership_id,
        role=source.role,
        capabilities=source.capabilities,
        membership_version=source.membership_version,
        request_id=request_id,
    )


async def _seed_default_model(seed: PrivateThreadSeed) -> None:
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    logical_name = f"builder-pg-{model_id.hex}"
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO system_model_configs
                (id,logical_name,display_name,description,status,current_version_id,
                 revision,sort_order,created_by_user_id,updated_by_user_id)
                VALUES (:id,:name,'Builder PG model','Builder PG model','active',
                        NULL,1,0,:owner,:owner)"""
            ),
            {
                "id": model_id,
                "name": logical_name,
                "owner": str(seed.owner_a.user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO system_model_config_versions
                (id,model_config_id,version_number,provider_adapter,provider_model,
                 settings,supports_thinking,supports_reasoning_effort,
                 supports_vision,credential_id,credential_version_id,
                 credential_env_key,payload_checksum,supersedes_version_id,
                 created_by_user_id)
                VALUES (:id,:model,1,'codex_cli',:name,'{}'::jsonb,false,false,
                        false,NULL,NULL,NULL,:checksum,NULL,:owner)"""
            ),
            {
                "id": version_id,
                "model": model_id,
                "name": logical_name,
                "checksum": "b" * 64,
                "owner": str(seed.owner_a.user_id),
            },
        )
        await connection.execute(
            text(
                """UPDATE system_model_configs
                   SET current_version_id=:version
                 WHERE id=:model"""
            ),
            {"version": version_id, "model": model_id},
        )
        await connection.execute(
            text(
                """UPDATE system_model_catalog_state
                   SET default_model_config_id=:model, revision=revision+1,
                       updated_by_user_id=:owner
                 WHERE id=1"""
            ),
            {"model": model_id, "owner": str(seed.owner_a.user_id)},
        )


async def _environment(
    database_url: str,
    *,
    quota: _RecordingQuota | None = None,
    audit: _RecordingAudit | None = None,
) -> tuple[
    PrivateThreadSeed,
    ProjectContext,
    SkillDesignService,
    _RecordingQuota,
    _RecordingAudit,
]:
    seed = await seed_private_thread_database(database_url)
    await bootstrap_service.bootstrap_system_assets(seed.factory)
    await _seed_default_model(seed)
    context = _project_context(seed, request_id="a" * 32)
    selected_quota = quota or _RecordingQuota()
    selected_audit = audit or _RecordingAudit()
    admission = SkillBuilderRunAdmissionService(
        seed.factory,
        model_catalog=SystemModelCatalogService(seed.factory),
        runtime_policy=SystemRuntimePolicyService,
        quota=selected_quota,
        audit=selected_audit,
    )
    service = SkillDesignService(
        seed.factory,
        run_admission=admission,
        quota=selected_quota,
        audit=selected_audit,
    )
    return seed, context, service, selected_quota, selected_audit


def _message_turn(
    *,
    message: str,
    revision: int,
    key: str,
) -> SubmitSkillDesignTurn:
    return SubmitSkillDesignTurn(
        input=SkillDesignMessageTurn(kind="message", message=message),
        expected_revision=revision,
        idempotency_key=key,
    )


async def _claim_and_begin(seed: PrivateThreadSeed, *, now: datetime):
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="skill-builder-pg",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=60,
            now=now,
        )
        assert claim is not None
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
            now=now,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a.resource_scope,
            run_id=claim.run_id or "",
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
            now=now,
        )
    return worker_id, claim


async def _reclaim_and_begin(
    seed: PrivateThreadSeed,
    *,
    worker_id: uuid.UUID,
    now: datetime,
):
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=60,
            now=now,
        )
        assert claim is not None
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
            now=now,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a.resource_scope,
            run_id=claim.run_id or "",
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
            now=now,
        )
    return claim


def _candidate_chunks() -> tuple[str, str]:
    return (
        "---\nname: integration-skill\n",
        ("description: Durable Builder integration test\n---\n\n# Integration Skill\n\nExecute the requested workflow.\n"),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_durable_builder_run_replay_retry_delta_cancel_and_delete_link(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, service, quota, audit = await _environment(
        migrated_postgres_database_url,
    )
    try:
        design = await service.create(
            context,
            CreateSkillDesignSession(
                slug="integration-skill",
                display_name="Integration Skill",
                idempotency_key="create-integration-skill",
            ),
        )
        first_command = _message_turn(
            message="生成一个可验证的集成测试 Skill",
            revision=design.revision,
            key="turn-one",
        )

        first = await service.submit_turn(context, design.id, first_command)
        replay = await service.submit_turn(context, design.id, first_command)

        assert isinstance(first, SkillBuilderRunAdmission)
        assert replay == first
        assert quota.reserved == [first.run_id]
        assert audit.admitted == [first.run_id]

        async with seed.factory() as session, session.begin():
            run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == first.run_id))).scalar_one()
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.run_id == first.run_id,
                    )
                )
            ).scalar_one()
            first_operation_id = operation.id
            thread = await session.get(ThreadMetaRow, first.thread_id)
            assert thread is not None
            assert thread.thread_kind == "skill_builder"
            assert run.metadata_json == {}
            initial_payload = json.loads(
                run.kwargs_json["input"]["messages"][0]["content"],
            )
            assert initial_payload["conversation"]["mode"] == "initial"
            assert "生成一个可验证" in initial_payload["conversation"]["brief"]
            assert (
                await PrivateThreadRepository(session).search(
                    scope=seed.owner_a.resource_scope,
                )
                == ()
            )

        private_runs = PrivateRunService(seed.factory)
        visible = await private_runs.get(
            seed.owner_a,
            first.thread_id,
            first.run_id,
        )
        assert visible.run_id == first.run_id
        with pytest.raises(PrivateWorkNotFound):
            await private_runs.get(
                seed.owner_b,
                first.thread_id,
                first.run_id,
            )

        bridge = PostgresStreamBridge(
            seed.factory,
            run_event_notify_enabled=False,
        )
        stream_request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    private_run_service=private_runs,
                    private_stream_bridge=bridge,
                ),
            ),
            headers={},
        )
        stream_response = await reconnect_private_run_stream(
            uuid.UUID(first.thread_id),
            uuid.UUID(first.run_id),
            stream_request,  # type: ignore[arg-type]
            seed.owner_a,
        )
        assert stream_response.status_code == 200
        await stream_response.body_iterator.aclose()
        with pytest.raises(HTTPException) as wrong_owner_stream:
            await reconnect_private_run_stream(
                uuid.UUID(first.thread_id),
                uuid.UUID(first.run_id),
                stream_request,  # type: ignore[arg-type]
                seed.owner_b,
            )
        assert wrong_owner_stream.value.status_code == 404

        started_at = datetime.now(UTC)
        worker_id, first_claim = await _claim_and_begin(seed, now=started_at)
        async with seed.factory() as session, session.begin():
            assert await PrivateRunJobHandler._runtime_kind_in_session(
                session,
                first_claim,
                lock_builder=True,
            ) == ("skill_builder", first.thread_id)

        handler = PrivateRunJobHandler(
            seed.factory,
            executor=object(),  # type: ignore[arg-type]
            quota=quota,
            audit=audit,
        )
        retryable = handler._settlement(
            first_claim,
            AgentExecutionResult.failed("TRANSIENT_BUILDER_FAILURE"),
            scope=seed.owner_a.resource_scope,
        )
        await retryable.commit()
        async with seed.factory() as session:
            after_retry = await session.get(SkillDesignSessionRow, design.id)
            retry_operation = await session.get(
                SkillDesignOperationRow,
                first_operation_id,
            )
            retry_run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == first.run_id))).scalar_one()
            assert after_retry is not None
            assert after_retry.status == "generating"
            assert retry_operation is not None
            assert retry_operation.status == "in_progress"
            assert retry_run.status == "pending"

        second_claim = await _reclaim_and_begin(
            seed,
            worker_id=worker_id,
            now=started_at + timedelta(seconds=5),
        )
        assert second_claim.run_id == first.run_id
        draft_sink = service.terminal_sink(seed.owner_a, second_claim)
        empty_draft = await draft_sink.list_candidate_files(
            SkillBuilderCandidateFileList(),
        )
        assert empty_draft.draft_checksum is None
        assert empty_draft.items == ()

        first_chunk, second_chunk = _candidate_chunks()
        first_mutation = await draft_sink.upsert_candidate_file(
            SkillBuilderCandidateFileUpsert(
                path="SKILL.md",
                media_type="text/markdown",
                content=first_chunk,
                mode="replace",
                expected_draft_checksum=None,
                expected_file_size_bytes=0,
                expected_file_sha256=None,
            ),
        )
        assert first_mutation.file is not None
        final_mutation = await draft_sink.upsert_candidate_file(
            SkillBuilderCandidateFileUpsert(
                path="SKILL.md",
                media_type="text/markdown",
                content=second_chunk,
                mode="append",
                expected_draft_checksum=first_mutation.draft_checksum,
                expected_file_size_bytes=first_mutation.file.size_bytes,
                expected_file_sha256=first_mutation.file.sha256,
            ),
        )
        assert final_mutation.draft_checksum is not None
        candidate_request = SkillBuilderCandidateFinalize(
            expected_draft_checksum=final_mutation.draft_checksum,
            summary="候选 Skill 已生成",
        )
        dependencies = SkillBuilderDependencySnapshot(
            draft_checksum=final_mutation.draft_checksum,
        )
        receipt = await draft_sink.finalize_candidate(
            candidate_request,
            dependencies,
        )
        assert receipt.terminal == "candidate"
        async with seed.factory() as session:
            crash_window_operation = await session.get(
                SkillDesignOperationRow,
                first_operation_id,
            )
            crash_window_run = (
                await session.execute(
                    sa.select(RunRow).where(RunRow.run_id == first.run_id),
                )
            ).scalar_one()
            assert crash_window_operation is not None
            assert crash_window_operation.status == "completed"
            assert crash_window_run.status == "running"

        replay_sink = service.terminal_sink(seed.owner_a, second_claim)
        assert (
            await replay_sink.finalize_candidate(
                candidate_request,
                dependencies,
            )
        ).terminal == "candidate"
        with pytest.raises((AuthorizationRevoked, AssetConflict)):
            await replay_sink.finalize_candidate(
                candidate_request.model_copy(
                    update={"summary": "不同的终端重放"},
                ),
                dependencies,
            )

        recovery_claim = await _reclaim_and_begin(
            seed,
            worker_id=worker_id,
            now=started_at + timedelta(seconds=70),
        )
        never_executor = _NeverExecutor()
        recovery_handler = PrivateRunJobHandler(
            seed.factory,
            executor=never_executor,
            quota=quota,
            audit=audit,
        )
        succeeded = await recovery_handler(
            recovery_claim,
            JobLeaseAuthority(
                seed.factory,
                recovery_claim,
                lease_seconds=60,
            ),
        )
        assert succeeded.outcome.status == "succeeded"
        assert never_executor.calls == 0
        await succeeded.commit()
        await succeeded.commit()

        async with seed.factory() as session:
            recovered_run = (
                await session.execute(
                    sa.select(RunRow).where(RunRow.run_id == first.run_id),
                )
            ).scalar_one()
            recovered_job = await session.get(JobRow, recovery_claim.job_id)
            terminals = (
                (
                    await session.execute(
                        sa.select(RunEventRow).where(
                            RunEventRow.project_id == seed.owner_a.project_id,
                            RunEventRow.owner_user_id == str(seed.owner_a.user_id),
                            RunEventRow.thread_id == first.thread_id,
                            RunEventRow.run_id == first.run_id,
                            RunEventRow.category == "stream",
                            RunEventRow.event_type == "stream.end",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert recovered_run.status == "success"
            assert recovered_job is not None
            assert recovered_job.status == "succeeded"
            assert len(terminals) == 1
            assert json.loads(terminals[0].content) == {"status": "completed"}
            assert terminals[0].event_metadata["stream_terminal"] is True
        assert quota.released.count(first.run_id) == 1
        assert audit.terminal.count((first.run_id, "success")) == 1

        ready = await service.get(context, design.id)
        assert ready.status.value == "draft_ready"
        assert ready.revision == 3

        second_command = _message_turn(
            message="只调整错误处理说明",
            revision=ready.revision,
            key="turn-two",
        )
        second = await service.submit_turn(context, design.id, second_command)
        second_replay = await service.submit_turn(context, design.id, second_command)
        assert isinstance(second, SkillBuilderRunAdmission)
        assert second_replay == second
        assert second.thread_id == first.thread_id

        async with seed.factory() as session:
            second_run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == second.run_id))).scalar_one()
            continuation_payload = json.loads(
                second_run.kwargs_json["input"]["messages"][0]["content"],
            )
            assert continuation_payload["conversation"] == {
                "mode": "continuation",
                "turn": "只调整错误处理说明",
            }
            assert "生成一个可验证" not in json.dumps(
                continuation_payload,
                ensure_ascii=False,
            )

        cancelled = await service.cancel(
            context,
            design.id,
            CancelSkillDesignSession(
                expected_revision=4,
                idempotency_key="cancel-second-run",
            ),
        )
        cancel_replay = await service.cancel(
            context,
            design.id,
            CancelSkillDesignSession(
                expected_revision=4,
                idempotency_key="cancel-second-run",
            ),
        )
        assert cancelled.status.value == "cancelled"
        assert cancel_replay == cancelled
        assert quota.released.count(second.run_id) == 1
        assert audit.cancel_requested == [second.run_id]
        assert (second.run_id, "interrupted") in audit.terminal

        await private_runs.delete(
            seed.owner_a,
            first.thread_id,
            first.run_id,
        )
        async with seed.factory() as session:
            deleted_run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == first.run_id))).scalar_one()
            durable_operation = await session.get(
                SkillDesignOperationRow,
                first_operation_id,
            )
            assert deleted_run.status == "deleted"
            assert durable_operation is not None
            assert durable_operation.status == "completed"
            assert durable_operation.run_id is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_builder_admission_quota_failure_rolls_back_the_whole_turn(
    migrated_postgres_database_url: str,
) -> None:
    quota = _RecordingQuota(reject_reservation=True)
    seed, context, service, _quota, audit = await _environment(
        migrated_postgres_database_url,
        quota=quota,
    )
    try:
        design = await service.create(
            context,
            CreateSkillDesignSession(
                slug="quota-rollback-skill",
                display_name="Quota Rollback Skill",
                idempotency_key="create-quota-rollback",
            ),
        )
        with pytest.raises(AssetRunQuotaExceeded):
            await service.submit_turn(
                context,
                design.id,
                _message_turn(
                    message="这个 admission 必须整体回滚",
                    revision=design.revision,
                    key="quota-rejected-turn",
                ),
            )

        async with seed.factory() as session:
            row = await session.get(SkillDesignSessionRow, design.id)
            assert row is not None
            assert row.status == "interviewing"
            assert row.revision == 1
            assert await session.scalar(sa.select(sa.func.count()).select_from(SkillDesignOperationRow).where(SkillDesignOperationRow.session_id == design.id)) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(ThreadMetaRow).where(ThreadMetaRow.thread_id == str(design.thread_id))) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(RunRow).where(RunRow.thread_id == str(design.thread_id))) == 0
            assert await session.scalar(sa.select(sa.func.count()).select_from(JobRow).where(JobRow.project_id == context.project_id)) == 0
        assert audit.admitted == []
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_retention_accepts_a_linked_builder_run_without_fk_leak(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, service, _quota, _audit = await _environment(
        migrated_postgres_database_url,
    )
    try:
        design = await service.create(
            context,
            CreateSkillDesignSession(
                slug="retention-builder-skill",
                display_name="Retention Builder Skill",
                idempotency_key="create-retention-builder",
            ),
        )
        admitted = await service.submit_turn(
            context,
            design.id,
            _message_turn(
                message="生成后由保留策略清理",
                revision=design.revision,
                key="retention-linked-turn",
            ),
        )
        assert isinstance(admitted, SkillBuilderRunAdmission)

        async with seed.factory() as session, session.begin():
            await purge_private_scope(
                session,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
            )

        async with seed.factory() as session:
            assert await session.get(SkillDesignSessionRow, design.id) is None
            assert await session.scalar(sa.select(sa.func.count()).select_from(SkillDesignOperationRow).where(SkillDesignOperationRow.session_id == design.id)) == 0
            retained_run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == admitted.run_id))).scalar_one()
            retained_job = (await session.execute(sa.select(JobRow).where(JobRow.run_id == admitted.run_id))).scalar_one()
            assert retained_job.status == "queued"
            assert retained_run.kwargs_json == {}
            assert retained_run.metadata_json == {}
    finally:
        await seed.engine.dispose()
