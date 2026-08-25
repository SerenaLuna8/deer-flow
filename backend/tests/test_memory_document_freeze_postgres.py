from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from support.private_thread_seed import seed_private_thread_database
from support.run_closure import add_sealed_test_run
from support.system_model_seed import seed_system_model_config

from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.errors import PrivateWorkConflict
from app.private_work.memory_dream_service import MemoryDreamAdmissionService
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.system_runtime_settings.models import RuntimePolicySection
from app.system_runtime_settings.repository import SystemRuntimePolicyRepository
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.validation import canonical_policy_payload
from app.system_settings.repository import SystemModelRepository
from app.worker.memory_dream import MemoryDreamJobHandler
from deerflow.agents.memory.dream import render_empty_memory_document
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobOwnerRef, JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentConflict,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    memory_document_digest,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyVersionRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


def _owner_ref(_owner_user_id: str) -> JobOwnerRef:
    return JobOwnerRef(key_id="memory-freeze-test", hmac_hex="f" * 64)


def _jobs(session) -> JobRepository:
    return JobRepository(session, owner_ref_hasher=_owner_ref)


async def _replace_memory_document_policy(
    session,
    *,
    sections: tuple[str, ...],
    actor_user_id: str,
) -> uuid.UUID:
    repository = SystemRuntimePolicyRepository(session)
    catalog = await repository.catalog_state(for_update=True)
    policy, previous = await repository.current(
        RuntimePolicySection.MEMORY_DOCUMENT,
        for_update=True,
    )
    canonical = canonical_policy_payload(
        RuntimePolicySection.MEMORY_DOCUMENT,
        {"sections": list(sections)},
    )
    now = datetime.now(UTC)
    next_revision = int(policy.revision) + 1
    version = SystemRuntimePolicyVersionRow(
        id=uuid.uuid4(),
        section=RuntimePolicySection.MEMORY_DOCUMENT.value,
        version_number=next_revision,
        schema_version=canonical.schema_version,
        value=canonical.value,
        payload_checksum=canonical.checksum,
        supersedes_version_id=previous.id,
        created_by_user_id=actor_user_id,
        created_at=now,
    )
    await repository.add_version(policy, version)
    policy.revision = next_revision
    policy.updated_by_user_id = actor_user_id
    policy.updated_at = now
    catalog.revision = int(catalog.revision) + 1
    catalog.updated_by_user_id = actor_user_id
    catalog.updated_at = now
    await session.flush()
    return version.id


async def _seed_memory_model(
    session,
    *,
    actor_user_id: str,
) -> tuple[str, uuid.UUID]:
    model_id = uuid.uuid4()
    await seed_system_model_config(
        session,
        model_id=model_id,
        owner_user_id=actor_user_id,
        display_name="Memory freeze model",
        provider_model="memory-freeze",
    )
    return str(model_id), model_id


async def _replace_agent_runtime_memory_model(
    session,
    *,
    model_name: str,
    actor_user_id: str,
) -> None:
    repository = SystemRuntimePolicyRepository(session)
    catalog = await repository.catalog_state(for_update=True)
    policy, previous = await repository.current(
        RuntimePolicySection.AGENT_RUNTIME,
        for_update=True,
    )
    value = dict(previous.value)
    value["memory"] = {**dict(value["memory"]), "model_name": model_name}
    canonical = canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)
    now = datetime.now(UTC)
    next_revision = int(policy.revision) + 1
    version = SystemRuntimePolicyVersionRow(
        id=uuid.uuid4(),
        section=RuntimePolicySection.AGENT_RUNTIME.value,
        version_number=next_revision,
        schema_version=canonical.schema_version,
        value=canonical.value,
        payload_checksum=canonical.checksum,
        supersedes_version_id=previous.id,
        created_by_user_id=actor_user_id,
        created_at=now,
    )
    await repository.add_version(policy, version)
    policy.revision = next_revision
    policy.updated_by_user_id = actor_user_id
    policy.updated_at = now
    catalog.revision = int(catalog.revision) + 1
    catalog.updated_by_user_id = actor_user_id
    catalog.updated_at = now
    await session.flush()


