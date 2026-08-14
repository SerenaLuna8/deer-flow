from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.shared_assets.skill_design_model import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _checks(table) -> dict[str, str]:
    return {str(constraint.name): re.sub(r"\s+", " ", str(constraint.sqltext)).strip() for constraint in table.constraints if isinstance(constraint, CheckConstraint)}


def test_dependency_and_terminal_receipt_columns_match_orm() -> None:
    session_table = SkillDesignSessionRow.__table__
    assert session_table.c.authoring_dependencies_json.nullable is True
    assert "jsonb_array_length" in _checks(session_table)["ck_skill_design_sessions_authoring_dependencies"]

    operation_table = SkillDesignOperationRow.__table__
    assert operation_table.c.terminal_kind.nullable is True
    assert operation_table.c.terminal_request_checksum.nullable is True
    checks = _checks(operation_table)
    assert "clarification" in checks["ck_skill_design_operations_terminal_kind"]
    assert "candidate" in checks["ck_skill_design_operations_terminal_kind"]
    assert "terminal_request_checksum IS NULL" in checks["ck_skill_design_operations_terminal_pair"]


def test_full_schema_keeps_the_safe_constraint_and_stamps_the_head() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "authoring_dependencies_json JSONB" in schema
    assert "CASE WHEN jsonb_typeof(authoring_dependencies_json -> 'requirements') = 'array'" in schema
    assert "terminal_kind VARCHAR(16)" in schema
    assert "terminal_request_checksum CHAR(64)" in schema
    assert "section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas')" in schema
    assert f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');" in schema
