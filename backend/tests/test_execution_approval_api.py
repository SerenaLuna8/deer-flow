from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router
from app.private_work.context import strip_private_client_fields
from app.private_work.execution_approval import ExecutionApprovalProjection


def _common_response_projection(approval_id: uuid.UUID) -> dict[str, object]:
    return {
        "approval_id": str(approval_id),
        "source_run_id": "11111111-1111-4111-8111-111111111111",
        "source_tool_call_id": "call-1",
        "version": "1",
        "execution_domain": {
            "label": "Local Provider host",
            "effective_user_label": "local OS user",
        },
        "command_preview": "python /mnt/user-data/workspace/count.py",
        "cwd_preview": "/mnt/user-data/workspace",
        "timeout_seconds": 60,
        "source_agent": {
            "kind": "lead",
            "label": "Project Assistant",
            "path": ["lead"],
        },
        "risk_level": "host_execution",
        "warning_code": "LOCAL_PROCESS_RUNS_ON_HOST",
        "can_decide": False,
        "continuation_run": None,
    }


class _ExecutionApprovalService:
    def __init__(self, approval_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _projection(self, *, status: str = "pending") -> ExecutionApprovalProjection:
        now = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
        approval = {
            **_common_response_projection(self.approval_id),
            "status": status,
            "can_decide": status == "pending",
            "decision_expires_at": "2026-08-14T08:05:00+00:00",
            "remaining_ttl_seconds": 300,
        }
        return ExecutionApprovalProjection(1, now, approval)

    async def active(self, *args: object, **kwargs: object) -> ExecutionApprovalProjection:
        self.calls.append(("active", args, kwargs))
        return self._projection()

    async def get(self, *args: object, **kwargs: object) -> ExecutionApprovalProjection:
        self.calls.append(("get", args, kwargs))
        return self._projection()

    async def decide(self, *args: object, **kwargs: object) -> ExecutionApprovalProjection:
        self.calls.append(("decide", args, kwargs))
        return self._projection()


@pytest.mark.asyncio
async def test_execution_approval_routes_are_owner_scoped_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="approval-api")
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    approval_id = uuid.uuid4()
    service = _ExecutionApprovalService(approval_id)
    monkeypatch.setattr(
        private_work_router,
        "_execution_approval_service",
        lambda _request, _request_id: service,
    )
    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    source_run_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    base = f"/api/projects/{project_id}/private-work/threads/{thread_id}/execution-approvals"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        active = await client.get(f"{base}/active")
        by_id = await client.get(f"{base}/{approval_id}")
        decision = await client.post(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{source_run_id}/execution-approvals/{approval_id}/decision",
            json={
                "schema_version": 1,
                "decision": "allow_once",
                "expected_version": "1",
                "idempotency_key": "22222222-2222-4222-8222-222222222222",
            },
        )
        invalid = await client.post(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{source_run_id}/execution-approvals/{approval_id}/decision",
            json={
                "schema_version": 1,
                "decision": "allow_once",
                "expected_version": 1,
                "idempotency_key": "22222222-2222-4222-8222-222222222222",
                "command": "forged",
            },
        )

    assert active.status_code == 200
    assert by_id.status_code == 200
    assert decision.status_code == 200
    assert invalid.status_code == 422
    assert active.json()["approval"]["command_preview"].startswith("python")
    assert [call[0] for call in service.calls] == ["active", "get", "decide"]
    decide_kwargs = service.calls[-1][2]
    assert decide_kwargs == {
        "thread_id": str(thread_id),
        "source_run_id": str(source_run_id),
        "approval_id": approval_id,
        "decision": "allow_once",
        "expected_version": 1,
        "idempotency_key": uuid.UUID(
            "22222222-2222-4222-8222-222222222222",
        ),
    }


