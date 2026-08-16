from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.private_work.memory_dream_service as service_module
import app.private_work.memory_service as memory_service_module
from app.personalization.repository import AccountMemoryPreference
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkInvalid,
    PrivateWorkUnavailable,
)
from app.private_work.memory_dream_service import (
    MemoryDreamAdmissionService,
    MemoryDreamSchedulerService,
)
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedMemoryDocumentPolicy,
    MemoryDocumentPolicy,
    MemoryPolicy,
)
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    EMPTY_MEMORY_DOCUMENT,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryBudgetRewriteScanCursor,
    MemoryBudgetRewriteScopePage,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
    MemoryEpisodeCursorInvalid,
)

NOW = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)
SECTIONS_POLICY_VERSION_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
DREAM_MODEL_REF = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _creation_policy(
    sections: tuple[str, ...] = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> LockedMemoryDocumentPolicy:
    return LockedMemoryDocumentPolicy(
        policy_version_id=SECTIONS_POLICY_VERSION_ID,
        revision=5,
        schema_version=1,
        payload_checksum="d" * 64,
        value=MemoryDocumentPolicy(sections=list(sections)),
    )


class _Personalization:
    async def read_memory(self, _owner, *, for_update: bool = False):
        assert for_update is True
        return AccountMemoryPreference(memory_enabled=True, version=8)


class _ProjectContext:
    def __init__(self) -> None:
        self.required = []

    def require(self, capability) -> None:
        self.required.append(capability)


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        self.session.transaction_depth += 1
        return self

    async def __aexit__(self, *_args):
        self.session.transaction_depth -= 1
        return False


class _Session:
    def __init__(self) -> None:
        self.transaction_depth = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def begin_nested(self) -> _Transaction:
        return _Transaction(self)

    def in_transaction(self) -> bool:
        return self.transaction_depth > 0


class _Repository:
    def __init__(
        self,
        scopes: tuple[MemoryDocumentScope, ...],
        *,
        document_exists: bool = True,
    ) -> None:
        self.scopes = scopes
        self.document_exists = document_exists
        self.list_kwargs = None
        self.admissions = []

    async def list_due_scopes(self, **kwargs):
        self.list_kwargs = kwargs
        return self.scopes

    async def read_state(self, _scope_value, **_kwargs):
        return SimpleNamespace(
            document=SimpleNamespace(
                version=1,
                content=EMPTY_MEMORY_DOCUMENT,
                sections_policy_version_id=(SECTIONS_POLICY_VERSION_ID if self.document_exists else None),
            ),
            pending_count=3,
        )

    async def admit_dream(self, scope, **kwargs):
        self.admissions.append((scope, kwargs))
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=uuid.uuid4(),
            history_count=20,
        )

    async def is_scope_due(self, _scope_value, **_kwargs):
        return True


def _scope(index: int = 1) -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID(f"{index:08d}-1111-4111-8111-111111111111"),
        owner_user_id=f"{index:08d}-2222-4222-8222-222222222222",
        namespace="default",
    )


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID(_scope().owner_user_id),
            project_id=_scope().project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-service",
        )
    )


def _runtime():
    policy = AgentRuntimePolicyValue(
        memory=MemoryPolicy(
            enabled=True,
            model_name=DREAM_MODEL_REF,
            dream_interval_minutes=45,
            max_injection_tokens=3_000,
        )
    )
    model = SimpleNamespace(
        model=SimpleNamespace(id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
        version=SimpleNamespace(
            id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            payload_checksum="c" * 64,
        ),
    )
    return policy, 23, model


def _service(repository: _Repository) -> MemoryDreamAdmissionService:
    return MemoryDreamAdmissionService(
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=lambda _session: _Personalization(),
        job_repository_builder=lambda _session: object(),
    )


@pytest.mark.asyncio
async def test_memory_read_advisory_includes_the_current_account_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Revalidator:
        async def require(self, *_args, **_kwargs):
            events.append("project")

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_state(self, _scope_value):
            events.append("document")
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content=EMPTY_MEMORY_DOCUMENT,
                    # Admission short-circuits at the disabled account switch,
                    # so the advisory must not inspect the current document.
                    content_digest="b" * 64,
                    sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                    active_dream_job_id=None,
                    updated_at=NOW,
                ),
                pending_count=0,
            )

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is False
            events.append("preference")
            return AccountMemoryPreference(memory_enabled=False, version=8)

    async def materialize(_session, _section):
        events.append("policy")
        return _runtime()[0]

    monkeypatch.setattr(
        memory_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    state, advisory = await PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=Personalization,
    ).get_with_injection_advisory(_context())

    assert state.document.version == 1
    assert (advisory.status, advisory.reason) == (
        "inactive",
        "account_disabled",
    )
    assert advisory.legacy_status == "ok"
    assert events == ["project", "policy", "preference", "document"]


