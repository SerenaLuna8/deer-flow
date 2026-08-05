from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.system_runtime_settings.models import AgentRuntimePolicyValue
from app.worker.memory_consolidate import (
    MemoryConsolidateJobHandler,
    MemoryRetentionPurgeJobHandler,
)
from app.worker.service import JobOutcome, JobSettlement
from deerflow.agents.memory.consolidator import (
    MemoryConsolidationDecision,
    MemoryConsolidationResult,
)
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryConsolidationCandidateRecord,
    MemoryConsolidationFactRecord,
    MemoryConsolidationWork,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    def begin_nested(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _PersonalizationRepository:
    def __init__(self, *preferences: tuple[bool, int]) -> None:
        self.preferences = list(preferences or ((True, 1),))
        self.calls: list[dict[str, object]] = []

    async def read_memory(self, _user_id, **kwargs):
        self.calls.append(kwargs)
        enabled, version = self.preferences.pop(0) if len(self.preferences) > 1 else self.preferences[0]
        return SimpleNamespace(memory_enabled=enabled, version=version)


def _enabled_personalization_repository(_session) -> _PersonalizationRepository:
    return _PersonalizationRepository()


class _Authority:
    def __init__(self, *, cancel_requested: bool = False) -> None:
        self.cancel_requested = cancel_requested
        self.heartbeats = 0

    async def heartbeat(self) -> None:
        self.heartbeats += 1


class _Repository:
    def __init__(self, work: MemoryConsolidationWork) -> None:
        self.work = work
        self.consolidation_finalized: list[dict[str, object]] = []
        self.retention_finalized: list[dict[str, object]] = []

    async def load_consolidation_work(self, **_kwargs):
        return self.work

    async def finalize_consolidation(self, **kwargs):
        self.consolidation_finalized.append(kwargs)
        return SimpleNamespace(status="succeeded")

    async def finalize_retention(self, **kwargs):
        self.retention_finalized.append(kwargs)
        return SimpleNamespace(status="succeeded")


class _PolicyMaterializer:
    def __init__(self, *, current_mode: str = "consolidate") -> None:
        base = AgentRuntimePolicyValue()
        self.current = base.model_copy(update={"memory": base.memory.model_copy(update={"enabled": True, "pipeline_mode": current_mode})})
        self.frozen = base.model_copy(update={"memory": base.memory.model_copy(update={"enabled": True, "pipeline_mode": "consolidate"})})
        self.current_calls: list[object] = []
        self.current_in_session_for_update: list[bool] = []
        self.revision_calls: list[tuple[object, int]] = []
        self.current_results: list[AgentRuntimePolicyValue] = []

    async def materialize_current(self, section):
        self.current_calls.append(section)
        if self.current_results:
            return self.current_results.pop(0)
        return self.current

    async def materialize_current_in_session(
        self,
        _session,
        section,
        *,
        for_update=False,
    ):
        self.current_in_session_for_update.append(for_update)
        return await self.materialize_current(section)

    async def materialize_revision(self, section, revision):
        self.revision_calls.append((section, revision))
        return self.frozen


class _ModelMaterializer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def materialize_exact(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(name="memory-test")


class _Consolidator:
    def __init__(self, result: MemoryConsolidationResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[object, object]] = []

    async def consolidate(self, candidates, facts):
        self.calls.append((candidates, facts))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _claim(job_type: str = "memory_consolidate") -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="memory-lease",
        job_type=job_type,
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=None,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        namespace="default",
    )


def _work(claim: JobClaim) -> MemoryConsolidationWork:
    candidate = MemoryConsolidationCandidateRecord(
        id=uuid.uuid4(),
        candidate_type="preference",
        content="用户偏好中文回答。",
        content_digest="a" * 64,
        confidence=0.95,
        retention_class="durable",
        sensitivity="normal",
        source_item_id=uuid.uuid4(),
        source_identity_hmac="b" * 64,
        thread_id="thread-1",
        run_id="run-1",
        run_event_sequence=None,
    )
    fact = MemoryConsolidationFactRecord(
        id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        revision_number=1,
        fact_kind="preference",
        version=1,
        content="用户偏好简洁回答。",
        content_digest="c" * 64,
        category="preference",
        confidence=0.9,
        last_confirmed_at=None,
    )
    return MemoryConsolidationWork(
        generation_id=uuid.uuid4(),
        job_id=claim.job_id,
        project_id=claim.scope.project_id,
        owner_user_id=claim.scope.owner_user_id or "",
        namespace=claim.namespace or "",
        candidate_input_digest="d" * 64,
        contract_digest="e" * 64,
        policy_revision=7,
        model_config_id=uuid.uuid4(),
        model_config_version_id=uuid.uuid4(),
        model_config_checksum="f" * 64,
        prompt_version="memory-consolidate-prompt-v2",
        consolidator_version="memory-consolidator-v2",
        output_schema_version="memory-consolidate-output-v2",
        candidates=(candidate,),
        facts=(fact,),
        active_fact_count=1,
        suppressed=False,
        cancel_requested=False,
        fact_committed=False,
    )


async def _scope_allowed(_session, _claim, *, lock: bool) -> bool:
    assert isinstance(lock, bool)
    return True


async def _scope_unavailable(_session, _claim, *, lock: bool) -> bool:
    assert lock is False
    return False


@pytest.mark.asyncio
async def test_consolidate_worker_uses_frozen_policy_and_exact_model() -> None:
    claim = _claim()
    work = _work(claim)
    repository = _Repository(work)
    policy = _PolicyMaterializer()
    model = _ModelMaterializer()
    candidate = work.candidates[0]
    consolidator = _Consolidator(
        MemoryConsolidationResult(
            decisions=(
                MemoryConsolidationDecision(
                    candidate_id=candidate.id,
                    action="create",
                    target_fact_id=None,
                    content=candidate.content,
                    category="preference",
                    confidence=0.95,
                    change_reason="new_fact",
                    decision_reason=None,
                ),
            )
        )
    )
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=policy,
        consolidator_factory=lambda _model: consolidator,
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome == JobOutcome.succeeded()
    assert policy.revision_calls[0][1] == 7
    assert model.calls == [
        {
            "model_config_id": work.model_config_id,
            "model_config_version_id": work.model_config_version_id,
            "payload_checksum": work.model_config_checksum,
        }
    ]
    await settlement.commit()
    finalized = repository.consolidation_finalized[0]
    assert finalized["max_facts"] == 100
    assert finalized["fact_confidence_threshold"] == 0.7
    assert finalized["decisions"][0].action == "create"
    assert finalized["release_candidates_on_cancel"] is False


@pytest.mark.asyncio
async def test_consolidate_worker_rejects_sensitive_candidate_without_model() -> None:
    claim = _claim()
    work = _work(claim)
    work = replace(
        work,
        candidates=(replace(work.candidates[0], sensitivity="sensitive"),),
    )
    repository = _Repository(work)
    consolidator = _Consolidator(MemoryConsolidationResult(decisions=()))
    policy = _PolicyMaterializer()
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=policy,
        consolidator_factory=lambda _model: consolidator,
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())
    await settlement.commit()

    assert consolidator.calls == []
    decision = repository.consolidation_finalized[0]["decisions"][0]
    assert decision.action == "reject"
    assert decision.decision_reason == "sensitive_content"


