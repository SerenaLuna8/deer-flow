from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from support.private_thread_seed import TEST_MODEL_REF, seed_private_thread_database

import app.private_work.snapshot_repository as snapshot_module
from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict
from app.private_work.memory_authority import PrivateRunMemoryAuthority
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRecord,
    PrivateRunRepository,
)
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedAgentRuntimePolicy,
)
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    EMPTY_MEMORY_DOCUMENT,
    render_empty_memory_document,
)
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobRepository,
    JobScope,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import SystemRuntimePolicyRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

LEAD_MODEL_REF = "00000000-0000-4000-8000-000000000305"
VISION_MODEL_REF = "00000000-0000-4000-8000-000000000306"
CATALOG_DEFAULT_MODEL_REF = "00000000-0000-4000-8000-000000000307"


class _Result:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, document) -> None:
        self.document = document
        self.execute_calls = 0
        self.added: list[object] = []

    async def execute(self, _statement):
        self.execute_calls += 1
        return _Result(self.document)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _SequenceSession(_Session):
    def __init__(self, *results) -> None:
        super().__init__(None)
        self.results = list(results)

    async def execute(self, _statement):
        self.execute_calls += 1
        return _Result(self.results.pop(0))


class _PreferenceRepository:
    def __init__(self, _session, *, enabled: bool) -> None:
        self.enabled = enabled
        self.read_calls = 0

    async def read_memory(self, _user_id, *, for_update: bool = False):
        assert for_update is True
        self.read_calls += 1
        return SimpleNamespace(memory_enabled=self.enabled, version=5)


def _context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="memory-admission-test",
        )
    )


def _policy(
    *,
    enabled: bool = True,
    max_tokens: int = 2_000,
    vision_model_name: str | None = None,
):
    return LockedAgentRuntimePolicy(
        policy_version_id=uuid.uuid4(),
        revision=4,
        schema_version=1,
        payload_checksum="a" * 64,
        value=AgentRuntimePolicyValue(
            memory={
                "enabled": enabled,
                "max_injection_tokens": max_tokens,
            },
            vision_bridge={"model_name": vision_model_name},
        ),
    )


@pytest.mark.asyncio
async def test_run_admission_freezes_the_complete_current_document() -> None:
    sections = ("协作方式", "架构边界", "当前目标")
    content = render_empty_memory_document(sections)
    document = SimpleNamespace(
        version=8,
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
        sections=list(sections),
    )
    session = _Session(document)
    context = _context()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        run_id="run-1",
        locked_policy=_policy(),
    )

    assert session.execute_calls == 1
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, RunMemoryContextSnapshotRow)
    assert row.run_id == "run-1"
    assert row.document_version == 8
    assert row.content == content
    assert row.content_digest == document.content_digest
    assert row.sections == list(sections)


@pytest.mark.asyncio
async def test_clarification_continuation_inherits_the_source_run_snapshot() -> None:
    sections = ("协作方式", "架构边界", "当前目标")
    content = render_empty_memory_document(sections) + "\n\n- source snapshot"
    source = SimpleNamespace(
        document_version=3,
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
        sections=list(sections),
    )
    session = _SequenceSession("source-run", source)
    context = _context()
    preference = _PreferenceRepository(None, enabled=False)

    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda _session: preference,
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        thread_id="thread-1",
        run_id="answer-run",
        continuation_source_run_id="source-run",
        locked_policy=_policy(),
    )

    assert session.execute_calls == 2
    assert preference.read_calls == 1
    assert len(session.added) == 1
    inherited = session.added[0]
    assert isinstance(inherited, RunMemoryContextSnapshotRow)
    assert inherited.run_id == "answer-run"
    assert inherited.document_version == source.document_version
    assert inherited.content == source.content
    assert inherited.content_digest == source.content_digest
    assert inherited.sections == list(sections)


@pytest.mark.asyncio
async def test_run_admission_fails_closed_when_content_does_not_match_frozen_sections() -> None:
    document = SimpleNamespace(
        version=8,
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest=hashlib.sha256(EMPTY_MEMORY_DOCUMENT.encode()).hexdigest(),
        sections=["协作方式", "架构边界"],
    )
    session = _Session(document)
    context = _context()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
    )

    with pytest.raises(PrivateWorkConflict):
        await repository._admit_memory_context_snapshot(
            session,
            context,
            run_id="run-section-drift",
            locked_policy=_policy(),
        )

    assert session.added == []


