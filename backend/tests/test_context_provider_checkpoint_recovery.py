from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.private_work import checkpoint_receipt_repair as repair_module
from app.private_work.checkpointer import _ScopedCheckpointSaver
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.provider_request_contract import (
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    provider_response_digest,
)
from deerflow.error_codes import ContextProviderCallAmbiguousError
from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextEvidenceScope,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextModelProjection,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderCallIdentity,
    ProviderObservedV1,
    ProviderUsageUnreportedV1,
    RequestDispatchedV1,
    RequestPreparedV1,
    WindowOpenedV1,
)

THREAD_ID = "11111111-1111-4111-8111-111111111111"
GENERATION = ContextWindowGeneration(
    generation_id="22222222-2222-4222-8222-222222222222",
)
SUBJECT = ContextSubject.lead_thread(thread_id=THREAD_ID)
MEASUREMENT = FinalRequestMeasurement(
    request_fingerprint="a" * 64,
    adapter_revision="checkpoint-recovery-test-v1",
    contributions=(),
)
CALL = ProviderCallIdentity.derive(
    subject=SUBJECT,
    generation=GENERATION,
    source_checkpoint_id="checkpoint-source",
    graph_step="model",
    model_call_ordinal=0,
    request_fingerprint=MEASUREMENT.request_fingerprint,
)


def _context() -> PrivateWorkContext:
    role = ProjectRole.RUNNER
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            project_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="provider-checkpoint-recovery",
        ),
    )


def _snapshot(response: AIMessage) -> ContextCheckpointProjectionSnapshot:
    return ContextCheckpointProjectionSnapshot(
        generation=GENERATION,
        model=ContextModelProjection(
            identity_digest="b" * 64,
            context_window_tokens=300_000,
        ),
        measurement=MEASUREMENT,
        compaction=CompactionProjection(enabled=False, reached=False),
        estimator=ContextCheckpointEstimator(
            error_allowance_ratio=0.2,
            provider_fixed_overhead_tokens=10,
            provider_per_message_overhead_tokens=2,
            provider_per_tool_overhead_tokens=3,
            fixed_message_count=1,
            tool_count=0,
        ),
        provider_call_id=CALL.provider_call_id,
        provider_subject=SUBJECT,
        origin_run_id="run-1",
        provider_response_message_start=1,
        provider_response_message_count=1,
        provider_response_digest=provider_response_digest((response,)),
    )


def _record(sequence: int, payload: object) -> ContextEvidenceRecord:
    mapping = payload.model_dump(mode="json", exclude_none=True)
    provider_call_id = mapping.get("provider_call_id")
    provider_call = mapping.get("provider_call")
    if provider_call_id is None and isinstance(provider_call, dict):
        provider_call_id = provider_call.get("provider_call_id")
    checkpoint_id = mapping.get("checkpoint_id")
    return ContextEvidenceRecord(
        scope=ContextEvidenceScope(
            project_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            owner_user_id="44444444-4444-4444-8444-444444444444",
            thread_id=THREAD_ID,
        ),
        evidence_seq=sequence,
        subject=ContextSubjectRef.lead_thread(THREAD_ID),
        context_window_generation=uuid.UUID(GENERATION.generation_id),
        event_type=mapping["event_type"],
        origin_run_id="run-1",
        provider_call_id=(provider_call_id if isinstance(provider_call_id, str) else None),
        checkpoint_id=checkpoint_id if isinstance(checkpoint_id, str) else None,
        idempotency_key=f"{sequence:x}" * 64,
        payload_digest="f" * 64,
        payload=mapping,
        occurred_at=datetime(2026, 8, 27, 10, sequence, tzinfo=UTC),
    )


def _records(outcome: object, *, linked: bool = False) -> tuple[ContextEvidenceRecord, ...]:
    payloads: list[object] = [
        WindowOpenedV1(
            model_identity_digest="b" * 64,
            context_window_tokens=300_000,
            compaction_enabled=False,
        ),
        RequestPreparedV1(provider_call=CALL, measurement=MEASUREMENT),
        RequestDispatchedV1(provider_call_id=CALL.provider_call_id),
        outcome,
    ]
    if linked:
        payloads.append(
            CheckpointLinkedV1(
                provider_call_id=CALL.provider_call_id,
                checkpoint_id="checkpoint-result",
            ),
        )
    return tuple(_record(index, payload) for index, payload in enumerate(payloads, 1))


def _checkpoint_item(
    snapshot: ContextCheckpointProjectionSnapshot,
    *,
    messages: list[object],
) -> object:
    return SimpleNamespace(
        config={
            "configurable": {
                "thread_id": THREAD_ID,
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-result",
            },
        },
        checkpoint={
            "channel_values": {
                "messages": messages,
                CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: snapshot.to_safe_mapping(),
            },
        },
    )


