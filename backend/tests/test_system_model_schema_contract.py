from __future__ import annotations

from pathlib import Path

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, Uuid

from deerflow.persistence.system_settings import SystemModelConfigRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def test_system_model_schema_has_terminal_soft_delete_state() -> None:
    column = SystemModelConfigRow.__table__.c.deleted_at

    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.nullable is True
    constraints = {constraint.name: " ".join(str(constraint.sqltext).split()) for constraint in SystemModelConfigRow.__table__.constraints if constraint.name is not None and hasattr(constraint, "sqltext")}
    assert constraints["ck_system_model_configs_deleted_state"] == ("deleted_at IS NULL OR status = 'suspended'")

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE system_model_configs (", 1)[1].split(
        ");",
        1,
    )[0]
    assert "deleted_at TIMESTAMP WITH TIME ZONE" in table
    assert ("CONSTRAINT ck_system_model_configs_deleted_state CHECK (deleted_at IS NULL OR status = 'suspended')") in table


def test_system_model_status_catalog_index_contains_only_live_rows() -> None:
    index = next(index for index in SystemModelConfigRow.__table__.indexes if index.name == "ix_system_model_configs_status_created")

    where = index.dialect_options["postgresql"]["where"]
    assert where is not None
    assert " ".join(str(where).split()) == ("system_model_configs.deleted_at IS NULL")

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert ("CREATE INDEX ix_system_model_configs_status_created ON system_model_configs (status, created_at DESC, id DESC) WHERE deleted_at IS NULL;") in schema


def test_system_model_schema_requires_bounded_max_input_tokens() -> None:
    column = SystemModelConfigRow.__table__.c.max_input_tokens

    assert isinstance(column.type, BigInteger)
    assert column.nullable is False
    constraints = {constraint.name: " ".join(str(constraint.sqltext).split()) for constraint in SystemModelConfigRow.__table__.constraints if constraint.name is not None and hasattr(constraint, "sqltext")}
    assert constraints["ck_system_model_configs_max_input_tokens"] == ("max_input_tokens BETWEEN 1 AND 2000000")

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE system_model_configs (", 1)[1].split(
        ");",
        1,
    )[0]
    assert "max_input_tokens BIGINT NOT NULL" in table
    assert ("CONSTRAINT ck_system_model_configs_max_input_tokens CHECK (max_input_tokens BETWEEN 1 AND 2000000)") in table


def test_system_model_schema_requires_provider_binding() -> None:
    column = SystemModelConfigRow.__table__.c.provider_id

    assert isinstance(column.type, Uuid)
    assert column.nullable is False

    provider_fk = next(
        (constraint for constraint in SystemModelConfigRow.__table__.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name == "fk_system_model_configs_provider"),
        None,
    )
    assert provider_fk is not None
    assert provider_fk.use_alter is True
    element = next(iter(provider_fk.elements))
    assert element.target_fullname == "model_providers.id"
    assert element.ondelete == "RESTRICT"

    index_names = {index.name for index in SystemModelConfigRow.__table__.indexes}
    assert "ix_system_model_configs_provider" in index_names

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    table = schema.split("CREATE TABLE system_model_configs (", 1)[1].split(
        ");",
        1,
    )[0]
    assert "provider_id UUID NOT NULL" in table
    # The provider table is created later in the snapshot, so the foreign key
    # must be a named ALTER TABLE placed after model_providers exists.
    assert "REFERENCES model_providers" not in table
    provider_table_at = schema.index("CREATE TABLE model_providers (")
    alter_statement = "ALTER TABLE system_model_configs ADD CONSTRAINT fk_system_model_configs_provider FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE RESTRICT;"
    assert alter_statement in schema
    assert schema.index(alter_statement) > provider_table_at
    assert ("CREATE INDEX ix_system_model_configs_provider ON system_model_configs (provider_id);") in schema
