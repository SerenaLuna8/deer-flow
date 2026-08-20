from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkAgentArchived
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AgentArchived
from app.shared_assets.models import AssetKind, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.shared_assets import AgentRow


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Session:
    def __init__(self, asset: AgentRow) -> None:
        self.asset = asset
        self.execute_count = 0

    async def execute(self, _statement: object) -> _ScalarResult:
        self.execute_count += 1
        return _ScalarResult(self.asset)


def _context() -> ProjectContext:
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="agent-archive-run-contract",
    )


@pytest.mark.asyncio
async def test_new_resolution_distinguishes_archived_project_agent() -> None:
    context = _context()
    asset = AgentRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        slug="archived-agent",
        display_name="Archived Agent",
        status="archived",
        revision=2,
        current_version_id=uuid.uuid4(),
        created_by_user_id=str(context.user_id),
    )
    session = _Session(asset)

    with pytest.raises(AgentArchived) as exc_info:
        await ProjectAssetResolver(lambda: None)._resolve_record(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            SimpleNamespace(),
            context,
            AssetSelection(AssetKind.AGENT, asset.id),
        )

    assert exc_info.value.request_id == context.request_id
    assert session.execute_count == 1


def test_agent_archived_error_has_stable_non_disclosing_http_contract() -> None:
    error = PrivateWorkAgentArchived("request-agent-archived")

    response = private_work_http_exception(error)

    assert response.status_code == 409
    assert response.detail == {
        "code": "PRIVATE_WORK_AGENT_ARCHIVED",
        "message": PrivateWorkAgentArchived.public_message,
        "request_id": "request-agent-archived",
    }
    assert "request-agent-archived" not in PrivateWorkAgentArchived.public_message
