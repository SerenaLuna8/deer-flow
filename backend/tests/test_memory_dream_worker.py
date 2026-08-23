from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.personalization.repository import AccountMemoryPreference
from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_runtime_settings.models import AgentRuntimePolicyValue, MemoryPolicy
from app.worker.memory_dream import MemoryDreamJobHandler
from app.worker.service import JobSettlement, LeaseLost
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    DREAM_PROMPT_VERSION,
    EMPTY_MEMORY_DOCUMENT,
    MemoryDreamError,
    MemoryDreamResult,
    render_empty_memory_document,
    validate_memory_document,
)
from deerflow.config.model_execution import FrozenSystemModelExecution
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDreamHistoryRecord,
    MemoryDreamLeaseConflict,
    MemoryDreamReleaseResult,
    MemoryDreamSettlementInvariant,
    MemoryDreamStaleConflict,
    MemoryDreamWork,
    compute_dream_history_digest,
    memory_document_diff_preview,
    memory_document_digest,
    memory_document_unified_diff,
)

DREAM_MODEL_REF = "66666666-6666-4666-8666-666666666666"


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.rollback_callbacks: list[Callable[[], None]] = []

    async def __aenter__(self):
        self.session.factory.active += 1
        self.session.transactions.append(self)
        return self

    async def __aexit__(self, exc_type, *_args):
        transaction = self.session.transactions.pop()
        assert transaction is self
        self.session.factory.transaction_exits.append(exc_type)
        if exc_type is not None:
            for callback in reversed(self.rollback_callbacks):
                callback()
        elif self.session.transactions:
            self.session.transactions[-1].rollback_callbacks.extend(self.rollback_callbacks)
        self.session.factory.active -= 1
        return False


class _Session:
    def __init__(self, factory: _SessionFactory) -> None:
        self.factory = factory
        self.transactions: list[_Transaction] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def begin_nested(self) -> _Transaction:
        return _Transaction(self)

    def on_rollback(self, callback: Callable[[], None]) -> None:
        assert self.transactions
        self.transactions[-1].rollback_callbacks.append(callback)


class _SessionFactory:
    def __init__(self) -> None:
        self.active = 0
        self.opened = 0
        self.transaction_exits: list[type[BaseException] | None] = []

    def __call__(self) -> _Session:
        self.opened += 1
        return _Session(self)


class _Authority:
    cancel_requested = False

    def __init__(self) -> None:
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1


class _Personalization:
    def __init__(self, preference: AccountMemoryPreference) -> None:
        self.preference = preference

    async def read_memory(self, _owner, *, for_update: bool = False):
        return self.preference


class _ModelMaterializer:
    def __init__(self) -> None:
        self.frozen: list[FrozenSystemModelExecution] = []

    async def materialize_frozen(self, execution):
        self.frozen.append(execution)
        return SimpleNamespace(name=DREAM_MODEL_REF)


class _PolicyMaterializer:
    def __init__(
        self,
        policy: AgentRuntimePolicyValue,
        *,
        current_revision: int = 17,
        current_error: Exception | None = None,
    ) -> None:
        self.policy = policy
        self.current_revision = current_revision
        self.current_error = current_error
        self.revisions: list[int] = []

    async def materialize_revision(self, _section, revision: int):
        self.revisions.append(revision)
        return self.policy

    async def materialize_current_with_revision_in_session(
        self,
        *_args,
        **_kwargs,
    ):
        if self.current_error is not None:
            raise self.current_error
        return self.policy, self.current_revision


class _RepositoryState:
    def __init__(self, work: MemoryDreamWork) -> None:
        self.work = work
        self.finalized: list[dict[str, object]] = []
        self.released: list[dict[str, object]] = []
        self.needs_review = False
        self.finalize_error: Exception | None = None
        self.release_error: Exception | None = None
        self.release_result: MemoryDreamReleaseResult | None = None