async def _add_pending_history(
    session,
    *,
    scope: MemoryDocumentScope,
    thread_id: str,
    model_config_id: uuid.UUID,
    preference_version: int,
    text: str,
) -> None:
    digest = hashlib.sha256(f"{scope.owner_user_id}:{thread_id}".encode()).hexdigest()
    session.add(
        MemoryHistoryEntryRow(
            id=uuid.uuid4(),
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            thread_id=thread_id,
            origin="snip",
            source_checkpoint_id=f"source-{thread_id}",
            committed_checkpoint_id=f"committed-{thread_id}",
            source_digest=digest,
            status="pending",
            tagged_text=text,
            content_digest=hashlib.sha256(text.encode()).hexdigest(),
            preference_version=preference_version,
            snip_prompt_version="snip-prompt-v1",
            summary_model_config_id=model_config_id,
            summary_model_payload_checksum="a" * 64,
            created_at=datetime.now(UTC),
        )
    )


async def _add_run(
    session,
    *,
    project_id: uuid.UUID,
    owner_user_id: str,
    agent_id: uuid.UUID,
    thread_id: str,
    run_id: str,
    trace_seed: str,
) -> None:
    await add_sealed_test_run(
        session,
        RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(agent_id),
            owner_user_id=owner_user_id,
            status="pending",
            model_name="test-model",
            multitask_strategy="reject",
            metadata_json={},
            kwargs_json={},
            origin_trace_id=hashlib.sha256(trace_seed.encode()).hexdigest()[:32],
            project_id=project_id,
        ),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_memory_document_sections_freeze_across_policy_drift_and_snapshots(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    sections_a = ("A 协作方式", "A 架构边界", "A 当前目标")
    sections_b = ("B 偏好", "B 交付约束")
    scope_a = MemoryDocumentScope(
        project_id=seed.owner_a.project_id,
        owner_user_id=str(seed.owner_a.user_id),
    )
    scope_b = MemoryDocumentScope(
        project_id=seed.owner_b.project_id,
        owner_user_id=str(seed.owner_b.user_id),
    )
    now = datetime.now(UTC)
    worker_id = uuid.uuid4()
    admission_service = MemoryDreamAdmissionService(
        job_repository_builder=_jobs,
    )
    try:
        async with seed.factory() as session, session.begin():
            model_name, model_config_id = await _seed_memory_model(
                session,
                actor_user_id=scope_a.owner_user_id,
            )
            await _replace_agent_runtime_memory_model(
                session,
                model_name=model_name,
                actor_user_id=scope_a.owner_user_id,
            )
            policy_a_version_id = await _replace_memory_document_policy(
                session,
                sections=sections_a,
                actor_user_id=scope_a.owner_user_id,
            )
            active_model = await SystemModelRepository(session).resolve_active_model(
                model_name,
                load_secret=False,
            )
            assert active_model is not None
            assert active_model.model.id == model_config_id
            preference_a = await AccountPersonalizationRepository(session).read_memory(scope_a.owner_user_id)
            preference_b = await AccountPersonalizationRepository(session).read_memory(scope_b.owner_user_id)
            await _add_pending_history(
                session,
                scope=scope_a,
                thread_id="freeze-a",
                model_config_id=active_model.model.id,
                preference_version=preference_a.version,
                text="- [durable] scope A fact",
            )
            await _add_pending_history(
                session,
                scope=scope_b,
                thread_id="freeze-b",
                model_config_id=active_model.model.id,
                preference_version=preference_b.version,
                text="- [durable] scope B fact",
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-freeze-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=now,
                    heartbeat_at=now,
                )
            )

        async with seed.factory() as session, session.begin():
            first_a = await admission_service.admit(
                session,
                scope_a,
                trigger="manual_dream",
                now=now,
            )
        assert first_a.disposition == "queued"
        assert first_a.job_id is not None

        async with seed.factory() as session, session.begin():
            document_a = await session.get(
                MemoryDocumentRow,
                (scope_a.project_id, scope_a.owner_user_id, scope_a.namespace),
            )
            assert document_a is not None
            assert tuple(document_a.sections) == sections_a
            assert document_a.sections_policy_version_id == policy_a_version_id
            assert document_a.content == render_empty_memory_document(sections_a)
            with pytest.raises(MemoryDocumentConflict):
                await MemoryDocumentRepository(
                    session,
                    jobs=_jobs(session),
                ).restore_version(
                    scope_a,
                    target_version=1,
                    expected_current_version=0,
                    expected_sections=sections_a,
                    max_tokens=8_000,
                    now=now,
                )

        async with seed.factory() as session, session.begin():
            policy_b_version_id = await _replace_memory_document_policy(
                session,
                sections=sections_b,
                actor_user_id=scope_a.owner_user_id,
            )

        async with seed.factory() as session, session.begin():
            repository = MemoryDocumentRepository(session, jobs=_jobs(session))
            queued_a = await repository.load_dream_work(scope_a, first_a.job_id)
            assert queued_a is not None
            assert queued_a.sections == sections_a
            assert queued_a.sections_policy_version_id == policy_a_version_id
            assert MemoryDreamJobHandler._input(queued_a, max_tokens=8_000).sections == sections_a
            assert await session.scalar(sa.select(JobRow.status).where(JobRow.id == first_a.job_id)) == "queued"

            claim = await _jobs(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_dream"}),
                lease_seconds=60,
                now=now + timedelta(seconds=1),
            )
            assert claim is not None and claim.job_id == first_a.job_id
            assert await _jobs(session).mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=1),
            )
            running_a = await repository.load_dream_work(scope_a, first_a.job_id)
            assert running_a is not None
            assert running_a.sections == sections_a
            assert running_a.sections_policy_version_id == policy_a_version_id
            assert await repository.release_dream(
                scope_a,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=2),
                cancelled=True,
            )

        async with seed.factory() as session, session.begin():
            continued_a = await admission_service.admit(
                session,
                scope_a,
                trigger="manual_dream",
                now=now + timedelta(seconds=3),
            )
        async with seed.factory() as session, session.begin():
            first_b = await admission_service.admit(
                session,
                scope_b,
                trigger="manual_dream",
                now=now + timedelta(seconds=3),
            )
        assert continued_a.disposition == first_b.disposition == "queued"
        assert continued_a.job_id is not None and first_b.job_id is not None

        async with seed.factory() as session, session.begin():
            repository = MemoryDocumentRepository(session, jobs=_jobs(session))
            work_a = await repository.load_dream_work(scope_a, continued_a.job_id)
            work_b = await repository.load_dream_work(scope_b, first_b.job_id)
            assert work_a is not None and work_b is not None
            assert work_a.sections == sections_a
            assert work_a.sections_policy_version_id == policy_a_version_id
            assert work_b.sections == sections_b
            assert work_b.sections_policy_version_id == policy_b_version_id
            assert MemoryDreamJobHandler._input(work_a, max_tokens=8_000).sections == sections_a
            assert MemoryDreamJobHandler._input(work_b, max_tokens=8_000).sections == sections_b
            assert await session.scalar(sa.select(JobRow.status).where(JobRow.id == continued_a.job_id)) == "queued"
            assert await session.scalar(sa.select(JobRow.status).where(JobRow.id == first_b.job_id)) == "queued"

            claim = await _jobs(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_dream"}),
                lease_seconds=60,
                now=now + timedelta(seconds=4),
            )
            assert claim is not None and claim.job_id == continued_a.job_id
            assert await _jobs(session).mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=4),
            )
            assert await repository.release_dream(
                scope_a,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                now=now + timedelta(seconds=5),
                cancelled=True,
            )

            document_a = await session.get(
                MemoryDocumentRow,
                (scope_a.project_id, scope_a.owner_user_id, scope_a.namespace),
            )
            document_b = await session.get(
                MemoryDocumentRow,
                (scope_b.project_id, scope_b.owner_user_id, scope_b.namespace),
            )
            assert document_a is not None and document_b is not None
            document_a.version = 1
            document_a.dream_cursor = 99
            document_b.version = 1
            session.add(
                MemoryDocumentVersionRow(
                    project_id=scope_a.project_id,
                    owner_user_id=scope_a.owner_user_id,
                    namespace=scope_a.namespace,
                    version=1,
                    content=document_a.content,
                    content_digest=document_a.content_digest,
                    unified_diff="",
                    trigger="restore",
                    dream_job_id=None,
                    history_from=None,
                    history_to=None,
                    history_count=None,
                    prompt_version=None,
                    needs_review=False,
                    created_at=now + timedelta(seconds=5),
                )
            )
            with pytest.raises(MemoryDocumentConflict):
                await repository.restore_version(
                    scope_a,
                    target_version=1,
                    expected_current_version=0,
                    expected_sections=sections_a,
                    max_tokens=8_000,
                    now=now + timedelta(seconds=5),
                )

        restored_a = await PrivateMemoryDocumentService(seed.factory).restore(
            seed.owner_a,
            target_version=1,
            expected_current_version=1,
        )
        assert restored_a.version == 2
        assert restored_a.content == render_empty_memory_document(sections_a)

        async with seed.factory() as session:
            document_a = await session.get(
                MemoryDocumentRow,
                (scope_a.project_id, scope_a.owner_user_id, scope_a.namespace),
            )
            assert document_a is not None
            assert tuple(document_a.sections) == sections_a
            assert document_a.sections_policy_version_id == policy_a_version_id
            assert document_a.dream_cursor == 99

        thread_a = str(uuid.uuid4())
        thread_b = str(uuid.uuid4())
        run_a = str(uuid.uuid4())
        continuation_a = str(uuid.uuid4())
        bad_a = str(uuid.uuid4())
        run_b = str(uuid.uuid4())
        async with seed.factory() as session, session.begin():
            for thread_id, owner_user_id in (
                (thread_a, scope_a.owner_user_id),
                (thread_b, scope_b.owner_user_id),
            ):
                session.add(
                    ThreadMetaRow(
                        thread_id=thread_id,
                        assistant_id=str(seed.project_agent_id),
                        owner_user_id=owner_user_id,
                        display_name="Memory sections freeze",
                        status="idle",
                        metadata_json={},
                        project_id=scope_a.project_id,
                        agent_asset_id=seed.project_agent_id,
                        agent_scope="project",
                    )
                )
            await session.flush()
            for owner_user_id, thread_id, run_id in (
                (scope_a.owner_user_id, thread_a, run_a),
                (scope_a.owner_user_id, thread_a, continuation_a),
                (scope_a.owner_user_id, thread_a, bad_a),
                (scope_b.owner_user_id, thread_b, run_b),
            ):
                await _add_run(
                    session,
                    project_id=scope_a.project_id,
                    owner_user_id=owner_user_id,
                    agent_id=seed.project_agent_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_seed=run_id,
                )

        snapshots = RunSnapshotRepository(seed.factory)
        async with seed.factory() as session, session.begin():
            locked_agent_policy = await SystemRuntimePolicyService.lock_agent_runtime_for_admission(session)
            await snapshots._admit_memory_context_snapshot(
                session,
                seed.owner_a,
                run_id=run_a,
                locked_policy=locked_agent_policy,
            )
        async with seed.factory() as session, session.begin():
            locked_agent_policy = await SystemRuntimePolicyService.lock_agent_runtime_for_admission(session)
            await snapshots._admit_memory_context_snapshot(
                session,
                seed.owner_b,
                run_id=run_b,
                locked_policy=locked_agent_policy,
            )
        async with seed.factory() as session, session.begin():
            locked_agent_policy = await SystemRuntimePolicyService.lock_agent_runtime_for_admission(session)
            await snapshots._admit_memory_context_snapshot(
                session,
                seed.owner_a,
                thread_id=thread_a,
                run_id=continuation_a,
                continuation_source_run_id=run_a,
                locked_policy=locked_agent_policy,
            )

        async with seed.factory() as session:
            rows = {row.run_id: row for row in (await session.execute(sa.select(RunMemoryContextSnapshotRow).where(RunMemoryContextSnapshotRow.run_id.in_((run_a, continuation_a, run_b))))).scalars()}
            assert tuple(rows[run_a].sections) == sections_a
            assert tuple(rows[continuation_a].sections) == sections_a
            assert tuple(rows[run_b].sections) == sections_b

        async with seed.factory() as session, session.begin():
            document_a = await session.get(
                MemoryDocumentRow,
                (scope_a.project_id, scope_a.owner_user_id, scope_a.namespace),
            )
            assert document_a is not None
            document_a.content = render_empty_memory_document(sections_b)
            document_a.content_digest = memory_document_digest(document_a.content)

        with pytest.raises(PrivateWorkConflict):
            async with seed.factory() as session, session.begin():
                locked_agent_policy = await SystemRuntimePolicyService.lock_agent_runtime_for_admission(session)
                await snapshots._admit_memory_context_snapshot(
                    session,
                    seed.owner_a,
                    run_id=bad_a,
                    locked_policy=locked_agent_policy,
                )
    finally:
        await seed.engine.dispose()