@pytest.mark.asyncio
async def test_consolidate_worker_pause_releases_bound_backlog() -> None:
    claim = _claim()
    repository = _Repository(_work(claim))
    policy = _PolicyMaterializer(current_mode="off")
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=policy,
        consolidator_factory=lambda _model: _Consolidator(MemoryConsolidationResult(decisions=())),
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())
    assert settlement.outcome == JobOutcome.cancelled()
    await settlement.commit()

    finalized = repository.consolidation_finalized[0]
    assert finalized["cancel"] is True
    assert finalized["release_candidates_on_cancel"] is True


@pytest.mark.asyncio
async def test_retention_worker_defers_atomic_terminal_body_erasure() -> None:
    claim = _claim("memory_retention_purge")
    repository = _Repository(_work(replace(claim, job_type="memory_consolidate")))
    policy = _PolicyMaterializer()
    handler = MemoryRetentionPurgeJobHandler(
        _SessionFactory(),
        runtime_policy_materializer=policy,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())

    assert settlement.outcome == JobOutcome.succeeded()
    assert repository.retention_finalized == []
    await settlement.commit()
    assert repository.retention_finalized[0]["cancel"] is False


@pytest.mark.asyncio
async def test_consolidate_worker_rechecks_pause_at_commit() -> None:
    claim = _claim()
    work = _work(claim)
    repository = _Repository(work)
    policy = _PolicyMaterializer()
    candidate = work.candidates[0]
    consolidator = _Consolidator(
        MemoryConsolidationResult(
            decisions=(
                MemoryConsolidationDecision(
                    candidate_id=candidate.id,
                    action="create",
                    target_fact_id=None,
                    content=candidate.content,
                    category="preference",
                    confidence=0.95,
                    change_reason="new_fact",
                    decision_reason=None,
                ),
            )
        )
    )
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=policy,
        consolidator_factory=lambda _model: consolidator,
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_allowed,
    )

    paused = policy.current.model_copy(
        update={
            "memory": policy.current.memory.model_copy(
                update={"pipeline_mode": "off"},
            )
        }
    )
    policy.current_results = [policy.current, policy.current]
    settlement = await handler(claim, _Authority())
    policy.current_results = [paused]
    await settlement.commit()

    finalized = repository.consolidation_finalized[0]
    assert finalized["cancel"] is True
    assert finalized["release_candidates_on_cancel"] is True
    assert finalized["decisions"] == ()
    assert policy.current_in_session_for_update == [True]


