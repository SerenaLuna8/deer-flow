from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint

from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.shared_assets.skill_design_model import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from migrations.versions import (
    full_schema_v15_skill_builder_dependency_snapshot as v15,
)
from migrations.versions import (
    full_schema_v16_skill_builder_dependency_constraint as v16,
)
from migrations.versions import (
    full_schema_v17_automations_runtime_policy as v17,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _checks(table) -> dict[str, str]:
    return {str(constraint.name): re.sub(r"\s+", " ", str(constraint.sqltext)).strip() for constraint in table.constraints if isinstance(constraint, CheckConstraint)}


def test_v15_dependency_and_terminal_receipt_columns_match_orm() -> None:
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


def test_v15_remains_the_historical_direct_constraint_ancestor(
    monkeypatch,
) -> None:
    assert v15.revision == "full_schema_v15"
    assert v15.down_revision == "full_schema_v14"

    statements: list[str] = []
    monkeypatch.setattr(v15.op, "execute", statements.append)
    v15.upgrade()
    dependency_constraint = next(statement for statement in statements if "ADD CONSTRAINT ck_skill_design_sessions_authoring_dependencies" in statement)
    assert "jsonb_typeof(authoring_dependencies_json -> 'requirements') = 'array'" in dependency_constraint
    assert "jsonb_array_length(authoring_dependencies_json -> 'requirements') <= 64" in dependency_constraint
    assert "CASE WHEN" not in dependency_constraint

    try:
        v15.downgrade()
    except RuntimeError as error:
        assert "do not support downgrade" in str(error)
    else:  # pragma: no cover - release contract
        raise AssertionError("v15 downgrade must fail closed")


def test_v16_normalizes_only_the_dependency_constraint(
    monkeypatch,
) -> None:
    assert v16.revision == "full_schema_v16"
    assert v16.down_revision == "full_schema_v15"

    statements: list[str] = []
    monkeypatch.setattr(v16.op, "execute", statements.append)
    v16.upgrade()
    assert len(statements) == 2
    assert statements[0] == "ALTER TABLE skill_design_sessions DROP CONSTRAINT ck_skill_design_sessions_authoring_dependencies"
    assert statements[1].startswith("ALTER TABLE skill_design_sessions ADD CONSTRAINT ck_skill_design_sessions_authoring_dependencies CHECK (")
    assert "CASE WHEN jsonb_typeof(" in statements[1]
    assert "THEN jsonb_array_length(" in statements[1]
    assert "ELSE FALSE END" in statements[1]

    try:
        v16.downgrade()
    except RuntimeError as error:
        assert "do not support downgrade" in str(error)
    else:  # pragma: no cover - release contract
        raise AssertionError("v16 downgrade must fail closed")


def test_v17_adds_automations_policy_and_is_the_head(monkeypatch) -> None:
    assert v17.revision == CURRENT_SCHEMA_REVISION == "full_schema_v17"
    assert v17.down_revision == "full_schema_v16"

    statements: list[str] = []
    monkeypatch.setattr(v17.op, "execute", statements.append)
    v17.upgrade()
    assert any("ck_system_runtime_policies_section CHECK (section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas'))" in statement for statement in statements)
    assert any("section = 'automations'" in statement for statement in statements)

    try:
        v17.downgrade()
    except RuntimeError as error:
        assert "do not support downgrade" in str(error)
    else:  # pragma: no cover - release contract
        raise AssertionError("v17 downgrade must fail closed")


def test_full_schema_keeps_the_safe_constraint_and_stamps_v17() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "authoring_dependencies_json JSONB" in schema
    assert "CASE WHEN jsonb_typeof(authoring_dependencies_json -> 'requirements') = 'array'" in schema
    assert "terminal_kind VARCHAR(16)" in schema
    assert "terminal_request_checksum CHAR(64)" in schema
    assert "section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas')" in schema
    assert ("INSERT INTO alembic_version (version_num) VALUES ('full_schema_v17');") in schema
