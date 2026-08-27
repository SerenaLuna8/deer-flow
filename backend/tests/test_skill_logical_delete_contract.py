from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.audit.models import AssetAuditMetadata, PurgeAuditMetadata
from app.gateway.routers.project_assets import SkillDeleteResponse, project_router
from app.shared_assets.skill_deletion import SkillDeleteResult
from deerflow.persistence.shared_assets.skill_model import SkillRow
from deerflow.persistence.shared_assets.skill_secret_model import (
    ProjectSkillSecretTombstoneRow,
)


def _project_unique_predicate(index_name: str) -> str:
    index = next(candidate for candidate in SkillRow.__table__.indexes if candidate.name == index_name)
    predicate = index.dialect_options["postgresql"]["where"]
    return str(predicate.compile(dialect=postgresql.dialect()))


def test_archived_project_skills_leave_the_live_name_namespace() -> None:
    assert _project_unique_predicate("uq_skills_project_slug") == ("scope = 'project' AND status != 'archived'")
    assert _project_unique_predicate("uq_skills_project_display_name") == ("scope = 'project' AND status != 'archived'")


def test_skill_delete_does_not_tombstone_retained_secret_ciphertext() -> None:
    reason = next(constraint for constraint in ProjectSkillSecretTombstoneRow.__table__.constraints if constraint.name == "ck_project_skill_secret_tombstones_reason")

    assert str(reason.sqltext) == "reason IN ('replace', 'clear')"


def test_skill_delete_result_exposes_only_affected_agent_count() -> None:
    deletion = SkillDeleteResult(affected_agent_count=2)

    assert deletion.affected_agent_count == 2


def test_skill_delete_http_contract_returns_affected_agent_count() -> None:
    route = next(candidate for candidate in project_router.routes if candidate.name == "delete_project_skill")

    assert route.status_code == 200
    assert route.response_model is SkillDeleteResponse
    assert set(SkillDeleteResponse.model_fields) == {
        "skill_id",
        "affected_agent_count",
        "request_id",
    }


@pytest.mark.parametrize(
    "metadata",
    (
        {"asset_kind": "skill", "operation": "skill.delete"},
        {
            "asset_kind": "skill",
            "operation": "skill.enable",
            "affected_agent_count": 1,
        },
    ),
)
def test_skill_delete_audit_requires_affected_agent_count_iff_deleted(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AssetAuditMetadata.model_validate(metadata)


def test_archived_skill_has_no_physical_purge_audit_resource_kind() -> None:
    with pytest.raises(ValidationError):
        PurgeAuditMetadata(
            resource_kind="archived_skill",  # type: ignore[arg-type]
            purged_count=2,
        )