@pytest.mark.parametrize(
    ("status", "specific"),
    [
        (
            "pending",
            {
                "can_decide": True,
                "decision_expires_at": "2026-08-14T08:05:00+00:00",
                "remaining_ttl_seconds": 300,
            },
        ),
        (
            "approved",
            {
                "decision_at": "2026-08-14T08:01:00+00:00",
                "claim_expires_at": "2026-08-14T08:02:00+00:00",
            },
        ),
        (
            "claimed",
            {
                "continuation_run": {
                    "run_id": "22222222-2222-4222-8222-222222222222",
                    "status": "running",
                },
                "claimed_at": "2026-08-14T08:01:05+00:00",
            },
        ),
        (
            "finished",
            {
                "finished_at": "2026-08-14T08:01:10+00:00",
                "exit_code": 0,
                "result_summary_code": "PROCESS_EXITED",
            },
        ),
        (
            "launch_failed",
            {
                "finished_at": "2026-08-14T08:01:10+00:00",
                "reason_code": "PROCESS_NOT_CREATED",
            },
        ),
        (
            "unknown",
            {
                "warning_code": "HOST_EXECUTION_STATE_UNKNOWN",
                "finished_at": "2026-08-14T08:01:10+00:00",
            },
        ),
        (
            "denied",
            {
                "decision_at": "2026-08-14T08:01:00+00:00",
                "denial_delivery_status": "not_required",
            },
        ),
        (
            "expired",
            {
                "finished_at": "2026-08-14T08:05:00+00:00",
                "reason_code": "DECISION_TTL_EXPIRED",
            },
        ),
        (
            "cancelled",
            {
                "finished_at": "2026-08-14T08:05:00+00:00",
                "reason_code": "POLICY_INVALIDATED",
            },
        ),
    ],
)
def test_execution_approval_response_union_is_strict(
    status: str,
    specific: dict[str, object],
) -> None:
    approval_id = uuid.uuid4()
    payload = {
        **_common_response_projection(approval_id),
        "status": status,
        **specific,
    }

    parsed = private_work_router.ExecutionApprovalEnvelopeResponse.model_validate(
        {
            "schema_version": 1,
            "server_time": "2026-08-14T08:00:00+00:00",
            "approval": payload,
        },
    )

    assert parsed.approval is not None
    assert parsed.approval.status == status

    with pytest.raises(ValidationError):
        private_work_router.ExecutionApprovalEnvelopeResponse.model_validate(
            {
                "schema_version": 1,
                "server_time": "2026-08-14T08:00:00+00:00",
                "approval": {**payload, "untrusted_authority": "forged"},
            },
        )


def test_finished_execution_approval_requires_a_durable_result() -> None:
    with pytest.raises(ValidationError):
        private_work_router.ExecutionApprovalEnvelopeResponse.model_validate(
            {
                "schema_version": 1,
                "server_time": "2026-08-14T08:00:00+00:00",
                "approval": {
                    **_common_response_projection(uuid.uuid4()),
                    "status": "finished",
                    "finished_at": "2026-08-14T08:01:10+00:00",
                    "result_summary_code": "PROCESS_EXITED",
                },
            },
        )


def test_internal_staged_approval_has_a_pollable_pending_api_shape() -> None:
    payload = {
        **_common_response_projection(uuid.uuid4()),
        "status": "pending",
        "can_decide": False,
        "decision_expires_at": "2026-08-14T08:05:00+00:00",
        "remaining_ttl_seconds": 300,
    }

    parsed = private_work_router.ExecutionApprovalEnvelopeResponse.model_validate(
        {
            "schema_version": 1,
            "server_time": "2026-08-14T08:00:00+00:00",
            "approval": payload,
        },
    )

    assert parsed.approval is not None
    assert parsed.approval.status == "pending"
    assert parsed.approval.can_decide is False


def test_client_cannot_persist_host_execution_authority_fields() -> None:
    assert strip_private_client_fields(
        {
            "host_execution_approval_id": str(uuid.uuid4()),
            "host_execution_decision_digest": "a" * 64,
            "input": {"messages": []},
        },
    ) == {"input": {"messages": []}}