class _Repository:
    def __init__(self, state: _RepositoryState, *, session: _Session) -> None:
        self.state = state
        self.session = session

    async def load_dream_work(self, _scope, _job_id):
        return self.state.work

    async def finalize_dream(self, _scope, **kwargs):
        if self.state.finalize_error is not None:
            raise self.state.finalize_error
        self.state.finalized.append(kwargs)
        self.session.on_rollback(lambda: self.state.finalized.remove(kwargs))
        return SimpleNamespace(
            version=1,
            needs_review=self.state.needs_review,
        )

    async def release_dream(self, _scope, **kwargs):
        if self.state.release_error is not None:
            raise self.state.release_error
        self.state.released.append(kwargs)
        self.session.on_rollback(lambda: self.state.released.remove(kwargs))
        if self.state.release_result is not None:
            return self.state.release_result
        disposition = "cancelled" if kwargs["cancelled"] else ("retry_wait" if kwargs.get("retryable", True) else "dead")
        return MemoryDreamReleaseResult(disposition=disposition)


class _Audit:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        settled_error: Exception | None = None,
    ) -> None:
        self.error = error
        self.settled_error = settled_error
        self.calls: list[dict[str, object]] = []
        self.settled_calls: list[dict[str, object]] = []

    async def memory_dream_review_flagged(self, _session, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

    async def memory_dream_settled(self, _session, **kwargs) -> None:
        self.settled_calls.append(kwargs)
        if self.settled_error is not None:
            raise self.settled_error


class _Runner:
    def __init__(
        self,
        factory: _SessionFactory,
        *,
        result: MemoryDreamResult | None = None,
        error: MemoryDreamError | None = None,
    ) -> None:
        self.factory = factory
        self.result = result
        self.error = error
        self.inputs = []

    async def run(self, value):
        assert self.factory.active == 0, "model wait must not hold a DB transaction"
        self.inputs.append(value)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        attempt_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        lease_token="lease-token",
        job_type="memory_dream",
        scope=JobScope(
            uuid.UUID("33333333-3333-4333-8333-333333333333"),
            "44444444-4444-4444-8444-444444444444",
        ),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        namespace="default",
    )


def _work() -> MemoryDreamWork:
    text = "- [durable] PostgreSQL is the only application database."
    history = (
        MemoryDreamHistoryRecord(
            id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
            sequence=9,
            tagged_text=text,
            content_digest=hashlib.sha256(text.encode()).hexdigest(),
        ),
    )
    return MemoryDreamWork(
        job_id=_claim().job_id,
        project_id=_claim().scope.project_id,
        owner_user_id=_claim().scope.owner_user_id or "",
        namespace="default",
        trigger="manual_dream",
        history_from=9,
        history_to=9,
        history_count=1,
        history_digest=compute_dream_history_digest(history),
        base_document_version=0,
        base_content=EMPTY_MEMORY_DOCUMENT,
        base_content_digest=memory_document_digest(EMPTY_MEMORY_DOCUMENT),
        sections=DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
        sections_policy_version_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        preference_version=6,
        policy_revision=17,
        model_execution=FrozenSystemModelExecution(
            model_config_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
            provider_payload={
                "name": DREAM_MODEL_REF,
                "provider_adapter": "deepseek",
                "model": "deepseek-v4-flash",
                "max_input_tokens": 64_000,
            },
            payload_checksum="a" * 64,
            secret_generation_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            secret_envelope_digest="b" * 64,
        ),
        prompt_version=DREAM_PROMPT_VERSION,
        result_version=None,
        cancel_requested=False,
        job_status="running",
        history=history,
    )


def _handler(
    *,
    factory: _SessionFactory,
    state: _RepositoryState,
    runner: _Runner,
    preference: AccountMemoryPreference | None = None,
    current_policy_revision: int = 17,
    current_policy_error: Exception | None = None,
    audit: _Audit | None = None,
):
    policy = AgentRuntimePolicyValue(
        memory=MemoryPolicy(
            enabled=True,
            model_name=DREAM_MODEL_REF,
            dream_interval_minutes=120,
            max_injection_tokens=2_000,
        )
    )
    materializer = _ModelMaterializer()
    return (
        MemoryDreamJobHandler(
            factory,
            app_config=None,
            model_materializer=materializer,
            runtime_policy_materializer=_PolicyMaterializer(
                policy,
                current_revision=current_policy_revision,
                current_error=current_policy_error,
            ),
            runner_factory=lambda _model: runner,
            repository_builder=lambda session, **_kwargs: _Repository(
                state,
                session=session,
            ),
            job_repository_builder=lambda _session: object(),
            scope_validator=lambda *_args, **_kwargs: _async_true(),
            personalization_repository_builder=lambda _session: _Personalization(preference or AccountMemoryPreference(True, 6)),
            audit=audit,
        ),
        materializer,
    )


