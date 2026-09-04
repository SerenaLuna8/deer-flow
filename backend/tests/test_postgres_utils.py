"""Safety contract for isolated PostgreSQL test databases."""

from __future__ import annotations

import pytest
from postgres_utils import _validate_test_database_name, replace_database, temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


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


@pytest.mark.asyncio
async def test_template_clones_preserve_schema_and_isolate_writes(postgres_admin_url) -> None:
    """Clones must copy the prepared catalog, not share mutable test data."""
    async with temporary_postgres_database(postgres_admin_url) as template_url:
        template = make_url(template_url).database
        engine = create_async_engine(template_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("CREATE TABLE clone_probe (value integer, embedding vector(3))"))
                await connection.execute(text("INSERT INTO clone_probe VALUES (1, '[1,0,0]')"))
        finally:
            await engine.dispose()

        clone_names = []
        for value in (2, 3):
            async with temporary_postgres_database(postgres_admin_url, template=template) as clone_url:
                clone_names.append(make_url(clone_url).database)
                engine = create_async_engine(clone_url)
                try:
                    async with engine.begin() as connection:
                        assert await connection.scalar(text("SELECT value FROM clone_probe")) == 1
                        assert await connection.scalar(text("SELECT vector_dims(embedding) FROM clone_probe")) == 3
                        await connection.execute(text("UPDATE clone_probe SET value = :value"), {"value": value})
                finally:
                    await engine.dispose()
        assert len({template, *clone_names}) == 3
        engine = create_async_engine(postgres_admin_url)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM pg_database WHERE datname = ANY(:names)"), {"names": clone_names}) == 0
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_template_name_is_validated_before_connecting() -> None:
    with pytest.raises(RuntimeError, match="refusing unsafe PostgreSQL test database name"):
        async with temporary_postgres_database("not-a-database-url", template='production"; DROP DATABASE postgres; --'):
            pytest.fail("unsafe template was accepted")