@pytest.mark.asyncio
async def test_consolidate_worker_skips_model_when_account_memory_is_disabled() -> None:
    claim = _claim()
    repository = _Repository(_work(claim))
    personalization = _PersonalizationRepository((False, 3))
    model = _ModelMaterializer()
    consolidator = _Consolidator(MemoryConsolidationResult(decisions=()))
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=_PolicyMaterializer(),
        consolidator_factory=lambda _model: consolidator,
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=lambda _session: personalization,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())

    assert isinstance(settlement, JobSettlement)
    assert settlement.outcome == JobOutcome.cancelled()
    assert model.calls == []
    assert consolidator.calls == []
    await settlement.commit()
    finalized = repository.consolidation_finalized[0]
    assert finalized["cancel"] is True
    assert finalized["release_candidates_on_cancel"] is True


@pytest.mark.asyncio
async def test_consolidate_worker_discards_result_after_preference_version_changes() -> None:
    claim = _claim()
    work = _work(claim)
    repository = _Repository(work)
    personalization = _PersonalizationRepository((True, 1), (True, 2))
    candidate = work.candidates[0]
    consolidator = _Consolidator(
        MemoryConsolidationResult(
            decisions=(
                MemoryConsolidationDecision(
                    candidate_id=candidate.id,
                    action="create",
                    target_fact_id=None,
                    content=candidate.content,
                    category="preference",
                    confidence=0.95,
                    change_reason="new_fact",
                    decision_reason=None,
                ),
            )
        )
    )
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        consolidator_factory=lambda _model: consolidator,
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=lambda _session: personalization,
        scope_validator=_scope_allowed,
    )

    settlement = await handler(claim, _Authority())
    await settlement.commit()

    assert len(consolidator.calls) == 1
    finalized = repository.consolidation_finalized[0]
    assert finalized["cancel"] is True
    assert finalized["decisions"] == ()
    assert finalized["release_candidates_on_cancel"] is True
    assert personalization.calls == [{}, {"for_update": True}]


@pytest.mark.asyncio
async def test_retention_worker_rechecks_pause_at_commit() -> None:
    claim = _claim("memory_retention_purge")
    repository = _Repository(_work(replace(claim, job_type="memory_consolidate")))
    policy = _PolicyMaterializer()
    handler = MemoryRetentionPurgeJobHandler(
        _SessionFactory(),
        runtime_policy_materializer=policy,
        repository_builder=lambda _session, **_kwargs: repository,
        scope_validator=_scope_allowed,
    )

    paused = policy.current.model_copy(
        update={
            "memory": policy.current.memory.model_copy(
                update={"pipeline_mode": "shadow"},
            )
        }
    )
    policy.current_results = [policy.current, policy.current]
    settlement = await handler(claim, _Authority())
    policy.current_results = [paused]
    await settlement.commit()

    assert repository.retention_finalized[0]["cancel"] is True
    assert policy.current_in_session_for_update == [True]


