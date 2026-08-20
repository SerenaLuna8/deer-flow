from __future__ import annotations

from pathlib import Path

from deerflow.persistence.shared_assets import SkillVersionRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def test_skill_version_revocation_is_explicit_and_one_way_in_the_schema() -> None:
    assert "revoked_at" in SkillVersionRow.__table__.columns
    assert "revoked_by_user_id" in SkillVersionRow.__table__.columns
    assert "revocation_reason_code" in SkillVersionRow.__table__.columns

    payload = FULL_SCHEMA.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION enforce_system_skill_version_revocation()" in payload
    assert "CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions" in payload
    assert "CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions" in payload


def test_system_skill_binding_trigger_rejects_revoked_versions() -> None:
    payload = FULL_SCHEMA.read_text(encoding="utf-8")
    assert "version_revoked_at timestamp with time zone;" in payload
    assert "version_revoked_at IS NOT NULL" in payload
    assert "system binding requires an eligible Current Version" in payload