@pytest.mark.asyncio
async def test_legacy_memory_read_keeps_its_budget_only_failure_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_state(self, _scope_value):
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content=EMPTY_MEMORY_DOCUMENT,
                    content_digest="corrupt legacy digest",
                    sections=("corrupt legacy sections",),
                ),
                pending_count=0,
            )

    class PersonalizationMustNotBeRead:
        def __init__(self, _session) -> None:
            raise AssertionError("legacy GET must not read account preference")

    async def materialize(_session, _section):
        return _runtime()[0]

    monkeypatch.setattr(
        memory_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    _state, legacy_status = await PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=PersonalizationMustNotBeRead,
    ).get(_context())

    assert legacy_status == "ok"


@pytest.mark.asyncio
async def test_opt_in_memory_advisory_fails_closed_on_enabled_document_damage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_state(self, _scope_value):
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content=EMPTY_MEMORY_DOCUMENT,
                    content_digest="b" * 64,
                    sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                ),
                pending_count=0,
            )

    async def materialize(_session, _section):
        return _runtime()[0]

    class Personalization:
        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is False
            return AccountMemoryPreference(memory_enabled=True, version=8)

    monkeypatch.setattr(
        memory_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=lambda _session: Personalization(),
    )

    with pytest.raises(PrivateWorkConflict):
        await service.get_with_injection_advisory(_context())


@pytest.mark.asyncio
async def test_platform_runtime_freezes_one_locked_policy_pointer_and_model_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def materialize(session, section, *, for_update=False):
        calls.append((session, section, for_update))
        return _runtime()[0], 23

    class Models:
        def __init__(self, session) -> None:
            self.session = session

        async def resolve_active_model(self, model_ref, *, load_envelope):
            calls.append((self.session, model_ref, load_envelope))
            return _runtime()[2]

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)

    async def lock_creation_policy(_session):
        calls.append((_session, "memory_document", True))
        return _creation_policy()

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyService,
        "lock_memory_document_for_creation",
        staticmethod(lock_creation_policy),
    )
    session = object()

    policy, revision, model, creation_policy = await MemoryDreamAdmissionService._platform_runtime(
        session,
        create_document=True,
    )

    assert policy == _runtime()[0]
    assert revision == 23
    assert model == _runtime()[2]
    assert creation_policy == _creation_policy()
    assert calls == [
        (session, service_module.RuntimePolicySection.AGENT_RUNTIME, True),
        (session, "memory_document", True),
        (session, DREAM_MODEL_REF, False),
    ]


