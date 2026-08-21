from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.gateway.routers.project_skill_builder import (
    SkillDesignSessionSummaryResponse,
    _activity_response,
)
from app.shared_assets.skill_design_activity import (
    SkillDesignActivity,
    SkillDesignActivityKind,
)
from app.shared_assets.skill_design_service import (
    SkillDesignSessionSummary,
    SkillDesignStatus,
)


def test_skill_builder_session_summary_exposes_revision_for_safe_deletion() -> None:
    summary = SkillDesignSessionSummary(
        id=uuid.UUID("00000000-0000-4000-8000-000000000010"),
        slug="transmission-channel-rquery",
        display_name="transmission-channel-rquery",
        status=SkillDesignStatus.INTERVIEWING,
        revision=4,
        updated_at=datetime(2026, 8, 13, tzinfo=UTC),
    )

    response = SkillDesignSessionSummaryResponse.model_validate(
        summary,
        from_attributes=True,
    )

    assert response.revision == 4
    assert response.session_kind == "create"


def test_skill_builder_activity_response_rejects_unclosed_tool_payload() -> None:
    activity = SkillDesignActivity(
        seq=1,
        operation_id=uuid.UUID("00000000-0000-4000-8000-000000000011"),
        run_id="00000000-0000-4000-8000-000000000012",
        kind=SkillDesignActivityKind.TOOL_STARTED,
        attempt=1,
        payload={
            "tool_call_id": "call-1",
            "tool_name": "read_candidate_file",
            "args": {"secret": "must-not-enter-response"},
        },
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
    )

    with pytest.raises(ValidationError):
        _activity_response(activity)
