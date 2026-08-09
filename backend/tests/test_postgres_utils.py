"""Safety contract for isolated PostgreSQL test databases."""

from __future__ import annotations

import pytest
from postgres_utils import _validate_test_database_name, replace_database
from sqlalchemy.engine import make_url


@pytest.mark.parametrize(
    "database",
    (
        "deerflow",
        "postgres",
        "deerflow_test_unit",
        "deerflow_test_1_not-a-uuid",
        "production",
    ),
)
def test_unsafe_or_non_generated_database_names_are_rejected(database: str) -> None:
    with pytest.raises(RuntimeError, match="refusing unsafe PostgreSQL test database name"):
        _validate_test_database_name(database)


def test_generated_database_name_is_accepted() -> None:
    _validate_test_database_name("deerflow_test_123_0123456789abcdef0123456789abcdef")


def test_development_url_is_only_used_to_derive_maintenance_and_test_targets() -> None:
    development_url = "postgresql+asyncpg://developer:secret@127.0.0.1:5432/deerflow"
    test_database = "deerflow_test_123_0123456789abcdef0123456789abcdef"

    assert make_url(development_url).database == "deerflow"
    assert make_url(replace_database(development_url, "postgres")).database == "postgres"
    assert make_url(replace_database(development_url, test_database)).database == test_database