@pytest.mark.asyncio
async def test_platform_runtime_does_not_read_current_document_policy_for_existing_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def materialize(session, section, *, for_update=False):
        calls.append((session, section, for_update))
        return _runtime()[0], 23

    async def forbidden_creation_policy(_session):
        raise AssertionError("existing documents must not read current memory_document")

    class Models:
        def __init__(self, session) -> None:
            self.session = session

        async def resolve_active_model(self, model_ref, *, load_envelope):
            calls.append((self.session, model_ref, load_envelope))
            return _runtime()[2]

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(
        service_module.SystemRuntimePolicyService,
        "lock_memory_document_for_creation",
        staticmethod(forbidden_creation_policy),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    session = object()

    policy, revision, model, creation_policy = await MemoryDreamAdmissionService._platform_runtime(
        session,
        create_document=False,
    )

    assert (policy, revision, model) == _runtime()
    assert creation_policy is None
    assert calls == [
        (session, service_module.RuntimePolicySection.AGENT_RUNTIME, True),
        (session, DREAM_MODEL_REF, False),
    ]


@pytest.mark.asyncio
async def test_manual_admission_freezes_exact_four_field_runtime_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(())
    service = _service(repository)

    async def runtime(_session, *, create_document):
        assert create_document is False
        return (*_runtime(), None)

    monkeypatch.setattr(service, "_platform_runtime", runtime)

    result = await service.admit(
        object(),
        _scope(),
        trigger="manual_dream",
        now=NOW,
    )

    assert result.disposition == "queued"
    _scope_value, kwargs = repository.admissions[0]
    frozen = kwargs["frozen"]
    assert frozen.preference_version == 8
    assert frozen.policy_revision == 23
    assert frozen.model_config_id == _runtime()[2].model.id
    assert frozen.model_version_id == _runtime()[2].version.id
    assert frozen.model_payload_checksum == "c" * 64
    assert kwargs["initial_content"] is None
    assert kwargs["initial_sections"] is None
    assert kwargs["sections_policy_version_id"] is None


@pytest.mark.asyncio
async def test_first_document_creation_uses_only_the_locked_memory_document_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sections = ("协作方式", "交付约束", "当前任务")
    creation_policy = _creation_policy(sections)
    repository = _Repository((), document_exists=False)
    service = _service(repository)

    async def runtime(_session, *, create_document):
        assert create_document is True
        return (*_runtime(), creation_policy)

    monkeypatch.setattr(service, "_platform_runtime", runtime)

    result = await service.admit(
        object(),
        _scope(),
        trigger="manual_dream",
        now=NOW,
    )

    assert result.disposition == "queued"
    _scope_value, kwargs = repository.admissions[0]
    assert kwargs["initial_content"] == "# 协作方式\n\n# 交付约束\n\n# 当前任务"
    assert kwargs["initial_sections"] == sections
    assert kwargs["sections_policy_version_id"] == creation_policy.policy_version_id


@pytest.mark.asyncio
async def test_scheduler_uses_dream_interval_and_admits_each_due_scope_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = (_scope(1), _scope(2))
    repository = _Repository(scopes)
    service = _service(repository)
    contexts = []

    async def policy(_session, *, for_update):
        assert for_update is False
        return _runtime()[0], _runtime()[1]

    async def runtime(_session, *, create_document):
        assert create_document is False
        return (*_runtime(), None)

    async def resolve(*_args, **_kwargs):
        context = _ProjectContext()
        contexts.append(context)
        return context

    monkeypatch.setattr(service, "_platform_policy", policy)
    monkeypatch.setattr(service, "_platform_runtime", runtime)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )

    session = _Session()
    async with session.begin():
        due = await service.list_due_scopes(session, now=NOW, max_jobs=7)
    results = []
    for scope in due:
        async with session.begin():
            results.append(
                await service.admit_scheduled_scope(
                    session,
                    scope,
                    now=NOW,
                )
            )

    assert all(result.disposition == "queued" for result in results)
    assert repository.list_kwargs == {
        "now": NOW,
        "interval_minutes": 45,
        "limit": 7,
    }
    assert [value[0] for value in repository.admissions] == list(scopes)
    assert all(value[1]["trigger"] == "auto_dream" for value in repository.admissions)
    assert all(context.required for context in contexts)