@pytest.mark.asyncio
async def test_clarification_continuation_keeps_no_snapshot_when_source_has_none() -> None:
    session = _SequenceSession("source-run", None)
    context = _context()
    preference = _PreferenceRepository(None, enabled=True)

    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda _session: preference,
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        thread_id="thread-1",
        run_id="answer-run",
        continuation_source_run_id="source-run",
        locked_policy=_policy(),
    )

    assert session.execute_calls == 2
    assert preference.read_calls == 1
    assert session.added == []


@pytest.mark.asyncio
async def test_clarification_continuation_rejects_an_unscoped_source_run() -> None:
    session = _SequenceSession(None)
    context = _context()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
    )

    with pytest.raises(PrivateWorkConflict):
        await repository._admit_memory_context_snapshot(
            session,
            context,
            thread_id="thread-1",
            run_id="answer-run",
            continuation_source_run_id="other-thread-run",
            locked_policy=_policy(),
        )

    assert session.execute_calls == 1
    assert session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_enabled", "account_enabled"),
    [(False, True), (True, False)],
)
async def test_run_admission_skips_snapshot_when_memory_is_disabled(
    platform_enabled: bool,
    account_enabled: bool,
) -> None:
    session = _Session(object())
    context = _context()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=account_enabled,
        ),
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        run_id="run-2",
        locked_policy=_policy(enabled=platform_enabled),
    )

    assert session.added == []
    assert session.execute_calls == 0


class _InjectionAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def memory_injection_skipped(
        self,
        session,
        *,
        project_id,
        run_id,
        request_id,
    ) -> None:
        self.calls.append(
            {
                "session": session,
                "project_id": project_id,
                "run_id": run_id,
                "request_id": request_id,
            }
        )


@pytest.mark.asyncio
async def test_run_admission_degrades_an_over_budget_document_to_a_skip() -> None:
    content = EMPTY_MEMORY_DOCUMENT + "\n" + ("超" * 200)
    session = _Session(
        SimpleNamespace(
            version=2,
            content=content,
            content_digest=hashlib.sha256(content.encode()).hexdigest(),
            sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
        )
    )
    context = _context()
    audit = _InjectionAudit()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
        audit=audit,
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        run_id="run-3",
        locked_policy=_policy(max_tokens=100),
    )

    assert session.added == []
    (skipped,) = audit.calls
    assert skipped["session"] is session
    assert skipped["project_id"] == context.project_id
    assert skipped["run_id"] == "run-3"
    assert skipped["request_id"] == context.request_id


@pytest.mark.asyncio
async def test_run_admission_still_fails_closed_on_document_digest_drift() -> None:
    content = EMPTY_MEMORY_DOCUMENT
    session = _Session(
        SimpleNamespace(
            version=2,
            content=content,
            content_digest="b" * 64,
            sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
        )
    )
    context = _context()
    audit = _InjectionAudit()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
        audit=audit,
    )

    with pytest.raises(PrivateWorkConflict):
        await repository._admit_memory_context_snapshot(
            session,
            context,
            run_id="run-4",
            locked_policy=_policy(),
        )

    assert session.added == []
    assert audit.calls == []


@pytest.mark.asyncio
async def test_run_admission_fails_closed_when_over_budget_document_digest_drifts() -> None:
    content = EMPTY_MEMORY_DOCUMENT + "\n" + ("超" * 200)
    session = _Session(
        SimpleNamespace(
            version=2,
            content=content,
            content_digest="b" * 64,
            sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
        )
    )
    context = _context()
    audit = _InjectionAudit()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
        audit=audit,
    )

    with pytest.raises(PrivateWorkConflict):
        await repository._admit_memory_context_snapshot(
            session,
            context,
            run_id="run-over-budget-drift",
            locked_policy=_policy(max_tokens=100),
        )

    assert session.added == []
    assert audit.calls == []


@pytest.mark.asyncio
async def test_run_admission_rejects_an_invalid_injection_audit_port() -> None:
    with pytest.raises(TypeError):
        RunSnapshotRepository(
            lambda: None,
            audit=object(),
        )


