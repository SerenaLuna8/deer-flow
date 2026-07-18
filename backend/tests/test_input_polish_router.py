from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.gateway.deps import project_input_polish_context
from app.gateway.routers import project_input_polish
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)


def _context(role: ProjectRole = ProjectRole.ADMIN) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="project-polish-test",
        )
    )


def _config(*, enabled: bool = True, max_chars: int = 4000):
    return SimpleNamespace(
        input_polish=SimpleNamespace(
            enabled=enabled,
            max_chars=max_chars,
            model_name="polish-model",
        )
    )


def test_clean_rewritten_text_removes_reasoning_but_keeps_literal_tag() -> None:
    assert project_input_polish._clean_rewritten_text("<think>reasoning</think>\n```text\nrewrite this\n```") == "rewrite this"
    literal = "Explain what the <think> tag does in reasoning models."
    assert project_input_polish._clean_rewritten_text(literal) == literal


@pytest.mark.parametrize(
    "field",
    (
        "account_id",
        "project_id",
        "owner_user_id",
        "capabilities",
        "asset_snapshot",
        "credential_grants",
    ),
)
def test_project_polish_request_rejects_client_authority(field: str) -> None:
    with pytest.raises(ValidationError):
        project_input_polish.ProjectInputPolishRequest.model_validate(
            {
                "text": "hello",
                "thread_id": str(uuid.uuid4()),
                field: "forged",
            }
        )


@pytest.mark.anyio
async def test_viewer_is_denied_before_service_or_model_call() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await project_input_polish_context(_context(ProjectRole.VIEWER))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "PRIVATE_WORK_FORBIDDEN"


@pytest.mark.anyio
async def test_polish_validates_server_agent_closure_before_auxiliary_model(
    monkeypatch,
) -> None:
    context = _context()
    service = project_input_polish.ProjectInputPolishService(lambda: None)  # type: ignore[arg-type]
    service.validate_authority = AsyncMock(return_value=object())  # type: ignore[method-assign]
    model_call = AsyncMock(return_value="Clear request")
    monkeypatch.setattr(project_input_polish, "run_oneshot_llm", model_call)
    thread_id = uuid.uuid4()

    result = await service.polish(
        context=context,
        body=project_input_polish.ProjectInputPolishRequest(
            text=" request ",
            locale="en-US",
            thread_id=thread_id,
        ),
        config=_config(),
    )

    service.validate_authority.assert_awaited_once_with(context, str(thread_id))
    model_call.assert_awaited_once()
    assert model_call.await_args.kwargs["run_name"] == "project_input_polish"
    assert result.rewritten_text == "Clear request"
    assert result.changed is True


@pytest.mark.anyio
async def test_authority_validation_resolves_thread_agent_and_credential_closure(
    monkeypatch,
) -> None:
    context = _context()
    agent_id = uuid.uuid4()
    resolved = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=agent_id,
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="",
            soul="project agent",
            model_ref="polish-model",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )

    class AsyncContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return False

    session = SimpleNamespace()
    session.begin = lambda: AsyncContext(session)
    service = project_input_polish.ProjectInputPolishService(
        lambda: AsyncContext(session),  # type: ignore[arg-type]
    )
    service._revalidator = SimpleNamespace(require=AsyncMock(return_value=context))
    service._resolver = SimpleNamespace(resolve_project_asset_snapshot_in_session=AsyncMock(return_value=resolved))
    closure = AsyncMock(return_value=([], [], {}))
    service._snapshots = SimpleNamespace(validate_agent_closure_in_session=closure)
    thread_repository = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                agent_asset_id=agent_id,
                agent_scope="project",
            )
        )
    )
    monkeypatch.setattr(
        project_input_polish,
        "PrivateThreadRepository",
        lambda _session: thread_repository,
    )

    result = await service.validate_authority(context, str(uuid.uuid4()))

    assert result is resolved
    closure.assert_awaited_once_with(session, context, resolved)
