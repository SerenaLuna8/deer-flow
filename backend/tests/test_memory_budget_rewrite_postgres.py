from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from support.private_thread_seed import seed_private_thread_database
from support.system_model_seed import seed_system_model_config

from app.private_work.memory_dream_service import MemoryDreamAdmissionService
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedAgentRuntimePolicy,
    RuntimePolicySection,
)
from app.system_settings.repository import SystemModelRepository
from app.worker.memory_dream import MemoryDreamJobHandler
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    EMPTY_MEMORY_DOCUMENT,
    estimate_memory_tokens,
    validate_memory_document,
)
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    BUDGET_REWRITE_HISTORY_DIGEST,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    memory_document_digest,
)
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow


class _InjectionAudit:
    def __init__(self) -> None:
        self.skipped_run_ids: list[str] = []

    async def memory_injection_skipped(
        self,
        _session,
        *,
        project_id,
        run_id,
        request_id,
    ) -> None:
        assert isinstance(project_id, uuid.UUID)
        assert isinstance(request_id, str) and request_id
        self.skipped_run_ids.append(run_id)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_budget_rewrite_candidate_limit_applies_after_exact_token_check(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    budget = 100
    false_candidate = EMPTY_MEMORY_DOCUMENT + "\n\n" + ("a" * (budget + 1))
    true_candidate = EMPTY_MEMORY_DOCUMENT + "\n\n" + ("真" * (budget + 1))
    assert len(false_candidate) > budget
    assert estimate_memory_tokens(false_candidate) <= budget
    assert estimate_memory_tokens(true_candidate) > budget
    now = datetime.now(UTC)

    try:
        async with seed.factory() as session, session.begin():
            sections_policy_version_id = await session.scalar(
                sa.select(SystemRuntimePolicyRow.current_version_id).where(
                    SystemRuntimePolicyRow.section == "memory_document",
                )
            )
            assert isinstance(sections_policy_version_id, uuid.UUID)
            documents = (
                *(
                    (
                        f"false-{index:03d}",
                        false_candidate,
                        now + timedelta(seconds=index),
                    )
                    for index in range(101)
                ),
                ("true-oldest", true_candidate, now + timedelta(seconds=101)),
                ("true-next", true_candidate, now + timedelta(seconds=102)),
            )
            session.add_all(
                MemoryDocumentRow(
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    namespace=namespace,
                    content=content,
                    content_digest=memory_document_digest(content),
                    sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                    sections_policy_version_id=sections_policy_version_id,
                    version=1,
                    dream_cursor=0,
                    updated_at=updated_at,
                )
                for namespace, content, updated_at in documents
            )

        async with seed.factory() as session:
            repository = MemoryDocumentRepository(
                session,
            )
            first = await repository.list_budget_rewrite_scope_page(
                budget_tokens=budget,
                admissible_roles=("admin", "editor", "runner", "channel_guest"),
                limit=2,
            )
            assert first.scopes == ()
            assert first.next_cursor is not None
            second = await repository.list_budget_rewrite_scope_page(
                budget_tokens=budget,
                admissible_roles=("admin", "editor", "runner", "channel_guest"),
                cursor=first.next_cursor,
                limit=2,
            )

        assert tuple(scope.namespace for scope in second.scopes) == (
            "true-oldest",
            "true-next",
        )
        assert second.next_cursor is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_budget_rewrite_discovery_keyset_pages_past_one_hundred_scopes(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    budget = 100
    true_candidate = EMPTY_MEMORY_DOCUMENT + "\n\n" + ("真" * (budget + 1))
    assert estimate_memory_tokens(true_candidate) > budget
    now = datetime.now(UTC)
    viewer_id = uuid.uuid4()

    try:
        async with seed.factory() as session, session.begin():
            sections_policy_version_id = await session.scalar(
                sa.select(SystemRuntimePolicyRow.current_version_id).where(
                    SystemRuntimePolicyRow.section == "memory_document",
                )
            )
            assert isinstance(sections_policy_version_id, uuid.UUID)
            disabled_owner = await session.get(UserRow, str(seed.owner_b.user_id))
            assert disabled_owner is not None
            disabled_owner.memory_enabled = False
            session.add(
                UserRow(
                    id=str(viewer_id),
                    email=f"{viewer_id}@example.com",
                    system_role="user",
                )
            )
            # Persist the referenced user before adding its membership.  These
            # independently mapped rows have no ORM relationship that can make
            # SQLAlchemy order one combined pending flush for us.
            await session.flush()
            session.add(
                ProjectMembershipRow(
                    id=uuid.uuid4(),
                    project_id=seed.owner_a.project_id,
                    user_id=str(viewer_id),
                    role="viewer",
                    status="active",
                    version=1,
                )
            )
            # Establish the synthetic viewer's membership authority before
            # seeding a document that references both foreign keys.
            await session.flush()
            eligible_documents = tuple(
                MemoryDocumentRow(
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    namespace=f"eligible-{index:03d}",
                    content=true_candidate,
                    content_digest=memory_document_digest(true_candidate),
                    sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                    sections_policy_version_id=sections_policy_version_id,
                    version=1,
                    dream_cursor=0,
                    updated_at=now + timedelta(seconds=index),
                )
                for index in range(102)
            )
            session.add_all(
                (
                    *eligible_documents,
                    MemoryDocumentRow(
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(seed.owner_b.user_id),
                        namespace="disabled-owner",
                        content=true_candidate,
                        content_digest=memory_document_digest(true_candidate),
                        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                        sections_policy_version_id=sections_policy_version_id,
                        version=1,
                        dream_cursor=0,
                        updated_at=now - timedelta(seconds=2),
                    ),
                    MemoryDocumentRow(
                        project_id=seed.owner_a.project_id,
                        owner_user_id=str(viewer_id),
                        namespace="viewer-owner",
                        content=true_candidate,
                        content_digest=memory_document_digest(true_candidate),
                        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                        sections_policy_version_id=sections_policy_version_id,
                        version=1,
                        dream_cursor=0,
                        updated_at=now - timedelta(seconds=1),
                    ),
                )
            )

        async with seed.factory() as session:
            repository = MemoryDocumentRepository(session)
            first = await repository.list_budget_rewrite_scope_page(
                budget_tokens=budget,
                admissible_roles=("admin", "editor", "runner", "channel_guest"),
                limit=100,
            )
            assert first.next_cursor is not None
            second = await repository.list_budget_rewrite_scope_page(
                budget_tokens=budget,
                admissible_roles=("admin", "editor", "runner", "channel_guest"),
                cursor=first.next_cursor,
                limit=100,
            )

        assert [scope.namespace for scope in first.scopes] == [f"eligible-{index:03d}" for index in range(100)]
        assert [scope.namespace for scope in second.scopes] == [
            "eligible-100",
            "eligible-101",
        ]
        assert second.next_cursor is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_budget_rewrite_restores_real_postgres_snapshot_injection(
    migrated_postgres_database_url: str,
) -> None:
    """Prove the budget-rewrite rescue path without crossing the external model boundary."""

    seed = await seed_private_thread_database(migrated_postgres_database_url)
    scope = MemoryDocumentScope(
        project_id=seed.owner_a.project_id,
        owner_user_id=str(seed.owner_a.user_id),
    )
    thread_id = str(uuid.uuid4())
    before_run_id = str(uuid.uuid4())
    after_run_id = str(uuid.uuid4())
    model_id = uuid.uuid4()
    model_ref = str(model_id)
    worker_id = uuid.uuid4()
    now = datetime.now(UTC)

    try:
        async with seed.factory() as session:
            policy, policy_revision = await SystemRuntimePolicyMaterializer.materialize_current_with_revision_in_session(
                session,
                RuntimePolicySection.AGENT_RUNTIME,
            )
        assert isinstance(policy, AgentRuntimePolicyValue)
        budget = policy.memory.max_injection_tokens
        over_budget_content = EMPTY_MEMORY_DOCUMENT.replace(
            "# 项目背景",
            "# 项目背景\n\n" + ("超" * (budget + 200)),
        )
        rewritten_content = EMPTY_MEMORY_DOCUMENT.replace(
            "# 项目背景",
            "# 项目背景\n\n- 保留当前有效约束。",
        )
        assert estimate_memory_tokens(over_budget_content) > budget
        assert validate_memory_document(rewritten_content, budget) == rewritten_content

        async with seed.engine.begin() as connection:
            await seed_system_model_config(
                connection,
                model_id=model_id,
                owner_user_id=scope.owner_user_id,
                display_name="Budget rewrite test",
                provider_model="budget-rewrite-test",
            )

        async with seed.factory() as session, session.begin():
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=scope.owner_user_id,
                    display_name="Budget rewrite closure",
                    status="idle",
                    metadata_json={},
                    project_id=scope.project_id,
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            await session.flush()
            for run_id, trace in (
                (before_run_id, "a" * 32),
                (after_run_id, "b" * 32),
            ):
                session.add(
                    RunRow(
                        run_id=run_id,
                        thread_id=thread_id,
                        assistant_id=str(seed.project_agent_id),
                        owner_user_id=scope.owner_user_id,
                        status="pending",
                        model_name=model_ref,
                        multitask_strategy="reject",
                        metadata_json={},
                        kwargs_json={},
                        origin_trace_id=trace,
                        project_id=scope.project_id,
                    )
                )
            sections_policy_version_id = await session.scalar(
                sa.select(SystemRuntimePolicyRow.current_version_id).where(
                    SystemRuntimePolicyRow.section == "memory_document",
                )
            )
            assert isinstance(sections_policy_version_id, uuid.UUID)
            session.add(
                MemoryDocumentRow(
                    project_id=scope.project_id,
                    owner_user_id=scope.owner_user_id,
                    namespace=scope.namespace,
                    content=over_budget_content,
                    content_digest=memory_document_digest(over_budget_content),
                    sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                    sections_policy_version_id=sections_policy_version_id,
                    version=1,
                    dream_cursor=0,
                    updated_at=now,
                )
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="budget-rewrite-test",
                    capabilities_json=["memory_dream"],
                    max_concurrent_jobs=1,
                    draining=False,
                    started_at=now,
                    heartbeat_at=now,
                )
            )

        memory_service = PrivateMemoryDocumentService(seed.factory)
        before_state, before_advisory = await memory_service.get_with_injection_advisory(seed.owner_a)
        assert before_state.pending_count == 0
        assert before_state.document.version == 1
        assert before_advisory.status == "skipped_over_budget"

        locked_policy = LockedAgentRuntimePolicy(
            policy_version_id=uuid.uuid4(),
            revision=policy_revision,
            schema_version=1,
            payload_checksum="d" * 64,
            value=policy,
        )
        injection_audit = _InjectionAudit()
        snapshot_repository = RunSnapshotRepository(
            seed.factory,
            audit=injection_audit,
        )
        async with seed.factory() as session, session.begin():
            await snapshot_repository._admit_memory_context_snapshot(
                session,
                seed.owner_a,
                run_id=before_run_id,
                locked_policy=locked_policy,
            )
        assert injection_audit.skipped_run_ids == [before_run_id]
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    sa.select(RunMemoryContextSnapshotRow).where(
                        RunMemoryContextSnapshotRow.run_id == before_run_id,
                    )
                )
                is None
            )

        class Admission(MemoryDreamAdmissionService):
            @staticmethod
            async def _platform_runtime(session, *, create_document):
                assert create_document is False
                runtime_model = await SystemModelRepository(session).resolve_active_model(model_ref, load_secret=True)
                assert runtime_model is not None
                return policy, policy_revision, runtime_model, None

        async with seed.factory() as session, session.begin():
            admission = await Admission().admit(
                session,
                scope,
                trigger="manual_dream",
                now=now,
            )
        assert admission.disposition == "queued"
        assert admission.admission_kind == "budget_rewrite"
        assert admission.history_count == 0
        assert admission.job_id is not None

        async with seed.factory() as session:
            dream_run = await session.get(MemoryDreamRunRow, admission.job_id)
            assert dream_run is not None
            assert dream_run.trigger == "budget_rewrite"
            assert dream_run.history_count == 0
            assert dream_run.history_from is dream_run.history_to is None
            assert dream_run.history_digest == BUDGET_REWRITE_HISTORY_DIGEST

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"memory_dream"}),
                lease_seconds=60,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            assert claim is not None and claim.job_id == admission.job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )

        async with seed.factory() as session:
            work = await MemoryDocumentRepository(session).load_dream_work(
                scope,
                claim.job_id,
            )
        assert work is not None
        assert work.trigger == "budget_rewrite"
        assert work.history == ()
        assert work.history_count == 0
        assert work.history_digest == BUDGET_REWRITE_HISTORY_DIGEST

        handler = MemoryDreamJobHandler(
            seed.factory,
            app_config=None,
            runner_factory=lambda _model: None,
        )
        settlement = handler._success_settlement(
            claim,
            work=work,
            content=rewritten_content,
            max_tokens=budget,
            episode_retention_days=0,
        )
        await settlement.commit()

        after_state, after_advisory = await memory_service.get_with_injection_advisory(seed.owner_a)
        assert after_advisory.status == "eligible"
        assert after_state.pending_count == 0
        assert after_state.document.version == 2
        assert after_state.document.content == rewritten_content

        async with seed.factory() as session, session.begin():
            await snapshot_repository._admit_memory_context_snapshot(
                session,
                seed.owner_a,
                run_id=after_run_id,
                locked_policy=locked_policy,
            )

        async with seed.factory() as session:
            snapshot = await session.scalar(
                sa.select(RunMemoryContextSnapshotRow).where(
                    RunMemoryContextSnapshotRow.run_id == after_run_id,
                )
            )
            version = await session.get(
                MemoryDocumentVersionRow,
                (scope.project_id, scope.owner_user_id, scope.namespace, 2),
            )
            job = await session.get(JobRow, claim.job_id)
            episode_count = await session.scalar(sa.select(sa.func.count()).select_from(MemoryEpisodeRow))
        assert snapshot is not None
        assert snapshot.document_version == 2
        assert snapshot.content == rewritten_content
        assert snapshot.content_digest == hashlib.sha256(rewritten_content.encode("utf-8")).hexdigest()
        assert injection_audit.skipped_run_ids == [before_run_id]
        assert version is not None
        assert version.trigger == "budget_rewrite"
        assert version.history_count == 0
        assert version.history_from is version.history_to is None
        assert job is not None and job.status == "succeeded"
        assert episode_count == 0
    finally:
        await seed.engine.dispose()