def test_consolidate_worker_keeps_only_one_revision_per_fact() -> None:
    claim = _claim()
    original = _work(claim)
    first = original.candidates[0]
    second = replace(
        first,
        id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_identity_hmac="9" * 64,
    )
    work = replace(original, candidates=(first, second))
    fact = work.facts[0]
    decisions = tuple(
        MemoryConsolidationDecision(
            candidate_id=candidate.id,
            action="revise",
            target_fact_id=fact.id,
            content=f"修订 {index}",
            category="preference",
            confidence=0.95,
            change_reason="correction",
            decision_reason=None,
        )
        for index, candidate in enumerate(work.candidates, start=1)
    )

    writes = MemoryConsolidateJobHandler._writes(
        work,
        decisions,
        max_facts=100,
        confidence_threshold=0.7,
    )

    assert writes[0].action == "revise"
    assert writes[1].action == "pending"
    assert writes[1].decision_reason == "possible_conflict"


@pytest.mark.parametrize("action", ["create", "confirm", "revise"])
@pytest.mark.parametrize("reason", ["ephemeral", "low_candidate_confidence"])
def test_consolidate_worker_keeps_weak_candidates_pending(
    action: str,
    reason: str,
) -> None:
    claim = _claim()
    original = _work(claim)
    candidate = replace(
        original.candidates[0],
        retention_class=("ephemeral" if reason == "ephemeral" else "durable"),
        confidence=(0.95 if reason == "ephemeral" else 0.69),
    )
    work = replace(original, candidates=(candidate,))
    fact = work.facts[0]
    decision = MemoryConsolidationDecision(
        candidate_id=candidate.id,
        action=action,
        target_fact_id=(None if action == "create" else fact.id),
        content=(None if action == "confirm" else candidate.content),
        category=(None if action == "confirm" else "preference"),
        confidence=(None if action == "confirm" else 0.99),
        change_reason=("new_fact" if action == "create" else None if action == "confirm" else "correction"),
        decision_reason=("same_fact" if action == "confirm" else None),
    )

    writes = MemoryConsolidateJobHandler._writes(
        work,
        (decision,),
        max_facts=100,
        confidence_threshold=0.7,
    )

    assert writes[0].action == "pending"
    assert writes[0].decision_reason == "insufficient_evidence"


@pytest.mark.asyncio
async def test_consolidate_worker_retries_temporarily_unavailable_scope() -> None:
    claim = _claim()
    repository = _Repository(_work(claim))
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=_ModelMaterializer(),
        runtime_policy_materializer=_PolicyMaterializer(),
        consolidator_factory=lambda _model: _Consolidator(
            MemoryConsolidationResult(decisions=()),
        ),
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_unavailable,
    )

    result = await handler(claim, _Authority())

    assert result == JobOutcome.failed("MEMORY_CONSOLIDATE_SCOPE_UNAVAILABLE")
    assert repository.consolidation_finalized == []


@pytest.mark.asyncio
async def test_consolidate_worker_rejects_unknown_contract_before_model() -> None:
    claim = _claim()
    work = replace(_work(claim), prompt_version="memory-consolidate-prompt-v0")
    repository = _Repository(work)
    model = _ModelMaterializer()
    handler = MemoryConsolidateJobHandler(
        _SessionFactory(),
        app_config=None,
        model_materializer=model,
        runtime_policy_materializer=_PolicyMaterializer(),
        consolidator_factory=lambda _model: _Consolidator(MemoryConsolidationResult(decisions=())),
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=_enabled_personalization_repository,
        scope_validator=_scope_allowed,
    )

    result = await handler(claim, _Authority())

    assert result == JobOutcome.failed("MEMORY_CONSOLIDATE_CONTRACT_UNSUPPORTED")
    assert model.calls == []
    assert repository.consolidation_finalized == []