@pytest.mark.asyncio
async def test_continuation_degrades_an_over_budget_inherited_snapshot() -> None:
    content = EMPTY_MEMORY_DOCUMENT + "\n" + ("超" * 200)
    source = SimpleNamespace(
        document_version=3,
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
    )
    session = _SequenceSession("source-run", source)
    context = _context()
    audit = _InjectionAudit()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
        audit=audit,
    )

    await repository._admit_memory_context_snapshot(
        session,
        context,
        thread_id="thread-1",
        run_id="answer-run",
        continuation_source_run_id="source-run",
        locked_policy=_policy(max_tokens=100),
    )

    assert session.added == []
    (skipped,) = audit.calls
    assert skipped["run_id"] == "answer-run"


@pytest.mark.asyncio
async def test_continuation_fails_closed_when_over_budget_snapshot_digest_drifts() -> None:
    content = EMPTY_MEMORY_DOCUMENT + "\n" + ("超" * 200)
    source = SimpleNamespace(
        document_version=3,
        content=content,
        content_digest="b" * 64,
        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
    )
    session = _SequenceSession("source-run", source)
    context = _context()
    audit = _InjectionAudit()
    repository = RunSnapshotRepository(
        lambda: None,
        personalization_repository_builder=lambda current: _PreferenceRepository(
            current,
            enabled=True,
        ),
        audit=audit,
    )

    with pytest.raises(PrivateWorkConflict):
        await repository._admit_memory_context_snapshot(
            session,
            context,
            thread_id="thread-1",
            run_id="answer-run",
            continuation_source_run_id="source-run",
            locked_policy=_policy(max_tokens=100),
        )

    assert session.added == []
    assert audit.calls == []


@pytest.mark.asyncio
async def test_run_admission_locks_models_before_user_memory_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    now = datetime.now(UTC)
    context = _context()
    thread_id = str(uuid.uuid4())
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=thread_id,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        origin_trace_id="a" * 32,
        error=None,
        model_name=None,
        created_at=now,
        updated_at=now,
    )

    class Session:
        def in_transaction(self) -> bool:
            return True

        def add_all(self, values) -> None:
            tuple(values)

        async def flush(self) -> None:
            return None

    class Runs:
        def __init__(self, _session) -> None:
            pass

        async def create(self, **_kwargs):
            return run

        async def update_admitted_execution_profile(self, **_kwargs):
            return True

        async def get(self, **_kwargs):
            return run

    class Threads:
        def __init__(self, _session) -> None:
            pass

        async def touch_activity(
            self,
            *,
            scope,
            thread_id,
            occurred_at,
            thread_kind,
        ):
            assert scope == context.resource_scope
            assert thread_id == run.thread_id
            assert occurred_at == run.created_at
            assert thread_kind == "chat"
            events.append("activity")
            return True

    class RuntimePolicy:
        async def lock_agent_runtime_for_admission(self, _session):
            events.append("policy")
            return _policy(vision_model_name=VISION_MODEL_REF)

        async def admit_run_snapshot(self, _session, **_kwargs):
            events.append("policy_snapshot")

    class Models:
        async def admit_model_snapshot(self, _session, *, purpose, **_kwargs):
            events.append(f"model:{purpose}")
            return SimpleNamespace(
                model_ref=(VISION_MODEL_REF if purpose == "vision" else LEAD_MODEL_REF),
                provider_adapter=("vision_bridge_fake" if purpose == "vision" else "openai"),
                provider_settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=purpose == "vision",
            )

    monkeypatch.setattr(snapshot_module, "AsyncSession", Session)
    monkeypatch.setattr(snapshot_module, "PrivateRunRepository", Runs)
    monkeypatch.setattr(
        snapshot_module,
        "PrivateThreadRepository",
        Threads,
        raising=False,
    )
    repository = RunSnapshotRepository(
        lambda: None,
        model_catalog=Models(),
        runtime_policy=RuntimePolicy(),
    )

    async def validate_closure(*_args, **_kwargs):
        events.append("asset")
        return [], [], {}, {}

    async def admit_memory(
        _session,
        _context,
        *,
        run_id,
        locked_policy,
        thread_id: str,
        continuation_source_run_id: str | None,
    ):
        assert run_id == run.run_id
        assert isinstance(locked_policy, LockedAgentRuntimePolicy)
        assert thread_id == run.thread_id
        assert continuation_source_run_id == "source-run"
        events.append("memory")

    monkeypatch.setattr(
        repository,
        "validate_agent_closure_in_session",
        validate_closure,
    )
    monkeypatch.setattr(
        repository,
        "_admit_memory_context_snapshot",
        admit_memory,
    )
    agent = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="b" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="",
            soul="",
            model_ref=LEAD_MODEL_REF,
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )

    await repository.create_run_with_snapshot_in_session(
        Session(),
        context,
        thread_id,
        PrivateRunCreate(
            run_id=run.run_id,
            follow_up_to_run_id="source-run",
        ),
        agent,
        continuation_source_run_id="source-run",
    )

    assert events == [
        "asset",
        "policy",
        "policy_snapshot",
        "model:lead",
        "model:title",
        "model:vision",
        "memory",
        "activity",
    ]


