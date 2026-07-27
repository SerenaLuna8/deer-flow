from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.gateway.routers import project_agent_builder
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_service import (
    AgentDesignBlueprint,
    AgentDesignClarificationOption,
    AgentDesignClarificationRequest,
    AgentDesignCommitResult,
    AgentDesignMessage,
    AgentDesignProgressItem,
    AgentDesignProgressStatus,
    AgentDesignSessionSummary,
    AgentDesignSessionView,
    AgentDesignStatus,
)
from app.shared_assets.agent_service import AgentAssetView, AgentVersionView
from app.shared_assets.errors import AssetConflict
from app.shared_assets.models import AssetScope, WorkflowStatus

PROJECT_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()
NOW = datetime.now(UTC)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-agent-builder",
    )


def _blueprint() -> AgentDesignBlueprint:
    return AgentDesignBlueprint(
        description="测试 Agent",
        model_ref="deepseek-v4",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="# AGENTS",
        soul="# SOUL",
        identity="# IDENTITY",
        user_context="# USER",
    )


def _session(
    *,
    status: AgentDesignStatus = AgentDesignStatus.PROPOSAL_READY,
    clarification: AgentDesignClarificationRequest | None = None,
) -> AgentDesignSessionView:
    return AgentDesignSessionView(
        id=SESSION_ID,
        project_id=PROJECT_ID,
        owner_user_id=str(uuid.uuid4()),
        thread_id=uuid.uuid4(),
        slug="code-test",
        display_name="Code Test",
        status=status,
        revision=3,
        blueprint=_blueprint(),
        blueprint_checksum="a" * 64,
        messages=(
            AgentDesignMessage(
                id="message-1",
                role="assistant",
                content="请描述你的 Agent",
                created_at=NOW,
            ),
        ),
        active_clarification=clarification,
        progress=(
            AgentDesignProgressItem(
                id="agents_instructions",
                label="AGENTS.md",
                status=AgentDesignProgressStatus.COMPLETED,
            ),
        ),
        error_code=None,
        error_message=None,
        created_agent_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _client(service: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(project_agent_builder.router)
    app.dependency_overrides[project_agent_builder.project_asset_context] = _context
    app.dependency_overrides[project_agent_builder.get_agent_design_service] = lambda: service
    return TestClient(app)


def test_agent_builder_create_list_and_get_use_strict_envelopes() -> None:
    service = AsyncMock()
    service.create.return_value = _session(status=AgentDesignStatus.INTERVIEWING)
    service.list_incomplete.return_value = (
        AgentDesignSessionSummary(
            id=SESSION_ID,
            slug="code-test",
            display_name="Code Test",
            status=AgentDesignStatus.INTERVIEWING,
            updated_at=NOW,
        ),
    )
    service.get.return_value = _session()
    client = _client(service)

    created = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions",
        json={
            "slug": "code-test",
            "display_name": "Code Test",
            "idempotency_key": "create-1",
        },
    )
    listed = client.get(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions",
    )
    fetched = client.get(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}",
    )

    assert created.status_code == 201
    assert set(created.json()) == {"data", "request_id"}
    assert created.json()["request_id"] == "req-agent-builder"
    assert created.json()["data"]["thread_id"]
    assert "created_agent_version_id" not in created.json()["data"]
    assert listed.status_code == 200
    assert listed.json() == {
        "data": [
            {
                "id": str(SESSION_ID),
                "slug": "code-test",
                "display_name": "Code Test",
                "status": "interviewing",
                "updated_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        ],
        "request_id": "req-agent-builder",
    }
    assert fetched.status_code == 200
    service.create.assert_awaited_once()
    service.list_incomplete.assert_awaited_once()
    service.get.assert_awaited_once_with(
        service.get.await_args.args[0],
        SESSION_ID,
    )


def test_agent_builder_turn_maps_frontend_union_to_domain_commands() -> None:
    clarification = AgentDesignClarificationRequest(
        version=1,
        kind="human_input_request",
        source="agent_builder",
        request_id="question-1",
        clarification_type="agent_design",
        title="需要你的帮助",
        question="它的主要职责是什么？",
        context="用于确定工作边界",
        input_mode="choice_with_other",
        options=(
            AgentDesignClarificationOption(
                id="option-1",
                label="测试执行",
                value="测试执行",
            ),
        ),
    )
    service = AsyncMock()
    service.submit_turn.return_value = _session(
        status=AgentDesignStatus.AWAITING_CLARIFICATION,
        clarification=clarification,
    )
    response = _client(service).post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}/turns",
        json={
            "input": {
                "kind": "clarification",
                "response": {
                    "version": 1,
                    "kind": "human_input_response",
                    "source": "agent_builder",
                    "request_id": "question-1",
                    "response_kind": "option",
                    "option_id": "option-1",
                    "value": "测试执行",
                },
            },
            "expected_revision": 2,
            "idempotency_key": "turn-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["active_clarification"]["input_mode"] == "choice_with_other"
    assert payload["data"]["active_clarification"]["options"][0]["id"] == "option-1"
    assert "tool_call_id" not in payload["data"]["active_clarification"]
    command = service.submit_turn.await_args.args[2]
    assert command.input.kind == "clarification"
    assert command.input.response.option_id == "option-1"
    assert command.expected_revision == 2


def test_agent_builder_turn_maps_message_and_blueprint_updates() -> None:
    service = AsyncMock()
    service.submit_turn.return_value = _session()
    client = _client(service)

    message = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}/turns",
        json={
            "input": {
                "kind": "message",
                "message": "请让它更直接。",
            },
            "expected_revision": 3,
            "idempotency_key": "turn-message",
        },
    )
    blueprint = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}/turns",
        json={
            "input": {
                "kind": "blueprint_update",
                "blueprint": {
                    "description": "测试 Agent",
                    "model_ref": "deepseek-v4",
                    "tool_groups": ["web"],
                    "skill_version_ids": [],
                    "mcp_version_ids": [],
                    "agents_instructions": "# AGENTS",
                    "soul": "# SOUL",
                    "identity": "# IDENTITY",
                    "user_context": "# USER",
                },
            },
            "expected_revision": 3,
            "idempotency_key": "turn-blueprint",
        },
    )

    assert message.status_code == 200
    assert blueprint.status_code == 200
    message_command = service.submit_turn.await_args_list[0].args[2]
    blueprint_command = service.submit_turn.await_args_list[1].args[2]
    assert message_command.input.message == "请让它更直接。"
    assert blueprint_command.input.blueprint.tool_groups == ("web",)
    assert blueprint_command.input.blueprint.agents_instructions == "# AGENTS"