async def _async_true() -> bool:
    return True


@pytest.mark.asyncio
async def test_dream_worker_waits_without_db_lock_then_atomically_finalizes() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, model_materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    assert not state.finalized
    assert model_materializer.frozen == [state.work.model_execution]
    await settlement.commit()
    await settlement.commit()
    assert len(state.finalized) == 1
    assert state.finalized[0]["content"] == EMPTY_MEMORY_DOCUMENT
    assert state.finalized[0]["expected_sections"] == DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES
    assert runner.inputs[0].sections == DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES
    assert not state.released


@pytest.mark.asyncio
async def test_dream_worker_does_not_consume_history_without_explicit_replacement() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=False,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == ("MEMORY_DREAM_REPLACEMENT_REQUIRED")
    await settlement.commit()
    assert state.finalized == []
    assert state.released[0]["cancelled"] is False
    assert state.released[0]["retryable"] is True


@pytest.mark.asyncio
async def test_dream_worker_audits_published_version_in_finalize_transaction() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    audit = _Audit()
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        audit=audit,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()

    assert audit.settled_calls == [
        {
            "project_id": state.work.project_id,
            "job_id": _claim().job_id,
            "request_id": "memory-dream-worker",
            "disposition": "published",
            "version": 1,
        }
    ]
    assert len(state.finalized) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancelled", "retryable", "expected_disposition"),
    (
        (True, True, "cancelled"),
        (False, True, None),
        (False, False, "dead"),
    ),
)
async def test_dream_worker_audits_only_terminal_typed_release_results(
    cancelled: bool,
    retryable: bool,
    expected_disposition: str | None,
) -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    audit = _Audit()
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        audit=audit,
    )
    settlement = handler._release_settlement(
        _claim(),
        cancelled=cancelled,
        public_error_code="MEMORY_DREAM_TEST_FAILED",
        retryable=retryable,
    )

    await settlement.commit()

    if expected_disposition is None:
        assert audit.settled_calls == []
        assert state.released[0]["retryable"] is True
    else:
        assert len(audit.settled_calls) == 1
        audited = audit.settled_calls[0]
        assert audited["disposition"] == expected_disposition
        assert audited["version"] is None
        assert audited["public_error_code"] == (None if expected_disposition == "cancelled" else "MEMORY_DREAM_TEST_FAILED")


@pytest.mark.asyncio
async def test_dream_worker_does_not_duplicate_already_published_audit() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.release_result = MemoryDreamReleaseResult(
        disposition="already_published",
    )
    audit = _Audit()
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        audit=audit,
    )

    await handler._release_settlement(
        _claim(),
        cancelled=False,
    ).commit()

    assert audit.settled_calls == []


@pytest.mark.asyncio
async def test_dream_worker_transient_failure_retries_the_frozen_batch() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        error=MemoryDreamError("MEMORY_DREAM_TIMEOUT"),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == "MEMORY_DREAM_TIMEOUT"
    await settlement.commit()
    assert not state.finalized
    assert state.released[0]["cancelled"] is False
    assert state.released[0]["retryable"] is True


@pytest.mark.asyncio
async def test_dream_worker_can_shrink_a_document_after_the_budget_is_lowered() -> None:
    previous_document = EMPTY_MEMORY_DOCUMENT.replace(
        "# 项目背景",
        "# 项目背景\n\n- " + ("x" * 9_000),
    )
    work = replace(
        _work(),
        base_content=previous_document,
        base_content_digest=memory_document_digest(previous_document),
    )
    factory = _SessionFactory()
    state = _RepositoryState(work)
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    assert runner.inputs[0].document == previous_document
    assert runner.inputs[0].max_tokens == 2_000
    await settlement.commit()
    assert state.finalized[0]["content"] == EMPTY_MEMORY_DOCUMENT


