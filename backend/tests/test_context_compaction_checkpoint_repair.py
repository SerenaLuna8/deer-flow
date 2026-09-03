from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.private_work import checkpoint_receipt_repair as repair_module
from app.private_work.checkpointer import _ScopedCheckpointSaver
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
)
from deerflow.runtime.context_evidence import (
    CompactionCommittedV1,
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextCompactionCheckpointReceipt,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionPhase,
    TokenEstimate,
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
            request_id="compaction-repair",
        )
    )


def _projection_snapshot(
    generation: ContextWindowGeneration,
    *,
    tokens: int,
) -> ContextCheckpointProjectionSnapshot:
    return ContextCheckpointProjectionSnapshot(
        generation=generation,
        model=ContextModelProjection(
            identity_digest="a" * 64,
            context_window_tokens=300_000,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint="b" * 64,
            adapter_revision="repair-test-v1",
            contributions=(
                ContextContribution(
                    contribution_id="c" * 64,
                    source_identity_digest="d" * 64,
                    lane=ContextLane.SUMMARIZED_CONVERSATION,
                    model_visible_bytes=tokens * 4,
                    token_estimate=TokenEstimate.exact(tokens),
                ),
            ),
        ),
        compaction=CompactionProjection(enabled=False, reached=False),
        estimator=ContextCheckpointEstimator(
            error_allowance_ratio=0.2,
            provider_fixed_overhead_tokens=10,
            provider_per_message_overhead_tokens=2,
            provider_per_tool_overhead_tokens=3,
            fixed_message_count=1,
            tool_count=2,
        ),
    )


def _receipt() -> ContextCompactionCheckpointReceipt:
    source_generation = ContextWindowGeneration(
        generation_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    )
    result_generation = ContextWindowGeneration(
        generation_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
    )
    return ContextCompactionCheckpointReceipt(
        receipt_id="e" * 64,
        source_checkpoint_id="checkpoint-source",
        source_generation=source_generation,
        result_generation=result_generation,
        source_tokens=1000,
        result_tokens=300,
        summary_digest="f" * 64,
        projection_snapshot=_projection_snapshot(
            result_generation,
            tokens=300,
        ),
        phase=ProjectionPhase.ACTIVE,
        origin_run_id="run-1",
    )


def _checkpoint_item(receipt: ContextCompactionCheckpointReceipt) -> object:
    return SimpleNamespace(
        config={
            "configurable": {
                "thread_id": THREAD_ID,
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-result",
            }
        },
        checkpoint={
            "channel_values": {
                CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: (receipt.projection_snapshot.to_safe_mapping()),
                CONTEXT_COMPACTION_RECEIPT_STATE_KEY: receipt.to_safe_mapping(),
            }
        },
    )


class _Repository:
    async def read_head(self, *_args: object, **_kwargs: object) -> None:
        return None


class _ProjectionTransaction:
    calls: list[dict[str, object]] = []

    def __init__(self, _repository: object) -> None:
        pass

    async def append_and_project(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_checkpoint_compaction_receipt_repairs_evidence_and_head_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt()
    saver = object.__new__(_ScopedCheckpointSaver)
    saver._context = _context()
    monkeypatch.setattr(
        repair_module,
        "ContextEvidenceRepository",
        lambda _session: _Repository(),
    )
    monkeypatch.setattr(
        repair_module,
        "ContextProjectionTransaction",
        _ProjectionTransaction,
    )
    _ProjectionTransaction.calls.clear()

    await saver._repair_context_compaction_receipt(
        object(),  # type: ignore[arg-type]
        _checkpoint_item(receipt),  # type: ignore[arg-type]
        thread_id=THREAD_ID,
    )

    assert len(_ProjectionTransaction.calls) == 1
    call = _ProjectionTransaction.calls[0]
    payload = call["payloads"][0]
    assert isinstance(payload, CompactionCommittedV1)
    assert payload.source_checkpoint_id == "checkpoint-source"
    assert payload.result_checkpoint_id == "checkpoint-result"
    assert call["origin_run_id"] == "run-1"
    assert call["active_run_id"] == "run-1"
    assert call["source"].generation == receipt.result_generation
    assert call["source"].checkpoint_id == "checkpoint-result"