def test_agent_builder_commit_and_cancel_match_frontend_contract() -> None:
    service = AsyncMock()
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    completed = dataclasses.replace(
        _session(status=AgentDesignStatus.COMPLETED),
        created_agent_id=agent_id,
    )
    agent = AgentAssetView(
        id=agent_id,
        scope=AssetScope.PROJECT,
        project_id=PROJECT_ID,
        slug="code-test",
        display_name="Code Test",
        status="suspended",
        current_published_version_id=version_id,
        version=2,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
        updated_at=NOW,
    )
    version = AgentVersionView(
        id=version_id,
        agent_id=agent_id,
        version_number=1,
        workflow_status=WorkflowStatus.PUBLISHED,
        description="测试 Agent",
        agents_instructions="# AGENTS",
        soul="# SOUL",
        identity="# IDENTITY",
        user_context="# USER",
        model_ref="deepseek-v4",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        supersedes_version_id=None,
        payload_schema_version=2,
        payload_checksum="b" * 64,
        created_by_user_id=str(uuid.uuid4()),
        created_at=NOW,
    )
    service.commit.return_value = AgentDesignCommitResult(
        session=completed,
        agent=agent,
        version=version,
    )
    service.cancel.return_value = _session(status=AgentDesignStatus.CANCELLED)
    client = _client(service)

    committed = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}/commit",
        json={
            "expected_revision": 3,
            "expected_blueprint_checksum": "a" * 64,
            "idempotency_key": "commit-1",
        },
    )
    cancelled = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}/cancel",
        json={
            "expected_revision": 3,
            "idempotency_key": "cancel-1",
        },
    )

    assert committed.status_code == 200
    assert set(committed.json()["data"]) == {"session", "agent"}
    assert committed.json()["data"]["agent"]["status"] == "suspended"
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_agent_builder_maps_domain_and_request_validation_errors() -> None:
    service = AsyncMock()
    service.get.side_effect = AssetConflict("req-agent-builder")
    client = _client(service)

    conflict = client.get(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions/{SESSION_ID}",
    )
    invalid = client.post(
        f"/api/projects/{PROJECT_ID}/agent-builder/sessions",
        json={
            "slug": "code-test",
            "display_name": "Code Test",
            "idempotency_key": "create-1",
            "unexpected": True,
        },
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "asset_conflict",
        "message": "Asset state conflict",
        "request_id": "req-agent-builder",
    }
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "asset_validation_failed"


def test_agent_builder_service_dependency_uses_app_owned_generation_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = object()
    generation = object()
    audit = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent_design_generation_service=generation,
                shared_asset_audit_sink=audit,
            )
        )
    )
    monkeypatch.setattr(
        project_agent_builder,
        "get_session_factory",
        lambda: session_factory,
    )

    service = project_agent_builder.get_agent_design_service(request)

    assert service._session_factory is session_factory
    assert service._generator is generation
    assert service._agent_service._governance_sink is audit
    assert request.app.state.agent_design_service is service


def test_agent_builder_service_dependency_fails_closed_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    monkeypatch.setattr(
        project_agent_builder,
        "get_session_factory",
        lambda: object(),
    )

    with pytest.raises(HTTPException) as captured:
        project_agent_builder.get_agent_design_service(request)

    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "asset_storage_unavailable"