@pytest.mark.asyncio
async def test_dream_worker_uses_frozen_custom_sections_for_input_and_settlement() -> None:
    sections = ("协作方式", "架构边界", "当前目标")
    content = render_empty_memory_document(sections)
    work = replace(
        _work(),
        base_content=content,
        base_content_digest=memory_document_digest(content),
        sections=sections,
    )
    factory = _SessionFactory()
    state = _RepositoryState(work)
    runner = _Runner(
        factory,
        result=MemoryDreamResult(content=content, replaced=True),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    assert runner.inputs[0].sections == sections
    await settlement.commit()
    assert state.finalized[0]["expected_sections"] == sections


@pytest.mark.asyncio
async def test_dream_worker_rejects_output_with_non_frozen_sections() -> None:
    sections = ("协作方式", "架构边界", "当前目标")
    content = render_empty_memory_document(sections)
    work = replace(
        _work(),
        base_content=content,
        base_content_digest=memory_document_digest(content),
        sections=sections,
    )
    factory = _SessionFactory()
    state = _RepositoryState(work)
    runner = _Runner(
        factory,
        result=MemoryDreamResult(content=EMPTY_MEMORY_DOCUMENT, replaced=True),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.public_error_code == "MEMORY_DREAM_OUTPUT_INVALID"
    await settlement.commit()
    assert state.finalized == []


@pytest.mark.asyncio
async def test_success_commit_policy_drift_persists_cancellation_not_version() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        current_policy_revision=18,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    await settlement.commit()
    assert not state.finalized
    assert len(state.released) == 1
    assert state.released[0]["job_id"] == _claim().job_id
    assert state.released[0]["lease_token"] == _claim().lease_token
    assert state.released[0]["cancelled"] is True


@pytest.mark.asyncio
async def test_success_commit_does_not_recheck_the_current_model() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "succeeded"
    await settlement.commit()
    assert len(state.finalized) == 1
    assert not state.released


@pytest.mark.asyncio
async def test_success_commit_policy_unavailable_rolls_back_then_retries() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        current_policy_error=SystemRuntimePolicyUnavailable(),
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)

    await settlement.commit()

    assert not state.finalized
    assert len(state.released) == 1
    assert state.released[0]["cancelled"] is False
    assert state.released[0]["public_error_code"] == "MEMORY_DREAM_POLICY_UNAVAILABLE"
    assert factory.opened == 3
    assert factory.transaction_exits[0] is None
    assert factory.transaction_exits[1] is not None
    assert factory.transaction_exits[2] is None


@pytest.mark.asyncio
async def test_success_commit_audit_failure_rolls_back_version_then_retries() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.needs_review = True
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    audit = _Audit(error=RuntimeError("audit storage unavailable"))
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        audit=audit,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)

    await settlement.commit()

    assert not state.finalized
    assert len(audit.calls) == 1
    assert len(audit.settled_calls) == 1
    assert len(state.released) == 1
    assert state.released[0]["cancelled"] is False
    assert state.released[0]["public_error_code"] == "MEMORY_DREAM_AUDIT_UNAVAILABLE"
    assert factory.opened == 3
    assert factory.transaction_exits[1] is not None
    assert factory.transaction_exits[2] is None


@pytest.mark.asyncio
async def test_published_lifecycle_audit_failure_rolls_back_then_retries() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    audit = _Audit(
        settled_error=RuntimeError("audit storage unavailable"),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        audit=audit,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)
    await settlement.commit()

    assert not state.finalized
    assert len(audit.settled_calls) == 1
    assert len(state.released) == 1
    assert state.released[0]["public_error_code"] == "MEMORY_DREAM_AUDIT_UNAVAILABLE"
    assert state.released[0]["retryable"] is True


@pytest.mark.asyncio
async def test_success_commit_invariant_conflict_rolls_back_then_dies() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.finalize_error = MemoryDreamSettlementInvariant(
        "settlement contract is impossible",
    )
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)

    await settlement.commit()

    assert not state.finalized
    assert len(state.released) == 1
    assert state.released[0]["cancelled"] is False
    assert state.released[0]["public_error_code"] == "MEMORY_DREAM_SETTLEMENT_INVARIANT"
    assert state.released[0]["retryable"] is False
    assert factory.opened == 3


@pytest.mark.asyncio
async def test_success_commit_stale_conflict_rolls_back_then_cancels() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.finalize_error = MemoryDreamStaleConflict("frozen document changed")
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)

    await settlement.commit()

    assert not state.finalized
    assert len(state.released) == 1
    assert state.released[0]["cancelled"] is True
    assert state.released[0]["public_error_code"] == "MEMORY_DREAM_STALE"
    assert state.released[0]["retryable"] is False


