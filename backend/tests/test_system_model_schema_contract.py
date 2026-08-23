from __future__ import annotations

from pathlib import Path

from sqlalchemy import BigInteger

from deerflow.persistence.system_settings import SystemModelConfigRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


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