@pytest.mark.asyncio
async def test_scheduler_discovers_without_row_locks_then_uses_one_transaction_per_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    scopes = (_scope(1), _scope(2))

    class Session(_Session):
        def __init__(self, identity: int) -> None:
            super().__init__()
            self.identity = identity

    class SessionFactory:
        def __init__(self) -> None:
            self.sessions: list[Session] = []

        def __call__(self) -> Session:
            session = Session(len(self.sessions))
            self.sessions.append(session)
            return session

    class Repository:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def list_due_scopes(self, **_kwargs):
            events.append(("due", self.session.identity))
            return scopes

        async def list_budget_rewrite_scope_page(self, **_kwargs):
            events.append(("budget", self.session.identity))
            return MemoryBudgetRewriteScopePage(scopes=(), next_cursor=None)

        async def read_state(self, _scope_value, **_kwargs):
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content=EMPTY_MEMORY_DOCUMENT,
                    sections_policy_version_id=SECTIONS_POLICY_VERSION_ID,
                ),
                pending_count=3,
            )

        async def admit_dream(self, _scope_value, **_kwargs):
            events.append(("document", self.session.identity))
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.uuid4(),
                history_count=1,
            )

        async def is_scope_due(self, _scope_value, **kwargs):
            assert kwargs == {"now": NOW, "interval_minutes": 45}
            events.append(("due_scope", self.session.identity))
            return True

    class Personalization:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is True
            events.append(("preference", self.session.identity))
            return AccountMemoryPreference(memory_enabled=True, version=8)

    async def materialize(session, _section, *, for_update=False):
        events.append(("policy", session.identity, for_update))
        return _runtime()[0], 23

    class Models:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def resolve_active_model(self, _model_ref, *, load_envelope):
            assert load_envelope is False
            events.append(("model", self.session.identity))
            return _runtime()[2]

    async def resolve(session, *_args, **kwargs):
        assert kwargs["lock"] is True
        events.append(("project", session.identity))
        return _ProjectContext()

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    factory = SessionFactory()
    admission = MemoryDreamAdmissionService(
        repository_builder=lambda session, **_kwargs: Repository(session),
        personalization_repository_builder=Personalization,
        job_repository_builder=lambda _session: object(),
    )
    scheduler = MemoryDreamSchedulerService(
        factory,
        admission=admission,
        max_jobs_per_poll=7,
    )

    assert await scheduler.admit_due(now=NOW) == 2
    assert len(factory.sessions) == 4
    assert events == [
        ("policy", 0, False),
        ("due", 0),
        ("project", 1),
        ("policy", 1, True),
        ("model", 1),
        ("due_scope", 1),
        ("preference", 1),
        ("document", 1),
        ("project", 2),
        ("policy", 2, True),
        ("model", 2),
        ("due_scope", 2),
        ("preference", 2),
        ("document", 2),
        ("policy", 3, False),
        ("budget", 3),
    ]


@pytest.mark.asyncio
async def test_scheduler_rechecks_due_with_the_locked_current_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    intervals: list[int] = []

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def list_due_scopes(self, **kwargs):
            intervals.append(kwargs["interval_minutes"])
            return (scope,)

        async def list_budget_rewrite_scope_page(self, **_kwargs):
            return MemoryBudgetRewriteScopePage(scopes=(), next_cursor=None)

        async def is_scope_due(self, _scope_value, **kwargs):
            intervals.append(kwargs["interval_minutes"])
            return False

        async def read_state(self, _scope_value, **_kwargs):
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content=EMPTY_MEMORY_DOCUMENT,
                    sections_policy_version_id=SECTIONS_POLICY_VERSION_ID,
                ),
                pending_count=3,
            )

        async def admit_dream(self, *_args, **_kwargs):
            raise AssertionError("stale interval must not admit Dream")

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, *_args, **_kwargs):
            raise AssertionError("due recheck must precede the user preference lock")

    async def materialize(_session, _section, *, for_update=False):
        policy, revision, _model = _runtime()
        interval = 120 if for_update else 15
        return (
            policy.model_copy(update={"memory": policy.memory.model_copy(update={"dream_interval_minutes": interval})}),
            revision,
        )

    class Models:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_model(self, *_args, **_kwargs):
            return _runtime()[2]

    async def resolve(*_args, **_kwargs):
        return _ProjectContext()

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    scheduler = MemoryDreamSchedulerService(
        lambda: _Session(),
        admission=MemoryDreamAdmissionService(
            repository_builder=lambda session, **_kwargs: Repository(session),
            personalization_repository_builder=Personalization,
            job_repository_builder=lambda _session: object(),
        ),
    )

    assert await scheduler.admit_due(now=NOW) == 0
    assert intervals == [15, 120]


@pytest.mark.asyncio
async def test_restore_locks_policy_before_user_memory_and_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Revalidator:
        async def require(self, *_args, **kwargs):
            assert kwargs["lock"] is True
            events.append("project")

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is True
            events.append("preference")
            return AccountMemoryPreference(memory_enabled=True, version=8)

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_version(self, _scope_value, _version):
            events.append("version")
            return SimpleNamespace(
                content=EMPTY_MEMORY_DOCUMENT,
                content_digest="b" * 64,
            )

        async def read_state(self, _scope_value, *, for_update=False):
            assert for_update is True
            events.append("document")
            return SimpleNamespace(
                document=SimpleNamespace(
                    version=1,
                    content_digest="a" * 64,
                    sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
                    sections_policy_version_id=SECTIONS_POLICY_VERSION_ID,
                )
            )

        async def restore_version(self, *_args, **_kwargs):
            events.append("restore")
            return SimpleNamespace(version=2)

    class Audit:
        async def memory_dream_admitted(self, *_args, **_kwargs):
            raise AssertionError("restore must not audit Dream admission")

        async def memory_restore_executed(self, _session, _context, **kwargs):
            events.append("audit")
            assert kwargs == {
                "source_version": 1,
                "previous_version": 1,
                "published_version": 2,
                "changed": True,
            }

    async def materialize(_session, _section, *, for_update=False):
        assert for_update is True
        events.append("policy")
        return _runtime()[0]

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=Personalization,
        audit=Audit(),
    )

    result = await service.restore(
        _context(),
        target_version=1,
        expected_current_version=1,
    )

    assert result.version == 2
    assert events == [
        "project",
        "policy",
        "preference",
        "document",
        "version",
        "restore",
        "audit",
    ]


