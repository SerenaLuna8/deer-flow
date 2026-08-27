from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.private_work import context_evidence_observer as observer_module
from app.private_work.context import PrivateWorkContext
from app.private_work.context_evidence_observer import (
    ContextProviderCallAmbiguous,
    PrivateRunContextEvidenceObserver,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextEvidenceScope,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CompactionCommittedV1,
    ContextCheckpointEstimator,
    ContextContribution,
    ContextLane,
    ContextRebaseReason,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionPhase,
    ProviderAmbiguityReason,
    ProviderAmbiguousV1,
    ProviderCallIdentity,
    ProviderFailedV1,
    ProviderObservedV1,
    ProviderRetrySafety,
    RequestDispatchedV1,
    RequestPreparedV1,
    TokenEstimate,
    WindowOpenedV1,
    WindowRebasedV1,
)

THREAD_ID = "11111111-1111-4111-8111-111111111111"


def _context() -> PrivateWorkContext:
    role = ProjectRole.RUNNER
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="context-evidence-observer",
        )
    )


def _measurement() -> FinalRequestMeasurement:
    return FinalRequestMeasurement(
        request_fingerprint="a" * 64,
        adapter_revision="observer-test-v1",
        contributions=(
            ContextContribution(
                contribution_id="b" * 64,
                source_identity_digest="c" * 64,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=40,
                token_estimate=TokenEstimate.exact(10),
            ),
        ),
    )


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _Boundary:
    def __init__(self) -> None:
        self.calls = 0
        self.active_calls = 0
        self.settlement_calls = 0

    async def lock_and_assert_materialization_active_in_session(
        self,
        _session: object,
        _context: object,
    ) -> None:
        self.calls += 1
        self.active_calls += 1

    async def lock_and_assert_context_evidence_settlement_in_session(
        self,
        _session: object,
        _context: object,
    ) -> None:
        self.calls += 1
        self.settlement_calls += 1


class _Revalidator:
    async def require(self, *_args: object, **_kwargs: object) -> object:
        return object()


