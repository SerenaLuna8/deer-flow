from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.sql.sqltypes import SmallInteger

from deerflow.persistence.final_schema_contract import REQUIRED_FUNCTIONS
from deerflow.persistence.models import RunSkillVersionRefRow as RegisteredRunSkillVersionRefRow
from deerflow.persistence.private_work import RunAssetVersionRow, RunSkillVersionRefRow
from deerflow.persistence.private_work.model import (
    _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION,
    _CREATE_RUN_CLOSURE_SEAL_FUNCTION,
    _CREATE_RUN_CLOSURE_VERIFY_FUNCTION,
    _RUN_CLOSURE_TRIGGER_DDL,
)
from deerflow.persistence.run import RunRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _checks(table) -> dict[str, str]:
    return {str(constraint.name): _normalized(constraint.sqltext) for constraint in table.constraints if isinstance(constraint, CheckConstraint)}


def _unique_columns(table, name: str) -> tuple[str, ...]:
    return next(tuple(constraint.columns.keys()) for constraint in table.constraints if isinstance(constraint, UniqueConstraint) and constraint.name == name)


def _foreign_key(table, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    constraint = next(constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name)
    return (
        tuple(constraint.columns.keys()),
        tuple(element.target_fullname for element in constraint.elements),
    )


def test_run_closure_schema_v1_has_typed_exact_skill_version_refs() -> None:
    assert RegisteredRunSkillVersionRefRow is RunSkillVersionRefRow

    run_table = RunRow.__table__
    assert run_table.c.asset_closure_sealed.nullable is False
    assert run_table.c.asset_closure_sealed.default.arg is False
    assert str(run_table.c.asset_closure_sealed.server_default.arg) == "false"
    assert _checks(run_table)["ck_runs_asset_closure_sealed"] == ("asset_closure_sealed IN (true, false)")

    asset_table = RunAssetVersionRow.__table__
    assert isinstance(asset_table.c.snapshot_schema_version.type, SmallInteger)
    assert asset_table.c.snapshot_schema_version.nullable is False
    assert _checks(asset_table)["ck_run_asset_versions_snapshot_schema"] == ("snapshot_schema_version BETWEEN 2 AND 4")
    assert _unique_columns(
        asset_table,
        "uq_run_asset_versions_dependency_order",
    ) == ("project_id", "owner_user_id", "run_id", "dependency_order")
    assert _unique_columns(asset_table, "uq_run_asset_versions_runtime_exact") == (
        "project_id",
        "owner_user_id",
        "thread_id",
        "run_id",
        "asset_kind",
        "dependency_order",
        "asset_scope",
        "asset_id",
        "version_id",
        "payload_checksum",
        "snapshot_schema_version",
    )
    assert {
        "ix_run_asset_versions_legacy_project_skill",
        "ix_run_asset_versions_legacy_skill_version",
    } <= {str(index.name) for index in asset_table.indexes}

    ref_table = RunSkillVersionRefRow.__table__
    assert tuple(ref_table.primary_key.columns.keys()) == (
        "project_id",
        "owner_user_id",
        "run_id",
        "asset_kind",
        "dependency_order",
    )
    assert _checks(ref_table) == {
        "ck_run_skill_version_refs_checksum": "payload_checksum ~ '^[0-9a-f]{64}$'",
        "ck_run_skill_version_refs_content_size": ("content_size_bytes BETWEEN 0 AND 104857600"),
        "ck_run_skill_version_refs_file_count": "file_count BETWEEN 1 AND 16384",
        "ck_run_skill_version_refs_kind": "asset_kind = 'skill'",
        "ck_run_skill_version_refs_order": "dependency_order >= 0",
        "ck_run_skill_version_refs_schema": "snapshot_schema_version = 4",
        "ck_run_skill_version_refs_scope": "asset_scope IN ('system', 'project')",
        "ck_run_skill_version_refs_scope_project": ("(asset_scope = 'system' AND skill_project_id IS NULL) OR (asset_scope = 'project' AND skill_project_id IS NOT NULL AND skill_project_id = project_id)"),
    }
    assert _unique_columns(
        ref_table,
        "uq_run_skill_version_refs_exact_version",
    ) == (
        "project_id",
        "owner_user_id",
        "run_id",
        "skill_id",
        "skill_version_id",
    )
    assert _foreign_key(
        ref_table,
        "fk_run_skill_version_refs_exact_run_asset",
    ) == (
        (
            "project_id",
            "owner_user_id",
            "thread_id",
            "run_id",
            "asset_kind",
            "dependency_order",
            "asset_scope",
            "skill_id",
            "skill_version_id",
            "payload_checksum",
            "snapshot_schema_version",
        ),
        (
            "run_asset_versions.project_id",
            "run_asset_versions.owner_user_id",
            "run_asset_versions.thread_id",
            "run_asset_versions.run_id",
            "run_asset_versions.asset_kind",
            "run_asset_versions.dependency_order",
            "run_asset_versions.asset_scope",
            "run_asset_versions.asset_id",
            "run_asset_versions.version_id",
            "run_asset_versions.payload_checksum",
            "run_asset_versions.snapshot_schema_version",
        ),
    )
    assert _foreign_key(ref_table, "fk_run_skill_version_refs_skill_scope") == (
        ("skill_id", "asset_scope"),
        ("skills.id", "skills.scope"),
    )
    assert _foreign_key(ref_table, "fk_run_skill_version_refs_project_skill") == (
        ("skill_project_id", "skill_id"),
        ("skills.project_id", "skills.id"),
    )
    assert _foreign_key(ref_table, "fk_run_skill_version_refs_exact_version") == (
        (
            "skill_id",
            "skill_version_id",
            "payload_checksum",
            "file_count",
            "content_size_bytes",
        ),
        (
            "skill_versions.skill_id",
            "skill_versions.id",
            "skill_versions.payload_checksum",
            "skill_versions.file_count",
            "skill_versions.content_size_bytes",
        ),
    )
    assert {
        "ix_run_skill_version_refs_version",
        "ix_run_skill_version_refs_skill_scope",
        "ix_run_skill_version_refs_project_skill",
    } == {str(index.name) for index in ref_table.indexes}

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    for fragment in (
        "asset_closure_sealed BOOLEAN DEFAULT false NOT NULL",
        "CONSTRAINT ck_runs_asset_closure_sealed CHECK (asset_closure_sealed IN (true, false))",
        "snapshot_schema_version SMALLINT NOT NULL",
        "CONSTRAINT ck_run_asset_versions_snapshot_schema CHECK (snapshot_schema_version BETWEEN 2 AND 4)",
        "CONSTRAINT uq_run_asset_versions_dependency_order UNIQUE (project_id, owner_user_id, run_id, dependency_order)",
        "CONSTRAINT uq_run_asset_versions_runtime_exact UNIQUE (project_id, owner_user_id, thread_id, run_id, asset_kind, dependency_order, asset_scope, asset_id, version_id, payload_checksum, snapshot_schema_version)",
        "CREATE TABLE run_skill_version_refs (",
        "CONSTRAINT fk_run_skill_version_refs_exact_run_asset FOREIGN KEY(project_id, owner_user_id, thread_id, run_id, asset_kind, dependency_order, asset_scope, skill_id, skill_version_id, payload_checksum, snapshot_schema_version)",
        "CONSTRAINT fk_run_skill_version_refs_exact_version FOREIGN KEY(skill_id, skill_version_id, payload_checksum, file_count, content_size_bytes)",
        "CREATE INDEX ix_run_asset_versions_legacy_project_skill",
        "CREATE INDEX ix_run_asset_versions_legacy_skill_version",
        "CREATE INDEX ix_run_skill_version_refs_version",
        "CREATE INDEX ix_run_skill_version_refs_skill_scope",
        "CREATE INDEX ix_run_skill_version_refs_project_skill",
    ):
        assert fragment in schema


def test_run_closure_trigger_contract_is_immediate_and_deferred() -> None:
    assert {
        "enforce_run_asset_closure_seal_transition",
        "gate_run_closure_child_mutation",
        "verify_run_asset_closure",
    } <= REQUIRED_FUNCTIONS
    assert "OLD.asset_closure_sealed IS FALSE" in _CREATE_RUN_CLOSURE_SEAL_FUNCTION
    assert "NEW.asset_closure_sealed IS TRUE" in _CREATE_RUN_CLOSURE_SEAL_FUNCTION
    assert "invalid Run asset closure seal transition" in _CREATE_RUN_CLOSURE_SEAL_FUNCTION

    for table in (
        "run_asset_versions",
        "run_skill_version_refs",
        "run_skill_secret_snapshots",
        "run_mcp_secret_snapshots",
    ):
        assert f"trg_{table}_closure_mutation" in "\n".join(_RUN_CLOSURE_TRIGGER_DDL)
    assert "FOR UPDATE" in _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION
    assert "deerflow.run_asset_closure_assembly" in (_CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION)
    assert "status IN ('queued', 'retry_wait')" in (_CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION)
    assert "status IN ('leased', 'running')" in (_CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION)
    assert "TG_OP = 'UPDATE'" in _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION
    assert "TG_OP = 'DELETE' AND NOT run_found" in (_CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION)
    for fragment in (
        "pg_temp.retention_purge_run_authority",
        "authority.purge_id IS NOT NULL",
        "authority.project_id = $1",
        "authority.thread_id = $2",
        "authority.run_id = $3",
        "authority.owner_user_id = $4",
        "TG_TABLE_NAME = 'run_skill_version_refs'",
        "Run Skill ref cannot be deleted independently",
    ):
        assert fragment in _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION
    assert "deerflow.retention_purge" not in _CREATE_RUN_CLOSURE_CHILD_GATE_FUNCTION

    for fragment in (
        "current_run.asset_closure_sealed IS NOT TRUE",
        "snapshot_schema_version",
        "snapshot_json->>'schema_version'",
        "snapshot_json->>'asset_id'",
        "snapshot_json->>'version_id'",
        "snapshot_json->>'checksum'",
        "snapshot_json->>'catalog_generation'",
        "snapshot_json - 'schema_version'",
        "snapshot_json->'skill'",
        "octet_length(asset.snapshot_json::text) > 262144",
        "ref_count != 1",
        "ref_count != 0",
        "max_dependency_order != asset_count - 1",
        "('success', 'error', 'timeout', 'interrupted', 'deleted')",
        "run_skill_secret_snapshots",
        "run_mcp_secret_snapshots",
    ):
        assert fragment in _CREATE_RUN_CLOSURE_VERIFY_FUNCTION

    ddl = "\n".join(_RUN_CLOSURE_TRIGGER_DDL)
    assert "trg_runs_asset_closure_seal_transition" in ddl
    assert "CREATE CONSTRAINT TRIGGER trg_runs_asset_closure_complete" in ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    for fragment in (
        "CREATE OR REPLACE FUNCTION enforce_run_asset_closure_seal_transition()",
        "CREATE OR REPLACE FUNCTION gate_run_closure_child_mutation()",
        "CREATE OR REPLACE FUNCTION verify_run_asset_closure()",
        "CREATE TRIGGER trg_runs_asset_closure_seal_transition",
        "CREATE TRIGGER trg_run_asset_versions_closure_mutation",
        "CREATE TRIGGER trg_run_skill_version_refs_closure_mutation",
        "CREATE TRIGGER trg_run_skill_secret_snapshots_closure_mutation",
        "CREATE TRIGGER trg_run_mcp_secret_snapshots_closure_mutation",
        "CREATE CONSTRAINT TRIGGER trg_runs_asset_closure_complete",
        "DEFERRABLE INITIALLY DEFERRED",
    ):
        assert fragment in schema