@pytest.mark.asyncio
async def test_episode_browse_maps_invalid_opaque_cursor_to_private_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def list_episodes(self, *_args, **_kwargs):
            raise MemoryEpisodeCursorInvalid("invalid")

    async def materialize(_session, _section):
        return _runtime()[0]

    monkeypatch.setattr(
        memory_service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
    )

    with pytest.raises(PrivateWorkInvalid):
        await service.list_episodes(
            _context(),
            q=None,
            tags=(),
            cursor="invalid-cursor",
            limit=20,
        )


@pytest.mark.asyncio
async def test_scheduler_wrapper_preserves_the_configured_poll_bound() -> None:
    class Admission:
        def __init__(self) -> None:
            self.kwargs = None
            self.sessions = []

        async def list_due_scopes(self, session, **kwargs):
            self.kwargs = (session, kwargs)
            self.sessions.append(session)
            return (_scope(),)

        async def list_budget_rewrite_scope_page(self, session, **kwargs):
            assert kwargs == {"cursor": None, "page_size": 100}
            self.sessions.append(session)
            return MemoryBudgetRewriteScopePage(scopes=(), next_cursor=None)

        async def admit_scheduled_scope(self, session, scope, **kwargs):
            self.sessions.append(session)
            assert scope == _scope()
            assert kwargs == {"now": NOW, "require_due": True}
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.uuid4(),
                history_count=1,
            )

    admission = Admission()
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    audit = _DreamLifecycleAudit()
    scheduler = MemoryDreamSchedulerService(
        factory,
        admission=admission,
        max_jobs_per_poll=9,
        audit=audit,
    )

    assert await scheduler.admit_due(now=NOW) == 1
    assert admission.kwargs == (sessions[0], {"now": NOW, "max_jobs": 9})
    assert admission.sessions == sessions
    assert len(sessions) == 3
    assert len(audit.calls) == 1
    assert audit.calls[0]["origin"] == "scheduled"
    assert audit.calls[0]["trigger"] == "auto_dream"


@pytest.mark.asyncio
async def test_budget_scheduler_pages_past_more_than_one_hundred_unadmittable_scopes() -> None:
    unadmittable = tuple(_scope(index) for index in range(1, 102))
    queued_scope = _scope(102)
    first_cursor = MemoryBudgetRewriteScanCursor(
        updated_at=NOW,
        project_id=unadmittable[99].project_id,
        owner_user_id=unadmittable[99].owner_user_id,
        namespace=unadmittable[99].namespace,
    )

    class Admission:
        def __init__(self) -> None:
            self.page_cursors: list[MemoryBudgetRewriteScanCursor | None] = []
            self.attempted: list[MemoryDocumentScope] = []

        async def list_due_scopes(self, session, **kwargs):
            assert session.in_transaction()
            assert kwargs == {"now": NOW, "max_jobs": 1}
            return ()

        async def list_budget_rewrite_scope_page(self, session, **kwargs):
            assert session.in_transaction()
            assert kwargs["page_size"] == 100
            cursor = kwargs["cursor"]
            self.page_cursors.append(cursor)
            if cursor is None:
                return MemoryBudgetRewriteScopePage(
                    scopes=unadmittable[:100],
                    next_cursor=first_cursor,
                )
            assert cursor == first_cursor
            return MemoryBudgetRewriteScopePage(
                scopes=(*unadmittable[100:], queued_scope),
                next_cursor=None,
            )

        async def admit_scheduled_scope(self, session, scope, **kwargs):
            assert session.in_transaction()
            assert kwargs == {"now": NOW, "require_due": False}
            self.attempted.append(scope)
            if scope != queued_scope:
                return MemoryDreamAdmissionRecord(
                    disposition="nothing_pending",
                    job_id=None,
                    history_count=0,
                )
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.uuid4(),
                history_count=0,
                admission_kind="budget_rewrite",
            )

    admission = Admission()
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    audit = _DreamLifecycleAudit()
    scheduler = MemoryDreamSchedulerService(
        factory,
        admission=admission,
        max_jobs_per_poll=1,
        audit=audit,
    )

    assert await scheduler.admit_due(now=NOW) == 1
    assert admission.page_cursors == [None, first_cursor]
    assert admission.attempted == [*unadmittable, queued_scope]
    # One due-discovery transaction, two bounded budget-discovery
    # transactions, then one independent transaction per attempted scope.
    assert len(sessions) == 1 + 2 + len(admission.attempted)
    assert all(not session.in_transaction() for session in sessions)
    assert len(audit.calls) == 1
    assert audit.calls[0]["origin"] == "scheduled"
    assert audit.calls[0]["trigger"] == "budget_rewrite"
    assert audit.calls[0]["history_count"] == 0


