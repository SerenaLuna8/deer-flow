from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.shared_assets.bootstrap.catalog import catalog_payload, load_bootstrap_catalog
from deerflow.persistence.shared_assets.skill_design_model import (
    SkillDesignOperationRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from migrations.versions import full_schema_v14_skill_builder_durable_agent as v14

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def test_builder_thread_and_run_link_are_scoped_in_orm() -> None:
    thread_table = ThreadMetaRow.__table__
    assert thread_table.c.thread_kind.nullable is False
    assert str(thread_table.c.thread_kind.server_default.arg) == "chat"
    thread_checks = {constraint.name: _normalized(constraint.sqltext) for constraint in thread_table.constraints if isinstance(constraint, CheckConstraint)}
    assert thread_checks["ck_threads_meta_kind"] == "thread_kind IN ('chat', 'skill_builder')"

    operation_table = SkillDesignOperationRow.__table__
    assert operation_table.c.run_id.nullable is True
    run_fk = next(constraint for constraint in operation_table.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name == "fk_skill_design_operations_run")
    assert tuple(run_fk.column_keys) == ("project_id", "owner_user_id", "run_id")
    assert tuple(element.target_fullname for element in run_fk.elements) == (
        "runs.project_id",
        "runs.owner_user_id",
        "runs.run_id",
    )
    assert run_fk.ondelete == "RESTRICT"
    assert any(isinstance(constraint, UniqueConstraint) and constraint.name == "uq_skill_design_operations_run" and tuple(constraint.columns.keys()) == ("project_id", "owner_user_id", "run_id") for constraint in operation_table.constraints)

    session_table = SkillDesignOperationRow.metadata.tables["skill_design_sessions"]
    assert all(tuple(constraint.column_keys) != ("project_id", "owner_user_id", "thread_id") for constraint in session_table.constraints if isinstance(constraint, ForeignKeyConstraint))


def test_v14_full_schema_pins_builder_thread_and_run_link() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "thread_kind VARCHAR(16) DEFAULT 'chat' NOT NULL" in schema
    assert "CONSTRAINT ck_threads_meta_kind CHECK (thread_kind IN ('chat', 'skill_builder'))" in schema
    assert "CONSTRAINT fk_skill_design_operations_run FOREIGN KEY(project_id,owner_user_id,run_id) REFERENCES runs(project_id,owner_user_id,run_id) ON DELETE RESTRICT" in schema
    assert "CONSTRAINT uq_skill_design_operations_run UNIQUE(project_id,owner_user_id,run_id)" in schema
    assert "INSERT INTO alembic_version (version_num) VALUES ('full_schema_v17');" in schema


def test_v14_migration_is_an_independent_forward_only_step() -> None:
    assert v14.revision == "full_schema_v14"
    assert v14.down_revision == "full_schema_v13"

    try:
        v14.downgrade()
    except RuntimeError as error:
        assert "do not support downgrade" in str(error)
    else:  # pragma: no cover - release contract
        raise AssertionError("v14 downgrade must fail closed")

    migration_source = (BACKEND_ROOT / "migrations" / "versions" / "full_schema_v14_skill_builder_durable_agent.py").read_text(encoding="utf-8")
    assert "builtin:agent:skill-builder" not in migration_source
    assert "INSERT INTO agents" not in migration_source
    assert "skill_design_sessions" not in migration_source


def test_packaged_skill_builder_agent_has_only_the_creator_dependency() -> None:
    catalog = load_bootstrap_catalog()
    entry = next(item for item in catalog.entries if item.source_key == "builtin:agent:skill-builder")
    payload = json.loads(catalog_payload(catalog, entry))

    assert entry.kind == "agent"
    assert entry.slug == "skill-builder"
    assert entry.version == 1
    assert payload["skill_source_keys"] == ["builtin:skill:skill-creator"]
    assert payload["mcp_source_keys"] == []
    assert payload["tool_groups"] == []
