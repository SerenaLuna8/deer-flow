from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from deerflow.persistence.channel_connections.group_model import (
    ProjectChannelGroupBindingRow,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def test_channel_group_binding_tombstone_releases_only_the_agent_pair() -> None:
    table = ProjectChannelGroupBindingRow.__table__

    assert table.c.agent_asset_id.nullable is True
    assert table.c.agent_scope.nullable is True
    assert table.c.project_id.nullable is False
    assert table.c.id.nullable is False

    checks = {constraint.name: _normalized(constraint.sqltext) for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
    assert checks["ck_project_channel_group_bindings_agent_ref_pair"] == "(agent_asset_id IS NULL) = (agent_scope IS NULL)"
    assert checks["ck_project_channel_group_bindings_agent_lifecycle"] == "(deleted_at IS NULL) = (agent_asset_id IS NOT NULL)"
    assert checks["ck_project_channel_group_bindings_deleted_status"] == "deleted_at IS NULL OR status = 'disabled'"

    agent_foreign_key = next(constraint for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name == "fk_project_channel_group_bindings_agent")
    assert agent_foreign_key.ondelete == "RESTRICT"


def test_full_schema_pins_the_v13_binding_lifecycle_contract() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    assert "agent_scope VARCHAR(16)," in schema
    assert "agent_asset_id UUID," in schema
    assert "CONSTRAINT ck_project_channel_group_bindings_agent_ref_pair CHECK ((agent_asset_id IS NULL) = (agent_scope IS NULL))" in schema
    assert "CONSTRAINT ck_project_channel_group_bindings_agent_lifecycle CHECK ((deleted_at IS NULL) = (agent_asset_id IS NOT NULL))" in schema
    assert "INSERT INTO alembic_version (version_num) VALUES ('full_schema');" in schema