@pytest.mark.asyncio
async def test_run_admission_freezes_catalog_default_title_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted: list[tuple[str, str]] = []
    now = datetime.now(UTC)
    context = _context()
    thread_id = str(uuid.uuid4())
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=thread_id,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        origin_trace_id="a" * 32,
        error=None,
        model_name=None,
        created_at=now,
        updated_at=now,
    )

    class Session:
        def in_transaction(self) -> bool:
            return True

        def add_all(self, values) -> None:
            tuple(values)

        async def flush(self) -> None:
            return None

    class Runs:
        def __init__(self, _session) -> None:
            pass

        async def create(self, **_kwargs):
            return run

        async def update_admitted_execution_profile(self, **_kwargs):
            return True

        async def get(self, **_kwargs):
            return run

    class Threads:
        def __init__(self, _session) -> None:
            pass

        async def touch_activity(self, **_kwargs):
            return True

    class RuntimePolicy:
        async def lock_agent_runtime_for_admission(self, _session):
            return _policy(vision_model_name=VISION_MODEL_REF)

        async def admit_run_snapshot(self, _session, **_kwargs):
            return None

    class Models:
        async def admit_model_snapshot(self, _session, *, purpose, model_ref, **_kwargs):
            admitted.append((purpose, model_ref))
            return SimpleNamespace(
                model_ref=(CATALOG_DEFAULT_MODEL_REF if model_ref == "default" else model_ref),
                provider_adapter="openai",
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=purpose == "lead",
            )

    monkeypatch.setattr(snapshot_module, "AsyncSession", Session)
    monkeypatch.setattr(snapshot_module, "PrivateRunRepository", Runs)
    monkeypatch.setattr(
        snapshot_module,
        "PrivateThreadRepository",
        Threads,
        raising=False,
    )
    repository = RunSnapshotRepository(
        lambda: None,
        model_catalog=Models(),
        runtime_policy=RuntimePolicy(),
    )

    async def validate_closure(*_args, **_kwargs):
        return [], [], {}, {}

    async def admit_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        repository,
        "validate_agent_closure_in_session",
        validate_closure,
    )
    monkeypatch.setattr(
        repository,
        "_admit_memory_context_snapshot",
        admit_memory,
    )
    agent = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="b" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="",
            soul="",
            model_ref=LEAD_MODEL_REF,
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )

    await repository.create_run_with_snapshot_in_session(
        Session(),
        context,
        thread_id,
        PrivateRunCreate(run_id=run.run_id),
        agent,
    )

    assert ("lead", LEAD_MODEL_REF) in admitted
    assert ("title", "default") in admitted
    assert not any(purpose == "vision" for purpose, _model in admitted)