def test_dream_lifecycle_services_reject_partial_audit_ports() -> None:
    with pytest.raises(ValueError, match="Memory document audit port is invalid"):
        PrivateMemoryDocumentService(
            lambda: _Session(),
            audit=object(),
        )
    with pytest.raises(ValueError, match="Dream Scheduler audit port is invalid"):
        MemoryDreamSchedulerService(
            lambda: _Session(),
            audit=object(),
        )


class _DreamAdmission:
    def __init__(
        self,
        session: _Session,
        *,
        result: MemoryDreamAdmissionRecord | None = None,
    ) -> None:
        self.session = session
        self.calls: list[tuple[object, object, dict[str, object]]] = []
        self.result = result or MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=uuid.uuid4(),
            history_count=3,
        )

    async def admit(self, session, scope, **kwargs):
        assert session is self.session
        assert session.in_transaction() is True
        self.calls.append((session, scope, kwargs))
        return self.result


class _DreamLifecycleAudit:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def memory_dream_admitted(self, session, **kwargs):
        assert session.in_transaction() is True
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

    async def memory_restore_executed(self, *_args, **_kwargs):
        raise AssertionError("Dream admission must not audit restore")


@pytest.mark.asyncio
async def test_manual_dream_audits_only_queued_admission_in_same_transaction() -> None:
    session = _Session()
    admission = _DreamAdmission(session)
    audit = _DreamLifecycleAudit()

    class Revalidator:
        async def require(self, validated_session, *_args, **kwargs):
            assert validated_session is session
            assert validated_session.in_transaction() is True
            assert kwargs["lock"] is True

    context = _context()
    service = PrivateMemoryDocumentService(
        lambda: session,
        revalidator=Revalidator(),
        dream_admission=admission,
        audit=audit,
    )

    result = await service.dream(context)

    assert result.disposition == "queued"
    assert audit.calls == [
        {
            "project_id": context.project_id,
            "job_id": result.job_id,
            "request_id": context.request_id,
            "origin": "manual",
            "trigger": "manual_dream",
            "history_count": 3,
            "context": context,
        }
    ]
    assert session.in_transaction() is False


@pytest.mark.asyncio
async def test_manual_dream_does_not_audit_nonqueued_admission() -> None:
    session = _Session()
    audit = _DreamLifecycleAudit()
    admission = _DreamAdmission(
        session,
        result=MemoryDreamAdmissionRecord(
            disposition="nothing_pending",
            job_id=None,
            history_count=0,
        ),
    )

    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    result = await PrivateMemoryDocumentService(
        lambda: session,
        revalidator=Revalidator(),
        dream_admission=admission,
        audit=audit,
    ).dream(_context())

    assert result.disposition == "nothing_pending"
    assert audit.calls == []


@pytest.mark.asyncio
async def test_manual_dream_audit_failure_rolls_back_admission_transaction() -> None:
    session = _Session()

    class Revalidator:
        async def require(self, *_args, **_kwargs):
            return None

    service = PrivateMemoryDocumentService(
        lambda: session,
        revalidator=Revalidator(),
        dream_admission=_DreamAdmission(session),
        audit=_DreamLifecycleAudit(error=RuntimeError("audit unavailable")),
    )

    with pytest.raises(PrivateWorkUnavailable):
        await service.dream(_context())

    assert session.in_transaction() is False