@pytest.mark.asyncio
async def test_success_commit_lease_conflict_does_not_retry() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.finalize_error = MemoryDreamLeaseConflict("lease changed")
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())
    assert isinstance(settlement, JobSettlement)

    with pytest.raises(LeaseLost):
        await settlement.commit()

    assert not state.finalized
    assert not state.released
    assert factory.opened == 2


@pytest.mark.asyncio
async def test_release_settlement_maps_only_lease_conflict_to_lease_lost() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    state.release_error = MemoryDreamLeaseConflict("lease changed")
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )
    settlement = handler._release_settlement(
        _claim(),
        cancelled=False,
    )

    with pytest.raises(LeaseLost):
        await settlement.commit()


@pytest.mark.asyncio
async def test_release_settlement_preserves_invariant_conflict() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    conflict = MemoryDreamSettlementInvariant("terminal state invalid")
    state.release_error = conflict
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )
    settlement = handler._release_settlement(
        _claim(),
        cancelled=False,
    )

    with pytest.raises(MemoryDreamSettlementInvariant):
        await settlement.commit()
    # The first invariant rolls back, then one fresh non-retryable release is
    # attempted.  A repeated invariant is preserved for operators.
    assert factory.opened == 2


@pytest.mark.asyncio
async def test_release_settlement_locks_scope_before_memory_release() -> None:
    events: list[str] = []
    factory = _SessionFactory()
    state = _RepositoryState(_work())

    async def scope_validator(*_args, **kwargs):
        assert kwargs["lock"] is True
        events.append("scope")
        return True

    class Repository(_Repository):
        async def release_dream(self, scope, **kwargs):
            events.append("release")
            return await super().release_dream(scope, **kwargs)

    handler = MemoryDreamJobHandler(
        factory,
        app_config=None,
        runner_factory=lambda _model: object(),
        repository_builder=lambda session, **_kwargs: Repository(
            state,
            session=session,
        ),
        job_repository_builder=lambda _session: object(),
        scope_validator=scope_validator,
    )

    settlement = handler._release_settlement(
        _claim(),
        cancelled=False,
    )
    await settlement.commit()

    assert events == ["scope", "release"]


def test_dream_worker_rejects_partial_audit_port() -> None:
    class ReviewOnlyAudit:
        async def memory_dream_review_flagged(self, *_args, **_kwargs):
            return None

    with pytest.raises(ValueError, match="Dream Worker audit port is invalid"):
        MemoryDreamJobHandler(
            _SessionFactory(),
            app_config=None,
            runner_factory=lambda _model: object(),
            audit=ReviewOnlyAudit(),
        )


@pytest.mark.asyncio
async def test_success_settlement_uses_global_memory_lock_order() -> None:
    events: list[str] = []
    factory = _SessionFactory()
    work = _work()

    async def scope_validator(*_args, **kwargs):
        assert kwargs["lock"] is True
        events.append("project")
        return True

    class PolicyMaterializer(_PolicyMaterializer):
        async def materialize_current_with_revision_in_session(
            self,
            *_args,
            **kwargs,
        ):
            assert kwargs["for_update"] is True
            events.append("policy")
            return self.policy, self.current_revision

    class Personalization(_Personalization):
        async def read_memory(self, owner, *, for_update: bool = False):
            assert for_update is True
            events.append("preference")
            return await super().read_memory(owner, for_update=for_update)

    class Repository(_Repository):
        async def finalize_dream(self, scope, **kwargs):
            events.append("document")
            return await super().finalize_dream(scope, **kwargs)

    policy = AgentRuntimePolicyValue(
        memory=MemoryPolicy(
            enabled=True,
            model_name=DREAM_MODEL_REF,
            dream_interval_minutes=120,
            max_injection_tokens=2_000,
        )
    )
    state = _RepositoryState(work)
    handler = MemoryDreamJobHandler(
        factory,
        app_config=None,
        runner_factory=lambda _model: object(),
        runtime_policy_materializer=PolicyMaterializer(policy),
        repository_builder=lambda session, **_kwargs: Repository(
            state,
            session=session,
        ),
        job_repository_builder=lambda _session: object(),
        scope_validator=scope_validator,
        personalization_repository_builder=lambda _session: Personalization(
            AccountMemoryPreference(True, 6),
        ),
    )

    settlement = handler._success_settlement(
        _claim(),
        work=work,
        content=EMPTY_MEMORY_DOCUMENT,
        max_tokens=2_000,
        episode_retention_days=0,
    )
    await settlement.commit()

    assert events == [
        "project",
        "policy",
        "preference",
        "document",
    ]


