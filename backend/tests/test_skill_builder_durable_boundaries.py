"""Fail-closed composition contracts for the durable Skill Builder."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

import app.gateway.routers.project_skill_builder as router_module
from app.gateway.routers.project_skill_builder import (
    SkillDesignMessageTurnRequest,
    SkillDesignRunAdmissionResponse,
    SkillDesignTurnRequest,
    get_skill_design_service,
    submit_skill_design_turn,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.skill_builder_admission_contract import (
    SkillBuilderRunAdmission,
)
from app.shared_assets.skill_design_service import SkillDesignService


def _request_with_state(**state: object) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(**state)),
    )


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="b" * 32,
    )


@pytest.mark.parametrize(
    "available_dependency",
    ("system_model_catalog", "system_runtime_policy_service"),
)
def test_gateway_builder_service_fails_closed_without_worker_snapshot_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    available_dependency: str,
) -> None:
    session_factory = object()
    monkeypatch.setattr(
        router_module,
        "get_session_factory",
        lambda: session_factory,
    )
    request = _request_with_state(
        shared_asset_audit_sink=object(),
        **{available_dependency: object()},
    )

    with pytest.raises(HTTPException) as raised:
        get_skill_design_service(request)  # type: ignore[arg-type]

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "asset_storage_unavailable"
    assert not hasattr(request.app.state, "skill_design_service")


def test_gateway_builder_composes_run_admission_without_a_model_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = object()
    monkeypatch.setattr(
        router_module,
        "get_session_factory",
        lambda: session_factory,
    )
    request = _request_with_state(
        shared_asset_audit_sink=object(),
        system_model_catalog=object(),
        system_runtime_policy_service=object(),
    )

    service = get_skill_design_service(request)  # type: ignore[arg-type]

    assert isinstance(service, SkillDesignService)
    assert service._generator is None
    assert service._run_admission is not None
    assert request.app.state.skill_design_service is service


@pytest.mark.asyncio
async def test_gateway_replays_the_same_admission_as_http_202() -> None:
    context = _context()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    admission = SkillBuilderRunAdmission(
        run_id=str(run_id),
        status="pending",
        thread_id=str(thread_id),
    )

    class Service:
        async def submit_turn(self, *_args: object) -> SkillBuilderRunAdmission:
            return admission

    body = SkillDesignTurnRequest(
        input=SkillDesignMessageTurnRequest(
            kind="message",
            message="build a Skill",
        ),
        expected_revision=1,
        idempotency_key="same-turn",
    )
    first_response = Response()
    second_response = Response()

    first = await submit_skill_design_turn(
        session_id,
        body,
        context,
        Service(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        first_response,
    )
    replay = await submit_skill_design_turn(
        session_id,
        body,
        context,
        Service(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        second_response,
    )

    assert isinstance(first, SkillDesignRunAdmissionResponse)
    assert replay == first
    assert first_response.status_code == 202
    assert second_response.status_code == 202
    assert first.runId == run_id
    assert first.streamUrl.endswith(f"/private-work/threads/{thread_id}/runs/{run_id}/stream")