class _Repository:
    async def read_head(self, *_args: object, **_kwargs: object) -> None:
        return None

    @property
    def records(self) -> tuple[ContextEvidenceRecord, ...]:
        return ()

    async def page_evidence(self, *_args: object, **_kwargs: object) -> tuple[()]:
        raise AssertionError("Observer hot paths must not scan Thread Evidence")

    async def read_latest_subject_evidence(
        self,
        _scope: object,
        subject: ContextSubjectRef,
    ) -> ContextEvidenceRecord | None:
        matching = [record for record in self.records if record.subject == subject]
        return matching[-1] if matching else None

    async def page_subject_event_evidence(
        self,
        _scope: object,
        subject: ContextSubjectRef,
        event_type: str,
        *,
        origin_run_id: str | None,
        generation_id: uuid.UUID | None,
        checkpoint_id: str | None,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.subject == subject
            and record.event_type == event_type
            and record.evidence_seq > after_seq
            and (origin_run_id is None or record.origin_run_id == origin_run_id)
            and (generation_id is None or record.context_window_generation == generation_id)
            and (checkpoint_id is None or record.checkpoint_id == checkpoint_id)
        )[:limit]

    async def page_provider_call_evidence(
        self,
        _scope: object,
        subject: ContextSubjectRef,
        provider_call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        return tuple(record for record in self.records if record.subject == subject and record.provider_call_id == provider_call_id and record.evidence_seq > after_seq)[:limit]

    async def count_subject_run_prepared_requests(
        self,
        _scope: object,
        subject: ContextSubjectRef,
        origin_run_id: str,
    ) -> int:
        return sum(1 for record in self.records if record.subject == subject and record.origin_run_id == origin_run_id and record.event_type == "request.prepared.v1")


class _RecoveryRepository(_Repository):
    def __init__(
        self,
        records: tuple[ContextEvidenceRecord, ...],
        *,
        head: object | None = None,
    ) -> None:
        self._records = records
        self.head = head

    @property
    def records(self) -> tuple[ContextEvidenceRecord, ...]:
        return self._records

    async def read_head(self, *_args: object, **_kwargs: object) -> object | None:
        return self.head


class _HeadRepository(_Repository):
    async def read_head(self, *_args: object, **_kwargs: object):
        return SimpleNamespace(
            context_window_generation=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            checkpoint_id="checkpoint-current",
        )


class _ProjectionTransaction:
    calls: list[tuple[str, tuple[object, ...], object]] = []
    active_run_ids: list[object] = []

    def __init__(self, repository: object) -> None:
        self.repository = repository

    async def append_only(self, **kwargs: object) -> tuple[()]:
        self.calls.append(("append", tuple(kwargs["payloads"]), kwargs["source"]))
        return ()

    async def append_and_project(self, **kwargs: object) -> object:
        self.calls.append(("project", tuple(kwargs["payloads"]), kwargs["source"]))
        self.active_run_ids.append(kwargs.get("active_run_id"))
        return object()


@pytest.mark.asyncio
async def test_worker_observer_acks_every_provider_lifecycle_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=_context(),
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-1",
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    provider_call = await observer.record_request_prepared(_measurement())
    await observer.record_request_dispatched(provider_call)
    await observer.record_provider_observed(provider_call, input_tokens=11)
    checkpoint_snapshot = observer.checkpoint_projection_snapshot(
        estimator=ContextCheckpointEstimator(
            error_allowance_ratio=0.2,
            provider_fixed_overhead_tokens=10,
            provider_per_message_overhead_tokens=2,
            provider_per_tool_overhead_tokens=3,
            fixed_message_count=1,
            tool_count=4,
        ),
        provider_call=provider_call,
        origin_run_id="run-1",
        provider_response_message_start=1,
        provider_response_message_count=1,
        provider_response_digest="e" * 64,
    )
    observer.accept_checkpoint_linked(
        provider_call_id=provider_call.provider_call_id,
        checkpoint_id="checkpoint-2",
    )

    assert boundary.calls == 3
    assert boundary.active_calls == 2
    assert boundary.settlement_calls == 1
    assert [kind for kind, _payloads, _source in _ProjectionTransaction.calls] == [
        "append",
        "append",
        "project",
    ]
    prepared_payloads = _ProjectionTransaction.calls[0][1]
    assert isinstance(prepared_payloads[0], WindowOpenedV1)
    assert isinstance(prepared_payloads[1], RequestPreparedV1)
    assert isinstance(_ProjectionTransaction.calls[1][1][0], RequestDispatchedV1)
    assert isinstance(_ProjectionTransaction.calls[2][1][0], ProviderObservedV1)
    assert _ProjectionTransaction.calls[2][1][0].input_tokens == 11
    assert observer._source_checkpoint_id == "checkpoint-2"
    assert checkpoint_snapshot.generation == provider_call.generation
    assert checkpoint_snapshot.measurement == _measurement()
    assert checkpoint_snapshot.estimator.fixed_message_count == 1
    assert checkpoint_snapshot.provider_call_id == provider_call.provider_call_id
    assert checkpoint_snapshot.provider_subject == provider_call.subject
    assert checkpoint_snapshot.provider_response_digest == "e" * 64


@pytest.mark.asyncio
async def test_settlement_before_first_provider_call_publishes_empty_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=_context(),
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-before-provider",
        subject=ContextSubject.subagent_task(
            thread_id=THREAD_ID,
            execution_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        ),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
        source_checkpoint_id="task-state:55555555-5555-4555-8555-555555555555",
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    await observer.record_settled()

    assert boundary.calls == 1
    assert boundary.active_calls == 0
    assert boundary.settlement_calls == 1
    assert len(_ProjectionTransaction.calls) == 1
    kind, payloads, source = _ProjectionTransaction.calls[0]
    assert kind == "project"
    assert len(payloads) == 1
    assert isinstance(payloads[0], WindowOpenedV1)
    assert source.phase is ProjectionPhase.SETTLED
    assert source.measurement.contributions == ()


@pytest.mark.asyncio
async def test_lead_ambiguity_settlement_projects_idle_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    _ProjectionTransaction.active_run_ids.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(
            get=lambda **_kwargs: _async_value(
                SimpleNamespace(thread_id=THREAD_ID),
            )
        ),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=_context(),
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-ambiguous",
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    provider_call = await observer.record_request_prepared(
        _measurement(),
    )
    await observer.record_request_dispatched(provider_call)
    await observer.record_provider_ambiguous(
        provider_call,
        reason=ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
    )
    await observer.record_settled()

    assert _ProjectionTransaction.calls[-1][2].phase is ProjectionPhase.IDLE
    assert _ProjectionTransaction.active_run_ids[-1] is None


@pytest.mark.asyncio
async def test_subagent_compaction_uses_state_digests_without_fake_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=_context(),
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-task-compaction",
        subject=ContextSubject.subagent_task(
            thread_id=THREAD_ID,
            execution_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        ),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    await observer.record_ephemeral_compaction_committed(
        source_state_digest="a" * 64,
        result_state_digest="b" * 64,
        source_tokens=1000,
        result_tokens=300,
        summary_tokens=80,
        summary_digest="c" * 64,
    )

    assert boundary.calls == 1
    kind, payloads, source = _ProjectionTransaction.calls[0]
    assert kind == "project"
    assert len(payloads) == 2
    assert isinstance(payloads[0], WindowOpenedV1)
    payload = payloads[1]
    assert isinstance(payload, CompactionCommittedV1)
    assert payload.source_checkpoint_id is None
    assert payload.result_checkpoint_id is None
    assert payload.source_state_digest == "a" * 64
    assert payload.result_state_digest == "b" * 64
    assert payload.summary_tokens == 80
    assert source.checkpoint_id is None
    assert source.phase is ProjectionPhase.ACTIVE


@pytest.mark.asyncio
async def test_regeneration_rebases_generation_before_preparing_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _HeadRepository()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=_context(),
        boundary=_Boundary(),
        thread_id=THREAD_ID,
        run_id="run-regenerate",
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
        source_checkpoint_id="checkpoint-historical",
        rebase_reason=ContextRebaseReason.REGENERATION,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    provider_call = await observer.record_request_prepared(_measurement())

    assert provider_call.generation.generation_id != ("33333333-3333-4333-8333-333333333333")
    assert [kind for kind, _payloads, _source in _ProjectionTransaction.calls] == [
        "project",
        "append",
    ]
    rebase = _ProjectionTransaction.calls[0][1][0]
    assert isinstance(rebase, WindowRebasedV1)
    assert rebase.reason is ContextRebaseReason.REGENERATION
    assert rebase.source_checkpoint_id == "checkpoint-current"
    assert rebase.result_checkpoint_id == "checkpoint-historical"
    assert isinstance(_ProjectionTransaction.calls[1][1][0], WindowOpenedV1)


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_state", ["dispatched", "ambiguous"])
async def test_restarted_ambiguous_call_settles_head_without_provider_retry(
    monkeypatch: pytest.MonkeyPatch,
    recovery_state: str,
) -> None:
    context = _context()
    scope = ContextEvidenceScope.from_resource(context.resource_scope, THREAD_ID)
    generation = ContextWindowGeneration(
        generation_id="44444444-4444-4444-8444-444444444444",
    )
    measurement = _measurement()
    provider_call = ProviderCallIdentity.derive(
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        generation=generation,
        source_checkpoint_id="run:run-1",
        graph_step="model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )
    subject_ref = ContextSubjectRef.lead_thread(THREAD_ID)
    prepared_payload = RequestPreparedV1(
        provider_call=provider_call,
        measurement=measurement,
    ).model_dump(mode="json", exclude_none=True)
    dispatched_payload = RequestDispatchedV1(
        provider_call_id=provider_call.provider_call_id,
    ).model_dump(mode="json", exclude_none=True)
    records = [
        ContextEvidenceRecord(
            scope=scope,
            evidence_seq=1,
            subject=subject_ref,
            context_window_generation=uuid.UUID(generation.generation_id),
            event_type="request.prepared.v1",
            origin_run_id="run-1",
            provider_call_id=provider_call.provider_call_id,
            checkpoint_id=None,
            idempotency_key="e" * 64,
            payload_digest="f" * 64,
            payload=prepared_payload,
            occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        ),
        ContextEvidenceRecord(
            scope=scope,
            evidence_seq=2,
            subject=subject_ref,
            context_window_generation=uuid.UUID(generation.generation_id),
            event_type="request.dispatched.v1",
            origin_run_id="run-1",
            provider_call_id=provider_call.provider_call_id,
            checkpoint_id=None,
            idempotency_key="1" * 64,
            payload_digest="2" * 64,
            payload=dispatched_payload,
            occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        ),
    ]
    if recovery_state == "ambiguous":
        ambiguous_payload = ProviderAmbiguousV1(
            provider_call_id=provider_call.provider_call_id,
            reason=ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
        ).model_dump(mode="json", exclude_none=True)
        records.append(
            ContextEvidenceRecord(
                scope=scope,
                evidence_seq=3,
                subject=subject_ref,
                context_window_generation=uuid.UUID(generation.generation_id),
                event_type="provider.ambiguous.v1",
                origin_run_id="run-1",
                provider_call_id=provider_call.provider_call_id,
                checkpoint_id=None,
                idempotency_key="3" * 64,
                payload_digest="4" * 64,
                payload=ambiguous_payload,
                occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
            )
        )
    repository = _RecoveryRepository(
        tuple(records),
        head=SimpleNamespace(
            context_window_generation=uuid.UUID(generation.generation_id),
            checkpoint_id=None,
        ),
    )
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    _ProjectionTransaction.active_run_ids.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=context,
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-1",
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    with pytest.raises(ContextProviderCallAmbiguous):
        await observer.record_request_prepared(measurement)
    await observer.record_settled()

    if recovery_state == "dispatched":
        assert isinstance(
            _ProjectionTransaction.calls[0][1][0],
            ProviderAmbiguousV1,
        )
        assert _ProjectionTransaction.calls[0][1][0].provider_call_id == provider_call.provider_call_id
        assert _ProjectionTransaction.active_run_ids == ["run-1", None]
    else:
        assert _ProjectionTransaction.active_run_ids == [None]
    assert _ProjectionTransaction.calls[-1][0] == "project"
    assert _ProjectionTransaction.calls[-1][2].phase is ProjectionPhase.IDLE


@pytest.mark.asyncio
async def test_worker_restart_derives_ordinal_without_scanning_unrelated_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    scope = ContextEvidenceScope.from_resource(context.resource_scope, THREAD_ID)
    generation = ContextWindowGeneration(
        generation_id="44444444-4444-4444-8444-444444444444",
    )
    measurement = _measurement()
    lead = ContextSubject.lead_thread(thread_id=THREAD_ID)
    lead_ref = ContextSubjectRef.lead_thread(THREAD_ID)
    old_task_ref = ContextSubjectRef.subagent_task(
        "55555555-5555-4555-8555-555555555555",
    )
    failed_call = ProviderCallIdentity.derive(
        subject=lead,
        generation=generation,
        source_checkpoint_id="checkpoint-current",
        graph_step="model",
        model_call_ordinal=0,
        request_fingerprint=measurement.request_fingerprint,
    )

    def record(
        sequence: int,
        subject: ContextSubjectRef,
        payload: object,
        *,
        origin_run_id: str,
        provider_call_id: str | None = None,
    ) -> ContextEvidenceRecord:
        mapping = payload.model_dump(mode="json", exclude_none=True)
        return ContextEvidenceRecord(
            scope=scope,
            evidence_seq=sequence,
            subject=subject,
            context_window_generation=uuid.UUID(generation.generation_id),
            event_type=mapping["event_type"],
            origin_run_id=origin_run_id,
            provider_call_id=provider_call_id,
            checkpoint_id=None,
            idempotency_key=f"{sequence:064x}",
            payload_digest="f" * 64,
            payload=mapping,
            occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        )

    old_window = WindowOpenedV1(
        model_identity_digest="1" * 64,
        context_window_tokens=100_000,
        compaction_enabled=False,
    )
    unrelated = tuple(
        record(
            sequence,
            old_task_ref,
            old_window,
            origin_run_id=f"old-run-{sequence}",
        )
        for sequence in range(1, 2001)
    )
    lifecycle = (
        record(
            2001,
            lead_ref,
            RequestPreparedV1(
                provider_call=failed_call,
                measurement=measurement,
            ),
            origin_run_id="run-1",
            provider_call_id=failed_call.provider_call_id,
        ),
        record(
            2002,
            lead_ref,
            RequestDispatchedV1(
                provider_call_id=failed_call.provider_call_id,
            ),
            origin_run_id="run-1",
            provider_call_id=failed_call.provider_call_id,
        ),
        record(
            2003,
            lead_ref,
            ProviderFailedV1(
                provider_call_id=failed_call.provider_call_id,
                failure_code="CONNECT_REJECTED",
                retry_safety=ProviderRetrySafety.NO_RESPONSE_PROVEN,
            ),
            origin_run_id="run-1",
            provider_call_id=failed_call.provider_call_id,
        ),
    )
    repository = _RecoveryRepository(
        unrelated + lifecycle,
        head=SimpleNamespace(
            context_window_generation=uuid.UUID(generation.generation_id),
            checkpoint_id="checkpoint-current",
        ),
    )
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(
            get=lambda **_kwargs: _async_value(
                SimpleNamespace(thread_id=THREAD_ID),
            ),
        ),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=context,
        boundary=_Boundary(),
        thread_id=THREAD_ID,
        run_id="run-1",
        subject=lead,
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    retry_call = await observer.record_request_prepared(measurement)

    assert retry_call.model_call_ordinal == 1
    assert retry_call.provider_call_id != failed_call.provider_call_id
    assert retry_call.source_checkpoint_id == "checkpoint-current"


@pytest.mark.asyncio
async def test_new_model_facts_open_a_measurement_epoch_in_same_history_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    scope = ContextEvidenceScope.from_resource(context.resource_scope, THREAD_ID)
    generation = ContextWindowGeneration(
        generation_id="44444444-4444-4444-8444-444444444444",
    )
    old_window = WindowOpenedV1(
        model_identity_digest="a" * 64,
        context_window_tokens=100_000,
        compaction_enabled=True,
        compaction_threshold_tokens=80_000,
        compaction_authority="frozen_run",
    )
    repository = _RecoveryRepository(
        (
            ContextEvidenceRecord(
                scope=scope,
                evidence_seq=1,
                subject=ContextSubjectRef.lead_thread(THREAD_ID),
                context_window_generation=uuid.UUID(generation.generation_id),
                event_type="context.window.opened.v1",
                origin_run_id="run-old",
                provider_call_id=None,
                checkpoint_id=None,
                idempotency_key="e" * 64,
                payload_digest="f" * 64,
                payload=old_window.model_dump(mode="json", exclude_none=True),
                occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
            ),
        ),
        head=SimpleNamespace(
            context_window_generation=uuid.UUID(generation.generation_id),
            checkpoint_id="checkpoint-old",
        ),
    )
    boundary = _Boundary()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        observer_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        observer_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    monkeypatch.setattr(
        observer_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    observer = PrivateRunContextEvidenceObserver(
        lambda: _Session(),  # type: ignore[arg-type]
        context=context,
        boundary=boundary,
        thread_id=THREAD_ID,
        run_id="run-new",
        subject=ContextSubject.lead_thread(thread_id=THREAD_ID),
        model_identity_digest="d" * 64,
        context_window_tokens=300_000,
        compaction_enabled=True,
        compaction_threshold_tokens=240_000,
    )
    observer._revalidator = _Revalidator()  # type: ignore[assignment]

    await observer.record_request_prepared(_measurement())

    payloads = _ProjectionTransaction.calls[0][1]
    assert isinstance(payloads[0], WindowOpenedV1)
    assert payloads[0].model_identity_digest == "d" * 64
    assert payloads[0].context_window_tokens == 300_000
    assert isinstance(payloads[1], RequestPreparedV1)