@pytest.mark.asyncio
async def test_dream_worker_cancel_releases_processing_history_without_model() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(_work())
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, _materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
        preference=AccountMemoryPreference(False, 6),
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "cancelled"
    await settlement.commit()
    assert not runner.inputs
    assert state.released[0]["cancelled"] is True


@pytest.mark.asyncio
async def test_dream_worker_prompt_version_drift_cancels_without_retry_or_model() -> None:
    factory = _SessionFactory()
    state = _RepositoryState(
        replace(
            _work(),
            prompt_version="dream-prompt-retired",
        )
    )
    runner = _Runner(
        factory,
        result=MemoryDreamResult(
            content=EMPTY_MEMORY_DOCUMENT,
            replaced=True,
        ),
    )
    handler, model_materializer = _handler(
        factory=factory,
        state=state,
        runner=runner,
    )

    settlement = await handler(_claim(), _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome.status == "cancelled"
    assert not model_materializer.frozen
    assert not runner.inputs
    await settlement.commit()
    assert not state.finalized
    assert state.released == [
        {
            "job_id": _claim().job_id,
            "lease_token": _claim().lease_token,
            "now": state.released[0]["now"],
            "cancelled": True,
            "public_error_code": "MEMORY_DREAM_CANCELLED",
            "retryable": False,
            "retry_initial_seconds": 5,
            "retry_max_seconds": 300,
        }
    ]


def test_server_diff_is_empty_for_no_change_and_real_for_replacement() -> None:
    assert memory_document_unified_diff(EMPTY_MEMORY_DOCUMENT, EMPTY_MEMORY_DOCUMENT) == ""
    changed = EMPTY_MEMORY_DOCUMENT.replace("# 项目背景", "# 项目背景\n\n- ActWeave")
    diff = memory_document_unified_diff(EMPTY_MEMORY_DOCUMENT, changed)
    assert "--- memory-before.md" in diff
    assert "+++ memory-after.md" in diff
    assert "+- ActWeave" in diff


def test_memory_document_diff_preview_is_line_safe_and_bounded() -> None:
    oversized = "--- memory-before.md\n+++ memory-after.md\n" + ("+" + "x" * 100 + "\n") * 700

    preview, truncated = memory_document_diff_preview(oversized)

    assert truncated is True
    assert len(preview) <= 64_000
    assert preview.endswith("\n")
    assert oversized.startswith(preview)
    assert memory_document_diff_preview("@@ -1 +1 @@\n-old\n+new\n") == (
        "@@ -1 +1 @@\n-old\n+new\n",
        False,
    )


def test_memory_document_diff_preview_uses_versioned_unicode_length() -> None:
    astral_diff = "😀\n" * 22_000

    preview, truncated = memory_document_diff_preview(astral_diff)
    legacy_preview, legacy_truncated = memory_document_diff_preview(
        astral_diff,
        legacy_utf16=True,
    )

    assert len(astral_diff) == 44_000
    assert len(astral_diff.encode("utf-16-le")) // 2 == 66_000
    assert (preview, truncated) == (astral_diff, False)
    assert legacy_truncated is True
    assert len(legacy_preview.encode("utf-16-le")) // 2 <= 64_000
    assert legacy_preview.endswith("\n")


def test_valid_memory_documents_can_generate_a_diff_over_public_limit() -> None:
    sections = ("A", "B")
    empty = render_empty_memory_document(sections)
    padding = "\n" * (15_995 - len(empty))
    before = padding + empty
    after = empty.replace("# B", padding + "# B")

    validate_memory_document(before, 8_000, sections=sections)
    validate_memory_document(after, 8_000, sections=sections)
    unified_diff = memory_document_unified_diff(before, after)
    preview, truncated = memory_document_diff_preview(unified_diff)

    assert len(before) == len(after) == 15_995
    assert len(unified_diff) > 64_000
    assert truncated is True
    assert len(preview) <= 64_000
