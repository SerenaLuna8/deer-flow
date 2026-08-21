from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.final_schema import FINAL_REQUIRED_RELATIONS
from deerflow.persistence.final_schema_contract import FINAL_APP_SEQUENCES
from deerflow.persistence.shared_assets import (
    SkillDesignActivityRow,
    SkillDesignOperationBaselineFileRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _checks(table) -> dict[str, str]:
    return {str(constraint.name): re.sub(r"\s+", " ", str(constraint.sqltext)).strip() for constraint in table.constraints if isinstance(constraint, CheckConstraint)}


def test_execution_preference_and_stopped_operation_are_pinned_in_orm() -> None:
    session = SkillDesignSessionRow.__table__
    assert session.c.execution_model_ref.nullable is True
    assert session.c.execution_mode.nullable is True
    assert session.c.execution_thinking_enabled.nullable is True
    assert session.c.execution_reasoning_effort.nullable is True
    assert (
        "execution_mode IN ('flash', 'thinking', 'pro', 'ultra')"
        in _checks(
            session,
        )["ck_skill_design_sessions_execution_preference"]
    )

    operation = SkillDesignOperationRow.__table__
    assert operation.c.stop_requested_at.nullable is True
    assert "stopped" in _checks(operation)["ck_skill_design_operations_status"]
    assert "status = 'stopped'" in _checks(operation)["ck_skill_design_operations_completion"]
    assert any(
        isinstance(constraint, UniqueConstraint) and constraint.name == "uq_skill_design_operations_private_scope" and tuple(constraint.columns.keys()) == ("project_id", "owner_user_id", "session_id", "id")
        for constraint in operation.constraints
    )


def test_activity_and_baseline_tables_are_private_and_bounded() -> None:
    activity = SkillDesignActivityRow.__table__
    operation_fk = next(constraint for constraint in activity.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name == "fk_skill_design_activities_operation")
    assert tuple(operation_fk.column_keys) == (
        "project_id",
        "owner_user_id",
        "session_id",
        "operation_id",
    )
    assert operation_fk.ondelete == "CASCADE"
    assert _checks(activity)["ck_skill_design_activities_attempt"] == ("attempt IS NULL OR attempt >= 1")
    assert {index.name for index in activity.indexes} >= {
        "ix_skill_design_activities_session_seq",
        "uq_skill_design_activities_source_event",
        "uq_skill_design_activities_terminal",
    }

    baseline = SkillDesignOperationBaselineFileRow.__table__
    assert "size_bytes <= 2097152" in _checks(baseline)["ck_skill_design_operation_baseline_files_size"]
    baseline_fk = next(constraint for constraint in baseline.constraints if isinstance(constraint, ForeignKeyConstraint))
    assert tuple(baseline_fk.column_keys) == (
        "project_id",
        "owner_user_id",
        "session_id",
        "operation_id",
    )
    assert baseline_fk.ondelete == "CASCADE"


def test_fresh_schema_and_readiness_contract_include_builder_activity() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE skill_design_activities (" in schema
    assert "CREATE TABLE skill_design_operation_baseline_files (" in schema
    assert "CREATE UNIQUE INDEX uq_skill_design_activities_terminal" in schema
    assert "skill_design_activities" in FINAL_REQUIRED_RELATIONS
    assert "skill_design_operation_baseline_files" in FINAL_REQUIRED_RELATIONS
    assert (
        "skill_design_activities_seq_seq",
        "skill_design_activities",
    ) in FINAL_APP_SEQUENCES
