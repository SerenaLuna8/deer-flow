"""Remember contract: idempotent proposals, caps, due rules, backlog reads."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.private_work.memory_authority as authority_module
import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from app.audit.models import (
    AUDIT_ACTION_CONTRACTS,
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditProcess,
    AuditTargetKind,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.memory_authority import PrivateRunMemoryAuthority
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.memory.authority_resolution import resolve_memory_authority
from deerflow.config.memory_config import MemoryConfig
from deerflow.error_codes import MemoryAuthorityUnavailable
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.private_work.memory_document_repository import (
    DREAM_HISTORY_BATCH_SIZE,
    REMEMBER_BACKLOG_LIMIT,
    REMEMBER_PROMPT_VERSION,
    REMEMBER_RUN_LIMIT,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryProposalOutcome,
    MemoryRememberProposal,
    compute_remember_source_digest,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.tools.builtins import remember_tool

NOW = datetime(2026, 8, 6, 10, 20, 30, tzinfo=UTC)


def _scope() -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        owner_user_id="22222222-2222-4222-8222-222222222222",
        namespace="default",
    )


def _proposal(**overrides) -> MemoryRememberProposal:
    values = {
        "scope": _scope(),
        "thread_id": "thread-1",
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "kind": "durable",
        "content": "deployment target is region-eu",
    }
    values.update(overrides)
    return MemoryRememberProposal(**values)


def _compiled(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# ---------------------------------------------------------------------------
# Proposal contract: digest and validation
# ---------------------------------------------------------------------------


def test_source_digest_is_domain_separated_and_field_safe() -> None:
    digest = compute_remember_source_digest(
        run_id="run-1",
        tool_call_id="call-1",
        content="fact",
    )
    expected = hashlib.sha256(
        json.dumps(
            {
                "content": "fact",
                "domain": "deerflow.remember.source.v1",
                "run_id": "run-1",
                "tool_call_id": "call-1",
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected

    # Field boundaries must matter: shifting characters between fields
    # produces a different identity.
    assert compute_remember_source_digest(
        run_id="ab",
        tool_call_id="c",
        content="fact",
    ) != compute_remember_source_digest(
        run_id="a",
        tool_call_id="bc",
        content="fact",
    )


def test_proposal_derives_the_snip_compatible_tagged_line() -> None:
    proposal = _proposal(content="  deployment target is region-eu  ")
    assert proposal.tagged_text == "- [durable] deployment target is region-eu"
    assert proposal.source_digest == compute_remember_source_digest(
        run_id="run-1",
        tool_call_id="call-1",
        content="deployment target is region-eu",
    )


def test_proposal_rejects_out_of_contract_input() -> None:
    for overrides in (
        {"kind": "skip"},
        {"kind": "unknown"},
        {"content": "   "},
        {"content": "x" * 501},
        {"content": "line one\nline two"},
        {"content": "line one\n"},
        {"content": "\tline one"},
        {"content": "bell\x07"},
        {"content": 7},
        {"thread_id": ""},
        {"thread_id": "x" * 65},
        {"run_id": ""},
        {"run_id": "x" * 65},
        {"tool_call_id": ""},
        {"tool_call_id": "x" * 129},
        {"scope": object()},
    ):
        with pytest.raises(ValueError):
            _proposal(**overrides)


# ---------------------------------------------------------------------------
# Repository: propose_entry
# ---------------------------------------------------------------------------


class _PreferenceResult:
    def __init__(self, row) -> None:
        self.row = row

    def one_or_none(self):
        return self.row


class _RememberSession:
    """Scripted session for the serialized propose_entry statement order."""

    def __init__(
        self,
        *,
        preference,
        scalars=(),
    ) -> None:
        self.preference = preference
        self.scalar_results = list(scalars)
        self.executed: list[object] = []
        self.scalar_statements: list[object] = []
        self.flushed = 0

    async def execute(self, statement):
        self.executed.append(statement)
        return _PreferenceResult(self.preference)

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        return self.scalar_results.pop(0)

    async def flush(self):
        self.flushed += 1


def _preference_row(*, enabled: bool = True, version: int = 7):
    return SimpleNamespace(memory_enabled=enabled, preferences_version=version)


@pytest.mark.asyncio
async def test_repository_records_a_tool_row_within_contract() -> None:
    entry_id = uuid.uuid4()
    session = _RememberSession(
        preference=_preference_row(version=7),
        scalars=(None, 0, 3, entry_id),
    )
    repository = MemoryDocumentRepository(session, jobs=object())
    proposal = _proposal()

    outcome = await repository.propose_entry(proposal)

    assert outcome == MemoryProposalOutcome(
        disposition="recorded",
        entry_id=entry_id,
        tagged_text="- [durable] deployment target is region-eu",
    )
    assert session.flushed == 1
    preference_sql = _compiled(session.executed[0])
    assert "FOR UPDATE" in preference_sql
    insert_sql = _compiled(session.scalar_statements[-1])
    assert "'tool'" in insert_sql
    assert "'run-1'" in insert_sql
    assert f"'{REMEMBER_PROMPT_VERSION}'" in insert_sql
    assert f"'{proposal.source_digest}'" in insert_sql
    assert "'- [durable] deployment target is region-eu'" in insert_sql
    assert "7" in insert_sql
    assert "ON CONFLICT" in insert_sql


@pytest.mark.asyncio
async def test_repository_reports_duplicate_before_any_cap() -> None:
    existing = uuid.uuid4()
    session = _RememberSession(
        preference=_preference_row(),
        scalars=(existing,),
    )
    repository = MemoryDocumentRepository(session, jobs=object())

    outcome = await repository.propose_entry(_proposal())

    assert outcome.disposition == "duplicate"
    assert outcome.entry_id == existing
    assert outcome.tagged_text == "- [durable] deployment target is region-eu"
    # Only the duplicate lookup ran: caps and insert were never consulted.
    assert len(session.scalar_statements) == 1


@pytest.mark.asyncio
async def test_repository_enforces_run_then_backlog_caps() -> None:
    at_run_cap = _RememberSession(
        preference=_preference_row(),
        scalars=(None, REMEMBER_RUN_LIMIT),
    )
    outcome = await MemoryDocumentRepository(
        at_run_cap,
        jobs=object(),
    ).propose_entry(_proposal())
    assert outcome.disposition == "run_limit_reached"
    assert outcome.entry_id is None
    assert len(at_run_cap.scalar_statements) == 2

    full_backlog = _RememberSession(
        preference=_preference_row(),
        scalars=(None, 0, REMEMBER_BACKLOG_LIMIT),
    )
    outcome = await MemoryDocumentRepository(
        full_backlog,
        jobs=object(),
    ).propose_entry(_proposal())
    assert outcome.disposition == "backlog_full"
    assert outcome.entry_id is None
    assert len(full_backlog.scalar_statements) == 3


@pytest.mark.asyncio
async def test_repository_reports_disabled_or_missing_preference() -> None:
    disabled = _RememberSession(preference=_preference_row(enabled=False))
    outcome = await MemoryDocumentRepository(
        disabled,
        jobs=object(),
    ).propose_entry(_proposal())
    assert outcome.disposition == "memory_disabled"
    assert disabled.scalar_statements == []

    missing = _RememberSession(preference=None)
    outcome = await MemoryDocumentRepository(
        missing,
        jobs=object(),
    ).propose_entry(_proposal())
    assert outcome.disposition == "memory_disabled"

    with pytest.raises(TypeError):
        await MemoryDocumentRepository(
            _RememberSession(preference=None),
            jobs=object(),
        ).propose_entry(object())


# ---------------------------------------------------------------------------
# Repository: due rule and pending backlog reads
# ---------------------------------------------------------------------------


class _EmptyRows:
    def __iter__(self):
        return iter(())


class _DueSession:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.scalar_statements: list[object] = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _EmptyRows()

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        return None


@pytest.mark.asyncio
async def test_due_rule_admits_interval_full_batch_or_aged_tool_rows() -> None:
    session = _DueSession()
    repository = MemoryDocumentRepository(session, jobs=object())

    await repository.list_due_scopes(now=NOW, interval_minutes=120, limit=10)
    listed = _compiled(session.executed[0])

    await repository.is_scope_due(_scope(), now=NOW, interval_minutes=120)
    checked = _compiled(session.scalar_statements[0])

    for sql in (listed, checked):
        # 1. interval anchor against now - dream_interval_minutes
        assert str((NOW - timedelta(minutes=120)).replace(tzinfo=None)) in sql.replace("+00:00", "")
        # 2. full pending batch admits immediately
        assert f"count(*) >= {DREAM_HISTORY_BATCH_SIZE}" in sql
        # 3. an aged tool proposal admits after its grace window
        assert "origin = 'tool'" in sql
        assert str((NOW - timedelta(minutes=10)).replace(tzinfo=None)) in sql.replace("+00:00", "")
        assert " OR " in sql

    with pytest.raises(ValueError):
        await repository.list_due_scopes(
            now=NOW.replace(tzinfo=None),
            interval_minutes=120,
        )


@pytest.mark.asyncio
async def test_repository_lists_pending_backlog_in_dream_order() -> None:
    row = SimpleNamespace(
        sequence=41,
        origin="tool",
        tagged_text="- [durable] deployment target is region-eu",
        created_at=NOW,
    )

    class _PendingResult:
        def scalars(self):
            return iter((row,))

    class _PendingSession:
        def __init__(self) -> None:
            self.executed: list[object] = []

        async def execute(self, statement):
            self.executed.append(statement)
            return _PendingResult()

    session = _PendingSession()
    repository = MemoryDocumentRepository(session, jobs=object())

    records = await repository.list_pending_entries(_scope(), limit=25, offset=50)

    assert len(records) == 1
    assert records[0].sequence == 41
    assert records[0].origin == "tool"
    assert records[0].tagged_text == row.tagged_text
    sql = _compiled(session.executed[0])
    assert "status = 'pending'" in sql
    assert "ORDER BY memory_history_entries.sequence" in sql
    assert "LIMIT 25" in sql
    assert "OFFSET 50" in sql

    for kwargs in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 10_001},
    ):
        with pytest.raises(ValueError):
            await repository.list_pending_entries(_scope(), **kwargs)
    with pytest.raises(TypeError):
        await repository.list_pending_entries(object())


# ---------------------------------------------------------------------------
# Authority: run-bound proposal with transactional audit
# ---------------------------------------------------------------------------


class _AuthorityTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _AuthoritySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _AuthorityTransaction()


class _Personalization:
    def __init__(self, _session, *, enabled: bool = True) -> None:
        self.enabled = enabled

    async def read_memory(self, _user_id):
        return SimpleNamespace(memory_enabled=self.enabled, version=4)


class _Runs:
    def __init__(self, _session, *, thread_id: str, job_id: uuid.UUID) -> None:
        self.thread_id = thread_id
        self.job_id = job_id

    async def assert_execution_active(self, **_kwargs):
        return False

    async def get(self, **_kwargs):
        return SimpleNamespace(thread_id=self.thread_id, job_id=self.job_id)


class _Threads:
    def __init__(
        self,
        _session,
        *,
        thread_id: str,
        project_id: uuid.UUID,
        owner_user_id: str,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self.owner_user_id = owner_user_id

    async def get(self, *, scope, thread_id: str, lock: bool):
        assert scope.project_id == str(self.project_id)
        assert scope.owner_user_id == self.owner_user_id
        assert thread_id == self.thread_id
        assert lock is True
        return SimpleNamespace(
            thread_id=self.thread_id,
            project_id=self.project_id,
            owner_user_id=self.owner_user_id,
            frozen_at=None,
            deleted_at=None,
        )


class _AuditPort:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.recall_calls: list[dict] = []

    async def memory_remembered(self, session, scope, **kwargs):
        self.calls.append({"session": session, "scope": scope, **kwargs})

    async def memory_recall_executed(self, session, scope, **kwargs):
        self.recall_calls.append({"session": session, "scope": scope, **kwargs})


class _StubRepository:
    outcome: MemoryProposalOutcome = MemoryProposalOutcome(
        disposition="recorded",
        entry_id=uuid.uuid4(),
        tagged_text="- [durable] deployment target is region-eu",
    )
    proposals: list[MemoryRememberProposal] = []

    def __init__(self, _session) -> None:
        pass

    async def propose_entry(self, proposal):
        _StubRepository.proposals.append(proposal)
        return _StubRepository.outcome


def _authority_parts(*, memory_platform_enabled: bool = True, audit=None):
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    job_id = uuid.uuid4()
    thread_id = str(uuid.uuid4())
    project = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=3,
        request_id="memory-remember-test",
    )
    context = PrivateWorkContext.from_project(project)
    claim = JobClaim(
        job_id=job_id,
        attempt_id=uuid.uuid4(),
        lease_token="lease-token",
        job_type="private_run",
        scope=JobScope(project_id, str(user_id)),
        run_id=str(uuid.uuid4()),
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id="a" * 32,
    )
    session = _AuthoritySession()
    sessions_opened = {"count": 0}

    def factory():
        sessions_opened["count"] += 1
        return session

    authority = PrivateRunMemoryAuthority(
        factory,
        context=context,
        claim=claim,
        thread_id=thread_id,
        namespace="default",
        memory_config=MemoryConfig(
            enabled=memory_platform_enabled,
            max_injection_tokens=2_000,
        ),
        personalization_repository_builder=_Personalization,
        run_repository_builder=lambda current: _Runs(
            current,
            thread_id=thread_id,
            job_id=job_id,
        ),
        thread_repository_builder=lambda current: _Threads(
            current,
            thread_id=thread_id,
            project_id=project_id,
            owner_user_id=str(user_id),
        ),
        audit=audit,
    )
    return authority, project, claim, session, sessions_opened


def _pass_revalidation(monkeypatch: pytest.MonkeyPatch, project) -> None:
    async def resolve(*_args, **_kwargs):
        return project

    async def active(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        authority_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        authority_module.PrivateRunAuthorizationService,
        "is_active",
        active,
    )


@pytest.mark.asyncio
async def test_authority_propose_validates_arguments_before_any_database_work() -> None:
    authority, _project, _claim, _session, opened = _authority_parts()

    for kwargs in (
        {"kind": "skip", "content": "ok", "tool_call_id": "call-1"},
        {"kind": "durable", "content": "  ", "tool_call_id": "call-1"},
        {"kind": "durable", "content": "x" * 501, "tool_call_id": "call-1"},
        {"kind": "durable", "content": "a\nb", "tool_call_id": "call-1"},
        {"kind": "durable", "content": "fact\n", "tool_call_id": "call-1"},
        {"kind": "durable", "content": "\tfact", "tool_call_id": "call-1"},
        {"kind": "durable", "content": "ok", "tool_call_id": ""},
    ):
        with pytest.raises(ValueError):
            await authority.propose_entry(**kwargs)
    assert opened["count"] == 0


@pytest.mark.asyncio
async def test_authority_propose_reports_platform_disabled_without_database_work() -> None:
    authority, _project, _claim, _session, opened = _authority_parts(
        memory_platform_enabled=False,
    )

    outcome = await authority.propose_entry(
        kind="durable",
        content="fact",
        tool_call_id="call-1",
    )

    assert outcome.disposition == "memory_disabled"
    assert opened["count"] == 0


@pytest.mark.asyncio
async def test_authority_propose_records_and_audits_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _AuditPort()
    authority, project, claim, session, _opened = _authority_parts(audit=audit)
    _pass_revalidation(monkeypatch, project)
    monkeypatch.setattr(authority_module, "MemoryDocumentRepository", _StubRepository)
    _StubRepository.proposals = []

    outcome = await authority.propose_entry(
        kind="durable",
        content="deployment target is region-eu",
        tool_call_id="call-1",
    )

    assert outcome.disposition == "recorded"
    proposal = _StubRepository.proposals[0]
    assert proposal.scope.project_id == project.project_id
    assert proposal.scope.owner_user_id == str(project.user_id)
    assert proposal.run_id == claim.run_id
    assert proposal.tool_call_id == "call-1"
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["session"] is session
    assert call["scope"].project_id == str(project.project_id)
    assert call["run_id"] == claim.run_id
    assert call["job_id"] == claim.job_id
    assert call["request_id"] == project.request_id
    assert call["kind"] == "durable"


@pytest.mark.asyncio
async def test_authority_propose_skips_audit_for_non_recorded_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _AuditPort()
    authority, project, _claim, _session, _opened = _authority_parts(audit=audit)
    _pass_revalidation(monkeypatch, project)
    monkeypatch.setattr(authority_module, "MemoryDocumentRepository", _StubRepository)
    _StubRepository.proposals = []
    _StubRepository.outcome = MemoryProposalOutcome(
        disposition="duplicate",
        entry_id=uuid.uuid4(),
        tagged_text="- [durable] deployment target is region-eu",
    )

    outcome = await authority.propose_entry(
        kind="durable",
        content="deployment target is region-eu",
        tool_call_id="call-1",
    )

    assert outcome.disposition == "duplicate"
    assert audit.calls == []
    _StubRepository.outcome = MemoryProposalOutcome(
        disposition="recorded",
        entry_id=uuid.uuid4(),
        tagged_text="- [durable] x",
    )


@pytest.mark.asyncio
async def test_authority_propose_fails_closed_when_revalidation_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _project, _claim, _session, _opened = _authority_parts()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        authority_module,
        "resolve_project_context_in_transaction",
        explode,
    )

    with pytest.raises(MemoryAuthorityUnavailable):
        await authority.propose_entry(
            kind="durable",
            content="fact",
            tool_call_id="call-1",
        )


def test_authority_rejects_an_audit_port_without_the_remember_hook() -> None:
    with pytest.raises(ValueError):
        _authority_parts(audit=object())


def test_authority_rejects_an_audit_port_without_the_recall_hook() -> None:
    async def remembered(*_args, **_kwargs):
        return None

    with pytest.raises(ValueError):
        _authority_parts(audit=SimpleNamespace(memory_remembered=remembered))


# ---------------------------------------------------------------------------
# Tool: remember
# ---------------------------------------------------------------------------


class _ToolAuthority:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def propose_entry(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _runtime(context) -> SimpleNamespace:
    return SimpleNamespace(context=context)


async def _invoke(context, **kwargs) -> str:
    kwargs.setdefault("tool_call_id", "call-1")
    return await remember_tool.coroutine(runtime=_runtime(context), **kwargs)


@pytest.mark.asyncio
async def test_remember_tool_reports_unavailable_without_a_trusted_authority() -> None:
    assert "unavailable" in await _invoke({}, content="fact", kind="durable")
    forged = {"__memory_authority": {"propose_entry": "forged"}}
    assert "unavailable" in await _invoke(forged, content="fact", kind="durable")
    search_only = {
        "__memory_authority": SimpleNamespace(search_episodes=lambda: None),
    }
    assert "unavailable" in await _invoke(
        search_only,
        content="fact",
        kind="durable",
    )


@pytest.mark.asyncio
async def test_remember_tool_rejects_out_of_contract_arguments_before_proposing() -> None:
    authority = _ToolAuthority(
        MemoryProposalOutcome(
            disposition="recorded",
            entry_id=uuid.uuid4(),
            tagged_text="- [durable] fact",
        )
    )
    context = {"__memory_authority": authority}

    assert (await _invoke(context, content="fact", kind="skip")).startswith("Error:")
    assert (await _invoke(context, content="  ", kind="durable")).startswith("Error:")
    assert (await _invoke(context, content="x" * 501, kind="durable")).startswith("Error:")
    assert (await _invoke(context, content="a\nb", kind="durable")).startswith("Error:")
    assert (await _invoke(context, content="fact\n", kind="durable")).startswith("Error:")
    assert (await _invoke(context, content="\tfact", kind="durable")).startswith("Error:")
    assert authority.calls == []


@pytest.mark.asyncio
async def test_remember_tool_echoes_the_recorded_line_for_the_chat_chip() -> None:
    authority = _ToolAuthority(
        MemoryProposalOutcome(
            disposition="recorded",
            entry_id=uuid.uuid4(),
            tagged_text="- [durable] deployment target is region-eu",
        )
    )
    context = {"__memory_authority": authority}

    rendered = await _invoke(
        context,
        content="  deployment target is region-eu  ",
        kind="durable",
    )

    assert rendered == ("Remembered for the next organization pass: - [durable] deployment target is region-eu")
    assert authority.calls == [
        {
            "kind": "durable",
            "content": "deployment target is region-eu",
            "tool_call_id": "call-1",
        }
    ]


@pytest.mark.asyncio
async def test_remember_tool_translates_each_refusal_disposition() -> None:
    def outcome(disposition):
        return MemoryProposalOutcome(
            disposition=disposition,
            entry_id=None,
            tagged_text=None,
        )

    disabled = {"__memory_authority": _ToolAuthority(outcome("memory_disabled"))}
    assert "disabled" in await _invoke(disabled, content="fact", kind="durable")

    limited = {"__memory_authority": _ToolAuthority(outcome("run_limit_reached"))}
    limited_text = await _invoke(limited, content="fact", kind="durable")
    assert limited_text.startswith("Error:")
    assert "do not call remember again" in limited_text

    full = {"__memory_authority": _ToolAuthority(outcome("backlog_full"))}
    full_text = await _invoke(full, content="fact", kind="durable")
    assert full_text.startswith("Error:")
    assert "backlog is full" in full_text


@pytest.mark.asyncio
async def test_remember_tool_treats_duplicates_as_success() -> None:
    authority = _ToolAuthority(
        MemoryProposalOutcome(
            disposition="duplicate",
            entry_id=uuid.uuid4(),
            tagged_text="- [durable] fact",
        )
    )
    context = {"__memory_authority": authority}

    rendered = await _invoke(context, content="fact", kind="durable")

    assert rendered == "Remembered for the next organization pass: - [durable] fact"


@pytest.mark.asyncio
async def test_remember_tool_propagates_revoked_authority() -> None:
    authority = _ToolAuthority(AuthorizationRevoked())
    context = {"__memory_authority": authority}

    with pytest.raises(AuthorizationRevoked):
        await _invoke(context, content="fact", kind="durable")


def test_remember_tool_shape_and_capability_gate() -> None:
    assert remember_tool.name == "remember"
    assert remember_tool.coroutine is not None
    # The tool-call id is injected by the runtime, never exposed to the model.
    assert "tool_call_id" not in remember_tool.args

    class _ProposeCapable:
        async def propose_entry(self, **_kwargs):
            return None

    propose = {"__memory_authority": _ProposeCapable()}
    assert resolve_memory_authority(propose, method="propose_entry") is not None

    search_only = {
        "__memory_authority": SimpleNamespace(search_episodes=lambda: None),
    }
    assert resolve_memory_authority(search_only, method="propose_entry") is None


# ---------------------------------------------------------------------------
# Audit contract
# ---------------------------------------------------------------------------


def test_memory_remember_audit_action_contract() -> None:
    action = AuditAction.MEMORY_REMEMBER
    assert action.value == "memory.remember"

    contract = AUDIT_ACTION_CONTRACTS[action]
    assert contract.target_kind is AuditTargetKind.RUN
    variant = contract.variants[0]
    assert variant.actor == "process"
    assert variant.processes == frozenset({AuditProcess.WORKER})

    model = AUDIT_METADATA_MODELS[action]
    assert model.model_validate({"kind": "durable"}).kind == "durable"
    for invalid in ({"kind": "skip"}, {"kind": "durable", "extra": 1}, {}):
        with pytest.raises(Exception):
            model.model_validate(invalid)
