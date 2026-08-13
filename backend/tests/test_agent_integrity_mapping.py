from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetConflict, AssetStorageUnavailable


def _actor() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="agent-integrity-mapping",
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self


class _ConstraintViolation(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constraint_name",
    [
        "fk_project_channel_group_bindings_agent",
        "fk_project_channel_group_binding_challenges_agent",
    ],
)
async def test_known_channel_agent_foreign_keys_map_to_conflict(
    constraint_name: str,
) -> None:
    async def violate(_repository):
        raise IntegrityError(
            "private SQL",
            {"secret": "must-not-leak"},
            _ConstraintViolation(constraint_name),
        )

    with pytest.raises(AssetConflict) as exc_info:
        await AgentService(_Session)._execute(_actor(), violate)

    assert exc_info.value.request_id == "agent-integrity-mapping"
    assert "private SQL" not in str(exc_info.value)
    assert "must-not-leak" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_unknown_integrity_constraint_remains_storage_unavailable() -> None:
    async def violate(_repository):
        raise IntegrityError(
            "private SQL",
            {"secret": "must-not-leak"},
            _ConstraintViolation("ck_unexpected_agent_invariant"),
        )

    with pytest.raises(AssetStorageUnavailable) as exc_info:
        await AgentService(_Session)._execute(_actor(), violate)

    assert exc_info.value.request_id == "agent-integrity-mapping"
    assert "private SQL" not in str(exc_info.value)
    assert "must-not-leak" not in str(exc_info.value)
