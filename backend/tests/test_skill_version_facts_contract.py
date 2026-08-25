from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from deerflow.persistence.final_schema_contract import REQUIRED_FUNCTIONS
from deerflow.persistence.shared_assets.binding_model import (
    _CREATE_CHILD_IMMUTABILITY_FUNCTION,
    _CREATE_IMMUTABLE_FUNCTION,
    _CREATE_SKILL_VERSION_FACTS_FUNCTION,
    _CREATE_SKILL_VERSION_SEAL_FUNCTION,
    _TRIGGER_DDL,
)
from deerflow.persistence.shared_assets.skill_model import (
    SkillVersionFileRow,
    SkillVersionRow,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def test_skill_version_facts_and_file_bounds_match_schema_v1() -> None:
    version_table = SkillVersionRow.__table__
    assert version_table.c.file_count.nullable is False
    assert version_table.c.content_size_bytes.nullable is False
    assert version_table.c.files_sealed.nullable is False
    assert version_table.c.files_sealed.default.arg is False
    assert str(version_table.c.files_sealed.server_default.arg) == "false"

    checks = {constraint.name: _normalized(constraint.sqltext) for constraint in version_table.constraints if isinstance(constraint, CheckConstraint)}
    assert checks["ck_skill_versions_file_count"] == "file_count BETWEEN 1 AND 16384"
    assert checks["ck_skill_versions_content_size"] == "content_size_bytes BETWEEN 0 AND 104857600"
    assert checks["ck_skill_versions_files_sealed"] == "files_sealed IN (true, false)"
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_skill_versions_runtime_exact"
        and tuple(constraint.columns.keys())
        == (
            "skill_id",
            "id",
            "payload_checksum",
            "file_count",
            "content_size_bytes",
        )
        for constraint in version_table.constraints
    )

    file_table = SkillVersionFileRow.__table__
    file_checks = {constraint.name: _normalized(constraint.sqltext) for constraint in file_table.constraints if isinstance(constraint, CheckConstraint)}
    assert file_checks["ck_skill_version_files_size"] == ("size_bytes >= 0 AND size_bytes <= 67108864")
    path_index = next(index for index in file_table.indexes if index.name == "ix_skill_version_files_version_path_c")
    assert str(CreateIndex(path_index).compile(dialect=postgresql.dialect())) == ('CREATE INDEX ix_skill_version_files_version_path_c ON skill_version_files (skill_version_id, path COLLATE "C")')

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "file_count INTEGER NOT NULL" in schema
    assert "content_size_bytes BIGINT NOT NULL" in schema
    assert "files_sealed BOOLEAN DEFAULT false NOT NULL" in schema
    assert "CONSTRAINT ck_skill_versions_file_count CHECK (file_count BETWEEN 1 AND 16384)" in schema
    assert "CONSTRAINT ck_skill_versions_content_size CHECK (content_size_bytes BETWEEN 0 AND 104857600)" in schema
    assert "CONSTRAINT ck_skill_versions_files_sealed CHECK (files_sealed IN (true, false))" in schema
    assert ("CONSTRAINT uq_skill_versions_runtime_exact UNIQUE (skill_id, id, payload_checksum, file_count, content_size_bytes)") in schema
    assert "CONSTRAINT ck_skill_version_files_size CHECK (size_bytes >= 0 AND size_bytes <= 67108864)" in schema
    assert ('CREATE INDEX ix_skill_version_files_version_path_c ON skill_version_files (skill_version_id, path COLLATE "C");') in schema


def test_skill_version_seal_triggers_match_schema_v1() -> None:
    assert {
        "enforce_skill_version_files_seal_transition",
        "verify_skill_version_file_facts",
    } <= REQUIRED_FUNCTIONS
    assert "'files_sealed'" in _CREATE_IMMUTABLE_FUNCTION
    assert "'skill_versions'" not in re.search(
        r"TG_TABLE_NAME IN \((.*?)\)",
        _CREATE_IMMUTABLE_FUNCTION,
        re.DOTALL,
    ).group(1)

    assert "OLD.files_sealed IS FALSE" in _CREATE_SKILL_VERSION_SEAL_FUNCTION
    assert "NEW.files_sealed IS TRUE" in _CREATE_SKILL_VERSION_SEAL_FUNCTION
    assert "invalid Skill version file seal transition" in _CREATE_SKILL_VERSION_SEAL_FUNCTION

    assert "version.files_sealed" in _CREATE_CHILD_IMMUTABILITY_FUNCTION
    assert "parent_files_sealed IS FALSE" in _CREATE_CHILD_IMMUTABILITY_FUNCTION
    assert "TG_OP = 'INSERT'" in _CREATE_CHILD_IMMUTABILITY_FUNCTION
    assert "TG_OP = 'DELETE' AND purge_allowed" in _CREATE_CHILD_IMMUTABILITY_FUNCTION

    assert "FROM skill_versions" in _CREATE_SKILL_VERSION_FACTS_FUNCTION
    assert "current_version.files_sealed IS NOT TRUE" in _CREATE_SKILL_VERSION_FACTS_FUNCTION
    assert "count(*)" in _CREATE_SKILL_VERSION_FACTS_FUNCTION
    assert "coalesce(sum(size_bytes), 0)" in _CREATE_SKILL_VERSION_FACTS_FUNCTION
    assert "actual_file_count IS DISTINCT FROM current_version.file_count" in (_CREATE_SKILL_VERSION_FACTS_FUNCTION)
    assert "actual_content_size IS DISTINCT FROM current_version.content_size_bytes" in (_CREATE_SKILL_VERSION_FACTS_FUNCTION)

    ddl = "\n".join(_TRIGGER_DDL)
    expected_triggers = (
        "trg_skill_versions_files_seal_transition",
        "trg_skill_versions_facts_complete",
        "DEFERRABLE INITIALLY DEFERRED",
    )
    assert all(fragment in ddl for fragment in expected_triggers)

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    for fragment in (
        "CREATE OR REPLACE FUNCTION enforce_skill_version_files_seal_transition()",
        "CREATE OR REPLACE FUNCTION verify_skill_version_file_facts()",
        "current_version.files_sealed IS NOT TRUE",
        "parent_files_sealed IS FALSE",
        "CREATE TRIGGER trg_skill_versions_files_seal_transition",
        "CREATE CONSTRAINT TRIGGER trg_skill_versions_facts_complete",
        "DEFERRABLE INITIALLY DEFERRED",
    ):
        assert fragment in schema
