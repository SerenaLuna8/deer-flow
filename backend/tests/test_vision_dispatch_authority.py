"""Governance tests for the application-owned Vision dispatch authority."""

from __future__ import annotations

import pytest

from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from deerflow.runtime.journal import RunJournal
from deerflow.vision.contracts import VisionUsageReceipt
from deerflow.vision.dispatch import (
    MAX_VISION_CALLS_PER_RUN,
    MAX_VISION_NORMALIZED_BYTES_PER_RUN,
    MAX_VISION_NORMALIZED_PIXELS_PER_RUN,
    VisionDispatchAttempt,
    VisionDispatchDenied,
)

VISION_MODEL_REF = "00000000-0000-4000-8000-000000000306"


class _Boundary:
    def __init__(self) -> None:
        self.before_calls = 0
        self.after_calls = 0
        self.normalized_bytes = 0
        self.normalized_pixels = 0

    async def before_vision_dispatch(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> None:
        if self.before_calls + 1 > MAX_VISION_CALLS_PER_RUN or self.normalized_bytes + normalized_bytes > MAX_VISION_NORMALIZED_BYTES_PER_RUN or self.normalized_pixels + normalized_pixels > MAX_VISION_NORMALIZED_PIXELS_PER_RUN:
            raise VisionDispatchDenied("VISION_BUDGET_EXHAUSTED")
        self.before_calls += 1
        self.normalized_bytes += normalized_bytes
        self.normalized_pixels += normalized_pixels

    async def after_vision_dispatch(self) -> None:
        self.after_calls += 1


def _authority(boundary: _Boundary) -> PrivateRunVisionDispatchAuthority:
    return PrivateRunVisionDispatchAuthority(boundary=boundary)


@pytest.mark.asyncio
async def test_dispatch_applies_frozen_run_boundary_before_and_after_http() -> None:
    boundary = _Boundary()
    authority = _authority(boundary)

    attempt = await authority.before_attempt(
        normalized_bytes=100,
        normalized_pixels=200,
    )
    assert isinstance(attempt, VisionDispatchAttempt)
    await authority.after_attempt(
        attempt=attempt,
        usage_receipt=VisionUsageReceipt(
            call_count=1,
            request_dispatched=True,
            input_tokens=3,
            output_tokens=2,
            usage_unknown=False,
        ),
        error_code=None,
    )

    assert boundary.before_calls == 1
    assert boundary.after_calls == 1


@pytest.mark.asyncio
async def test_dispatch_uses_durable_boundary_for_cumulative_limits() -> None:
    boundary = _Boundary()
    authority = _authority(boundary)

    for _ in range(MAX_VISION_CALLS_PER_RUN):
        await authority.before_attempt(
            normalized_bytes=1,
            normalized_pixels=1,
        )
    with pytest.raises(VisionDispatchDenied) as caught:
        await authority.before_attempt(
            normalized_bytes=1,
            normalized_pixels=1,
        )
    assert caught.value.code == "VISION_BUDGET_EXHAUSTED"
    assert boundary.before_calls == MAX_VISION_CALLS_PER_RUN

    # Reconstructing the process-local authority must not reset the boundary.
    reconstructed = _authority(boundary)
    with pytest.raises(VisionDispatchDenied) as caught:
        await reconstructed.before_attempt(
            normalized_bytes=1,
            normalized_pixels=1,
        )
    assert caught.value.code == "VISION_BUDGET_EXHAUSTED"

    byte_boundary = _Boundary()
    byte_authority = _authority(byte_boundary)
    with pytest.raises(VisionDispatchDenied) as caught:
        await byte_authority.before_attempt(
            normalized_bytes=MAX_VISION_NORMALIZED_BYTES_PER_RUN + 1,
            normalized_pixels=1,
        )
    assert caught.value.code == "VISION_BUDGET_EXHAUSTED"
    assert byte_boundary.before_calls == 0


@pytest.mark.asyncio
async def test_dispatch_attempt_receipt_is_opaque_and_single_use() -> None:
    boundary = _Boundary()
    authority = _authority(boundary)
    attempt = await authority.before_attempt(
        normalized_bytes=100,
        normalized_pixels=200,
    )
    receipt = VisionUsageReceipt(
        call_count=0,
        request_dispatched=False,
        usage_unknown=False,
    )

    await authority.after_attempt(
        attempt=attempt,
        usage_receipt=receipt,
        error_code="VISION_UNAVAILABLE",
    )
    with pytest.raises(VisionDispatchDenied) as caught:
        await authority.after_attempt(
            attempt=attempt,
            usage_receipt=receipt,
            error_code="VISION_UNAVAILABLE",
        )

    assert caught.value.code == "VISION_CONFIGURATION_ERROR"
    assert boundary.after_calls == 1


def test_vision_usage_receipt_counts_auxiliary_call_without_content() -> None:
    journal = RunJournal(
        "run-1",
        "thread-1",
        object(),
    )

    journal.record_vision_usage(
        source_id="vision:run-1:call-1",
        model_name=VISION_MODEL_REF,
        call_count=1,
        input_tokens=31,
        output_tokens=12,
        usage_unknown=False,
        request_dispatched=True,
    )
    # Exact duplicate receipts are ignored during retry/reconciliation.
    journal.record_vision_usage(
        source_id="vision:run-1:call-1",
        model_name=VISION_MODEL_REF,
        call_count=1,
        input_tokens=31,
        output_tokens=12,
        usage_unknown=False,
        request_dispatched=True,
    )

    completion = journal.get_completion_data()
    assert completion["llm_call_count"] == 1
    assert completion["total_input_tokens"] == 31
    assert completion["total_output_tokens"] == 12
    assert completion["middleware_tokens"] == 43
    assert completion["token_usage_by_model"] == {
        VISION_MODEL_REF: {
            "input_tokens": 31,
            "output_tokens": 12,
            "total_tokens": 43,
        }
    }