@pytest.mark.asyncio
async def test_postgres_snapshot_stays_frozen_and_reset_removes_it(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    content_v1 = EMPTY_MEMORY_DOCUMENT + "\n\n- 使用中文。"
    content_v2 = EMPTY_MEMORY_DOCUMENT + "\n\n- 使用中文并给出简洁结论。"
    scope = seed.owner_a.resource_scope
    try:
        async with seed.factory() as session, session.begin():
            sections_policy_version_id = await session.scalar(
                sa.select(SystemRuntimePolicyRow.current_version_id).where(
                    SystemRuntimePolicyRow.section == "memory_document",
                )
            )
            assert sections_policy_version_id is not None
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=scope.owner_user_id,
                    display_name="Memory snapshot",
                    status="idle",
                    metadata_json={},
                    project_id=uuid.UUID(scope.project_id),
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            await session.flush()
            session.add(
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=scope.owner_user_id,
                    status="pending",
                    model_name=TEST_MODEL_REF,
                    multitask_strategy="reject",
                    metadata_json={},
                    kwargs_json={},
                    origin_trace_id="b" * 32,
                    project_id=uuid.UUID(scope.project_id),
                )
            )
            session.add(
                MemoryDocumentRow(
                    project_id=uuid.UUID(scope.project_id),
                    owner_user_id=scope.owner_user_id,
                    namespace="default",
                    content=content_v1,
                    content_digest=hashlib.sha256(content_v1.encode()).hexdigest(),
                    sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
                    sections_policy_version_id=sections_policy_version_id,
                    version=1,
                    dream_cursor=0,
                )
            )

        repository = RunSnapshotRepository(seed.factory)
        async with seed.factory() as session, session.begin():
            await repository._admit_memory_context_snapshot(
                session,
                seed.owner_a,
                run_id=run_id,
                locked_policy=_policy(),
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="memory-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )
            job_id = await jobs.enqueue(
                EnqueueJob(
                    job_type="private_run",
                    scope=JobScope(
                        uuid.UUID(scope.project_id),
                        scope.owner_user_id,
                    ),
                    idempotency_key=hashlib.sha256(f"memory-authority:{run_id}".encode()).hexdigest(),
                    run_id=run_id,
                    occurrence_id=None,
                    max_attempts=3,
                    retry_safety="safe",
                    origin_trace_id="b" * 32,
                )
            )
            await PrivateRunRepository(session).attach_job(
                scope=scope,
                run_id=run_id,
                job_id=job_id,
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )

        authority = PrivateRunMemoryAuthority(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
            thread_id=thread_id,
            namespace="default",
            memory_config=MemoryConfig(
                enabled=True,
                max_injection_tokens=2_000,
            ),
        )
        frozen = await authority.load_snapshot()
        assert frozen is not None
        assert frozen.document_version == 1
        assert frozen.content == content_v1
        assert frozen.sections == DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES

        async with seed.factory() as session, session.begin():
            document = await session.scalar(
                sa.select(MemoryDocumentRow).where(
                    MemoryDocumentRow.project_id == uuid.UUID(scope.project_id),
                    MemoryDocumentRow.owner_user_id == scope.owner_user_id,
                    MemoryDocumentRow.namespace == "default",
                )
            )
            assert document is not None
            document.content = content_v2
            document.content_digest = hashlib.sha256(content_v2.encode()).hexdigest()
            document.version = 2

        frozen_after_dream = await authority.load_snapshot()
        assert frozen_after_dream is not None
        assert frozen_after_dream.document_version == 1
        assert frozen_after_dream.content == content_v1

        async with seed.factory() as session, session.begin():
            personalization = AccountPersonalizationRepository(session)
            preference = await personalization.read_memory(scope.owner_user_id)
            await personalization.update_memory(
                uuid.UUID(scope.owner_user_id),
                memory_enabled=False,
                expected_version=preference.version,
            )
        assert await authority.load_snapshot() is None

        async with seed.factory() as session, session.begin():
            personalization = AccountPersonalizationRepository(session)
            preference = await personalization.read_memory(scope.owner_user_id)
            await personalization.update_memory(
                uuid.UUID(scope.owner_user_id),
                memory_enabled=True,
                expected_version=preference.version,
            )
        reopened = await authority.load_snapshot()
        assert reopened is not None
        assert reopened.document_version == 1
        assert reopened.content == content_v1

        async with seed.factory() as session, session.begin():
            snapshot = await session.scalar(
                sa.select(RunMemoryContextSnapshotRow).where(
                    RunMemoryContextSnapshotRow.run_id == run_id,
                )
            )
            assert snapshot is not None
            assert snapshot.document_version == 1
            assert snapshot.content == content_v1
            assert snapshot.sections == list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES)
            preference = await AccountPersonalizationRepository(session).read_memory(scope.owner_user_id)
            reset = await AccountPersonalizationRepository(session).reset_memory(
                uuid.UUID(scope.owner_user_id),
                expected_version=preference.version,
                now=datetime.now(UTC),
            )
            assert reset.snapshots == 1
            assert reset.affected_project_ids == (uuid.UUID(scope.project_id),)

        async with seed.factory() as session:
            assert (
                await session.scalar(
                    sa.select(RunMemoryContextSnapshotRow).where(
                        RunMemoryContextSnapshotRow.run_id == run_id,
                    )
                )
                is None
            )
    finally:
        await seed.engine.dispose()
