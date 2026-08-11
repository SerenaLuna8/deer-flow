from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

import deerflow.persistence.models  # noqa: F401 -- register final metadata
from app.final_schema import FINAL_REQUIRED_RELATIONS
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages/harness/deerflow/persistence/full_schema.sql"


def test_v12_all_mutation_idempotency_has_one_durable_control_operation_authority() -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v12"
    assert "workflow_control_operations" in Base.metadata.tables
    assert "workflow_control_operations" in FINAL_REQUIRED_RELATIONS

    table = Base.metadata.tables["workflow_control_operations"]
    assert set(table.c.keys()) == {
        "project_id",
        "workflow_id",
        "operation",
        "scope_key",
        "idempotency_hash",
        "request_digest",
        "result_version_id",
        "result_revision",
        "result_checksum",
        "result_slot_id",
        "result_credential_id",
        "result_credential_version_id",
        "result_status",
        "result_deleted",
        "result_created_at",
        "result_updated_at",
        "result_revoked_at",
        "result_name",
        "result_description",
        "result_lifecycle",
        "result_published_version_id",
        "result_published_version_number",
        "result_draft_revision",
        "result_draft_checksum",
        "result_missing_slot_ids_csv",
        "created_by",
        "created_at",
    }
    assert table.c.idempotency_hash.type.length == 64
    assert table.c.request_digest.type.length == 64
    assert table.c.operation.type.length == 32
    assert {constraint.name for constraint in table.constraints} >= {
        "pk_workflow_control_operations",
        "ck_workflow_control_operations_operation",
        "ck_workflow_control_operations_idempotency_hash",
        "ck_workflow_control_operations_request_digest",
        "ck_workflow_control_operations_scope_key",
        "ck_workflow_control_operations_scope_shape",
        "ck_workflow_control_operations_result_missing_slots",
        "ck_workflow_control_operations_version_shape",
        "ck_workflow_control_operations_slot_shape",
        "ck_workflow_control_operations_credential_shape",
        "ck_workflow_control_operations_delete_shape",
        "ck_workflow_control_operations_definition_shape",
        "ck_workflow_control_operations_lifecycle_shape",
        "ck_workflow_control_operations_publication_shape",
        "ck_workflow_control_operations_draft_shape",
        "ck_workflow_control_operations_revision_shape",
        "ck_workflow_control_operations_status_shape",
        "ck_workflow_control_operations_created_at_shape",
        "ck_workflow_control_operations_updated_at_shape",
        "ck_workflow_control_operations_revoked_at_shape",
        "ck_workflow_control_operations_publish_shape",
        "fk_workflow_control_operations_definition",
        "fk_workflow_control_operations_result_version",
        "fk_workflow_control_operations_published_version",
        "fk_workflow_control_operations_actor",
    }
    primary_key = table.primary_key
    assert tuple(column.name for column in primary_key.columns) == (
        "project_id",
        "operation",
        "scope_key",
        "idempotency_hash",
    )

    schema_sql = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE workflow_control_operations (" in schema_sql
    assert "INSERT INTO alembic_version (version_num) VALUES ('full_schema_v12');" in schema_sql
    assert "full_schema_v10');" not in schema_sql


def test_v12_schema_revision_is_explicit_and_only_extends_v11() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    revision = ScriptDirectory.from_config(config).get_revision(CURRENT_SCHEMA_REVISION)
    assert revision is not None
    assert revision.down_revision == "full_schema_v11"
    source = Path(revision.path).read_text(encoding="utf-8")
    assert "ALTER TABLE workflow_control_operations" in source
    assert "CREATE TABLE workflow_control_operations" not in source
    assert "deerflow.persistence" not in source
    assert "from app." not in source


def test_g15_published_code_and_http_requirements_are_normalized_immutable_authority() -> None:
    code = Base.metadata.tables["workflow_version_code_requirements"]
    http = Base.metadata.tables["workflow_version_http_requirements"]
    slots = Base.metadata.tables["workflow_version_credential_slots"]
    assert {code.name, http.name} <= set(FINAL_REQUIRED_RELATIONS)
    assert tuple(column.name for column in code.primary_key.columns) == (
        "workflow_version_id",
        "node_id",
    )
    assert tuple(column.name for column in http.primary_key.columns) == (
        "workflow_version_id",
        "node_id",
    )
    assert {constraint.name for constraint in code.constraints} >= {
        "ck_workflow_version_code_requirements_contract",
        "fk_workflow_version_code_requirements_version",
    }
    assert {constraint.name for constraint in http.constraints} >= {
        "ck_workflow_version_http_requirements_method",
        "ck_workflow_version_http_requirements_endpoint",
        "ck_workflow_version_http_requirements_injection",
        "ck_workflow_version_http_requirements_slot",
        "ck_workflow_version_http_requirements_auth_pair",
        "fk_workflow_version_http_requirements_version",
        "fk_workflow_version_http_requirements_slot",
    }
    assert "uq_workflow_version_credential_slots_scope" in {constraint.name for constraint in slots.constraints}
    schema_sql = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    for relation in (code.name, http.name):
        assert f"CREATE TABLE {relation} (" in schema_sql
        assert f"CREATE TRIGGER trg_{relation}_immutable" in schema_sql
