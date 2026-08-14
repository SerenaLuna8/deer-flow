from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.gateway.routers.project_skill_builder import (
    SkillDesignSessionSummaryResponse,
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