class _Repository:
    def __init__(self, records: tuple[ContextEvidenceRecord, ...]) -> None:
        self.records = records

    async def page_provider_call_evidence(
        self,
        _scope: object,
        subject: ContextSubjectRef,
        provider_call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[ContextEvidenceRecord, ...]:
        assert subject == ContextSubjectRef.lead_thread(THREAD_ID)
        assert provider_call_id == CALL.provider_call_id
        assert limit == 1000
        if after_seq:
            return ()
        return self.records


class _ProjectionTransaction:
    calls: list[dict[str, object]] = []

    def __init__(self, _repository: object) -> None:
        pass

    async def append_and_project(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return object()


def test_provider_response_proof_ignores_graph_assigned_message_ids_only() -> None:
    without_id = AIMessage(content="answer")
    with_id = AIMessage(id="graph-assigned", content="answer")

    assert provider_response_digest((without_id,)) == provider_response_digest(
        (with_id,),
    )
    assert provider_response_digest((with_id,)) != provider_response_digest(
        (AIMessage(id="graph-assigned", content="different"),),
    )


def test_checkpoint_provider_response_authority_cannot_be_partial() -> None:
    response = AIMessage(id="provider-response", content="answer")
    safe = _snapshot(response).to_safe_mapping()
    del safe["provider_response_digest"]

    with pytest.raises(ValueError, match="must be complete"):
        ContextCheckpointProjectionSnapshot.from_safe_mapping(safe)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ProviderObservedV1(
            provider_call_id=CALL.provider_call_id,
            input_tokens=123,
        ),
        ProviderUsageUnreportedV1(provider_call_id=CALL.provider_call_id),
    ],
)
async def test_checkpoint_response_proof_repairs_provider_link_and_head_together(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    repository = _Repository(_records(outcome))
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )

    repaired = await saver._repair_context_provider_checkpoint(
        object(),  # type: ignore[arg-type]
        _checkpoint_item(
            _snapshot(response),
            messages=[HumanMessage(id="human", content="question"), response],
        ),  # type: ignore[arg-type]
        thread_id=THREAD_ID,
    )

    assert repaired == CALL.provider_call_id
    assert len(_ProjectionTransaction.calls) == 1
    call = _ProjectionTransaction.calls[0]
    payload = call["payloads"][0]
    assert isinstance(payload, CheckpointLinkedV1)
    assert payload.provider_call_id == CALL.provider_call_id
    assert payload.checkpoint_id == "checkpoint-result"
    assert call["source"].checkpoint_id == "checkpoint-result"
    assert call["source"].current_provider_call_id == CALL.provider_call_id
    assert call["origin_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_checkpoint_without_the_bound_provider_response_is_terminal_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    repository = _Repository(
        _records(
            ProviderObservedV1(
                provider_call_id=CALL.provider_call_id,
                input_tokens=123,
            ),
        ),
    )
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )

    with pytest.raises(ContextProviderCallAmbiguousError):
        await saver._repair_context_provider_checkpoint(
            object(),  # type: ignore[arg-type]
            _checkpoint_item(
                _snapshot(response),
                messages=[HumanMessage(id="human", content="question")],
            ),  # type: ignore[arg-type]
            thread_id=THREAD_ID,
        )

    assert _ProjectionTransaction.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ProviderObservedV1(
            provider_call_id=CALL.provider_call_id,
            input_tokens=123,
        ),
        ProviderUsageUnreportedV1(
            provider_call_id=CALL.provider_call_id,
        ),
    ],
)
async def test_unlinked_provider_snapshot_rejects_local_message_metadata_updates(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    locally_enriched = response.model_copy(
        update={
            "additional_kwargs": {
                "token_usage_attribution": {
                    "lane": "lead",
                },
            },
        },
    )
    repository = _Repository(_records(outcome))
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )

    with pytest.raises(ContextProviderCallAmbiguousError):
        await saver._repair_context_provider_checkpoint(
            object(),  # type: ignore[arg-type]
            _checkpoint_item(
                _snapshot(response),
                messages=[
                    HumanMessage(id="human", content="question"),
                    locally_enriched,
                ],
            ),  # type: ignore[arg-type]
            thread_id=THREAD_ID,
        )

    assert _ProjectionTransaction.calls == []


@pytest.mark.asyncio
async def test_already_linked_provider_snapshot_repair_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    repository = _Repository(
        _records(
            ProviderObservedV1(
                provider_call_id=CALL.provider_call_id,
                input_tokens=123,
            ),
            linked=True,
        ),
    )
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )

    repaired = await saver._repair_context_provider_checkpoint(
        object(),  # type: ignore[arg-type]
        _checkpoint_item(
            _snapshot(response),
            messages=[HumanMessage(id="human", content="question"), response],
        ),  # type: ignore[arg-type]
        thread_id=THREAD_ID,
    )

    assert repaired == CALL.provider_call_id
    assert _ProjectionTransaction.calls == []


@pytest.mark.asyncio
async def test_already_linked_provider_snapshot_allows_local_message_metadata_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    locally_enriched = response.model_copy(
        update={
            "additional_kwargs": {
                "token_usage_attribution": {
                    "lane": "lead",
                },
            },
        },
    )
    repository = _Repository(
        _records(
            ProviderObservedV1(
                provider_call_id=CALL.provider_call_id,
                input_tokens=123,
            ),
            linked=True,
        ),
    )
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )

    repaired = await saver._repair_context_provider_checkpoint(
        object(),  # type: ignore[arg-type]
        _checkpoint_item(
            _snapshot(response),
            messages=[
                HumanMessage(id="human", content="question"),
                locally_enriched,
            ],
        ),  # type: ignore[arg-type]
        thread_id=THREAD_ID,
    )

    assert repaired == CALL.provider_call_id
    assert _ProjectionTransaction.calls == []


@pytest.mark.asyncio
async def test_already_linked_provider_snapshot_still_requires_frozen_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(id="provider-response", content="answer")
    repository = _Repository(
        _records(
            ProviderObservedV1(
                provider_call_id=CALL.provider_call_id,
                input_tokens=123,
            ),
            linked=True,
        ),
    )
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    _ProjectionTransaction.calls.clear()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    drifted = _snapshot(response).model_copy(
        update={"origin_run_id": "different-run"},
    )

    with pytest.raises(ContextProviderCallAmbiguousError):
        await saver._repair_context_provider_checkpoint(
            object(),  # type: ignore[arg-type]
            _checkpoint_item(
                drifted,
                messages=[
                    HumanMessage(id="human", content="question"),
                    response,
                ],
            ),  # type: ignore[arg-type]
            thread_id=THREAD_ID,
        )

    assert _ProjectionTransaction.calls == []
