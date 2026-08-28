from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.reliability.run_execution.stream_authority import (
    LeaseAuthorizedStreamBridge,
)
from deerflow.runtime.events.models import StreamLeaseProof
from deerflow.runtime.private_scope import PrivateResourceScope


class _Boundary:
    def __init__(self) -> None:
        self.authorization_revoked = False
        self.lease_lost = False

    def stream_lease_proof(self) -> StreamLeaseProof:
        return StreamLeaseProof(
            job_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            lease_token="lease-token",
        )

    def record_stream_authorization_revoked(self) -> None:
        self.authorization_revoked = True

    def record_stream_lease_lost(self) -> None:
        self.lease_lost = True

    def request_local_cancel(self) -> None:
        raise AssertionError("candidate persistence must not synthesize cancellation")


@pytest.mark.asyncio
async def test_durable_response_persists_candidate_without_closing_stream() -> None:
    underlying = type(
        "CandidateBridge",
        (),
        {
            "publish_terminal_candidate": AsyncMock(),
            "publish_terminal": AsyncMock(),
        },
    )()
    boundary = _Boundary()
    scope = PrivateResourceScope(
        project_id="11111111-1111-4111-8111-111111111111",
        owner_user_id="owner-1",
        membership_version=1,
    )
    bridge = LeaseAuthorizedStreamBridge(
        underlying,
        boundary,  # type: ignore[arg-type]
        scope=scope,
        thread_id="thread-1",
        terminal_status=lambda: "error",
        terminal_error_code=lambda: "MODEL_OUTPUT_LIMIT",
        terminal_authority=lambda: "durable_response",
    )

    await bridge.publish_end("run-1")

    underlying.publish_terminal_candidate.assert_awaited_once_with(
        scope,
        "thread-1",
        "run-1",
        status="error",
        error_code="MODEL_OUTPUT_LIMIT",
        lease=boundary.stream_lease_proof(),
    )
    underlying.publish_terminal.assert_not_awaited()
