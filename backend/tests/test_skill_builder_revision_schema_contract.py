"""Skill Builder 修订会话的目标/基线列、复合外键与部分唯一索引。"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.shared_assets.skill_design_model import (
    SkillDesignSessionRow,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _checks(table) -> dict[str, str]:
    return {str(constraint.name): re.sub(r"\s+", " ", str(constraint.sqltext)).strip() for constraint in table.constraints if isinstance(constraint, CheckConstraint)}


def _foreign_keys(table) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    result: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, ForeignKeyConstraint):
            result[str(constraint.name)] = (
                tuple(column.name for column in constraint.columns),
                tuple(element.target_fullname for element in constraint.elements),
            )
    return result


def test_revision_columns_match_orm() -> None:
    table = SkillDesignSessionRow.__table__

    assert table.c.session_kind.nullable is False
    assert table.c.session_kind.server_default.arg == "create"
    assert table.c.target_skill_id.nullable is True
    assert table.c.base_version_id.nullable is True
    assert table.c.base_version_number.nullable is True
    assert table.c.base_payload_checksum.nullable is True
    assert table.c.target_skill_deleted.nullable is False

    checks = _checks(table)
    assert checks["ck_skill_design_sessions_kind"] == "session_kind IN ('create', 'revise')"
    assert "^[0-9a-f]{64}$" in checks["ck_skill_design_sessions_base_checksum"]
    assert "base_version_number >= 1" in checks["ck_skill_design_sessions_base_version_number"]

    pairing = checks["ck_skill_design_sessions_revision_target"]
    assert "session_kind = 'create' AND target_skill_id IS NULL" in pairing
    assert "target_skill_deleted IS FALSE AND target_skill_id IS NOT NULL" in pairing
    assert "target_skill_deleted IS TRUE AND target_skill_id IS NULL" in pairing


def test_revision_composite_foreign_keys_pin_project_and_asset() -> None:
    foreign_keys = _foreign_keys(SkillDesignSessionRow.__table__)

    assert foreign_keys["fk_skill_design_sessions_target_skill_project"] == (
        ("project_id", "target_skill_id"),
        ("skills.project_id", "skills.id"),
    )
    assert foreign_keys["fk_skill_design_sessions_base_version"] == (
        ("target_skill_id", "base_version_id"),
        ("skill_versions.skill_id", "skill_versions.id"),
    )


def test_live_revise_target_partial_unique_index() -> None:
    table = SkillDesignSessionRow.__table__
    index = next(index for index in table.indexes if index.name == "uq_skill_design_sessions_live_revise_target")
    assert index.unique is True
    assert [column.name for column in index.expressions] == [
        "project_id",
        "owner_user_id",
        "target_skill_id",
    ]
    where = re.sub(r"\s+", " ", str(index.dialect_options["postgresql"]["where"]))
    assert "session_kind = 'revise'" in where
    assert "target_skill_id IS NOT NULL" in where
    assert "status NOT IN ('completed', 'cancelled')" in where


def test_full_schema_carries_the_revision_shape() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "session_kind VARCHAR(16) DEFAULT 'create' NOT NULL" in schema
    assert "target_skill_id UUID" in schema
    assert "base_payload_checksum CHAR(64)" in schema
    assert "CONSTRAINT ck_skill_design_sessions_revision_target" in schema
    assert "CONSTRAINT fk_skill_design_sessions_target_skill_project FOREIGN KEY(project_id, target_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT" in schema
    assert "CONSTRAINT fk_skill_design_sessions_base_version FOREIGN KEY(target_skill_id, base_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT" in schema
    assert (
        "CREATE UNIQUE INDEX uq_skill_design_sessions_live_revise_target ON "
        "skill_design_sessions (project_id, owner_user_id, target_skill_id) "
        "WHERE session_kind = 'revise' AND target_skill_id IS NOT NULL "
        "AND status NOT IN ('completed', 'cancelled');"
    ) in schema
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');" in schema
