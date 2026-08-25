from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.audit.models import AssetAuditMetadata, PurgeAuditMetadata
from app.gateway.routers.project_assets import SkillDeleteResponse, project_router
from app.shared_assets.skill_deletion import (
    ArchivedSkillPurgeReconciler,
    ArchivedSkillPurgeReport,
    SkillDeleteResult,
)
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


def test_skill_delete_is_a_first_class_secret_tombstone_reason() -> None:
    reason = next(constraint for constraint in ProjectSkillSecretTombstoneRow.__table__.constraints if constraint.name == "ck_project_skill_secret_tombstones_reason")

    assert "'skill_delete'" in str(reason.sqltext)


def test_skill_lifecycle_results_expose_only_safe_aggregate_facts() -> None:
    deletion = SkillDeleteResult(affected_agent_count=2)
    purge = ArchivedSkillPurgeReport(
        projects_scanned=1,
        skills_examined=3,
        skills_purged=1,
        versions_purged=2,
        released_bytes=4096,
    )

    assert deletion.affected_agent_count == 2
    assert purge == ArchivedSkillPurgeReport(1, 3, 1, 2, 4096)


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


def test_archived_skill_purge_audit_metadata_is_content_free() -> None:
    metadata = PurgeAuditMetadata(
        resource_kind="archived_skill",
        purged_count=2,
    )

    assert metadata.model_dump(mode="json") == {
        "resource_kind": "archived_skill",
        "purged_count": 2,
    }


class _FlakySweep:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = __import__("asyncio").Event()

    async def sweep(self, *, limit: int, request_id: str):
        del limit, request_id
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary purge failure")
        self.recovered.set()
        return ArchivedSkillPurgeReport(0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_low_frequency_purge_reconciler_survives_one_failed_pass() -> None:
    import asyncio

    sweep = _FlakySweep()
    reconciler = ArchivedSkillPurgeReconciler(
        sweep,  # type: ignore[arg-type]
        batch_size=10,
        interval_seconds=0.01,
    )

    await reconciler.start()
    try:
        await asyncio.wait_for(sweep.recovered.wait(), timeout=1)
    finally:
        await reconciler.aclose()

    assert reconciler.closed is True
    assert sweep.calls >= 2
