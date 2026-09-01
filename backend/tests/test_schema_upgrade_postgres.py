"""PostgreSQL integration coverage for the explicit schema upgrade entry point."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.bootstrap as bootstrap_module
import deerflow.persistence.schema_upgrade as schema_upgrade_module
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    SchemaRecreateRequired,
    SchemaUpgradeRequired,
    _install_full_schema,
    classify_database,
    load_schema_comment_statements,
)
from deerflow.persistence.final_schema_contract import (
    FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
    inventory_user_schema_objects,
    read_schema_v1_catalog_signature,
)
from deerflow.persistence.schema_upgrade import (
    SCHEMA_COMMENTS_PLACEHOLDER,
    SchemaMigration,
    SchemaUpgradeError,
    schema_inventory_digest,
    upgrade_schema,
)
from scripts.upgrade_postgres import upgrade_postgres


async def _install_future_v2_migration(
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    statement: str,
) -> SchemaMigration:
    async with engine.connect() as connection:
        source_objects = await inventory_user_schema_objects(connection)
    migration_path = tmp_path / "schema_v1_to_schema_v2.sql"
    migration_path.write_text(
        f"{statement}\n{SCHEMA_COMMENTS_PLACEHOLDER}\n",
        encoding="utf-8",
    )
    migration = SchemaMigration(
        source_revision="schema_v1",
        target_revision="schema_v2",
        sql_path=migration_path,
        source_catalog_signature=FINAL_SCHEMA_V1_CATALOG_SIGNATURE,
        source_inventory_digest=schema_inventory_digest(source_objects),
    )
    monkeypatch.setattr(schema_upgrade_module, "MIGRATIONS", (migration,))
    monkeypatch.setattr(
        schema_upgrade_module,
        "CURRENT_SCHEMA_REVISION",
        "schema_v2",
    )
    monkeypatch.setattr(
        bootstrap_module,
        "CURRENT_SCHEMA_REVISION",
        "schema_v2",
    )
    return migration


@pytest.mark.asyncio
async def test_current_schema_upgrade_is_an_exact_noop(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        async with engine.connect() as connection:
            before = await read_schema_v1_catalog_signature(connection)
        executed: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            executed.append(statement.strip())

        event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)

        try:
            result = await upgrade_schema(engine)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

        assert result.previous_revision == CURRENT_SCHEMA_REVISION
        assert result.current_revision == CURRENT_SCHEMA_REVISION
        assert result.upgraded is False
        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            after = await read_schema_v1_catalog_signature(connection)
        assert marker == CURRENT_SCHEMA_REVISION
        assert before == after == FINAL_SCHEMA_V1_CATALOG_SIGNATURE
        assert not any(
            statement.upper().startswith(
                (
                    "ALTER ",
                    "COMMENT ",
                    "CREATE ",
                    "DELETE ",
                    "DROP ",
                    "INSERT ",
                    "TRUNCATE ",
                    "UPDATE ",
                ),
            )
            for statement in executed
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_database_upgrade_refuses_without_mutation(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        with pytest.raises(SchemaUpgradeError, match="setup-db"):
            await upgrade_schema(engine)

        async with engine.connect() as connection:
            assert not await connection.scalar(
                text("SELECT to_regclass('alembic_version') IS NOT NULL"),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_operator_upgrade_entry_point_reports_current_schema_v1(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url)

    assert result.previous_revision == "schema_v1"
    assert result.current_revision == "schema_v1"
    assert result.upgraded is False


@pytest.mark.asyncio
async def test_simulated_future_v1_to_v2_upgrade_publishes_the_new_marker(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        await _install_future_v2_migration(
            engine,
            monkeypatch,
            tmp_path,
            statement=("UPDATE users SET token_version = token_version WHERE false;"),
        )
        executed: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            executed.append(statement.strip())

        event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)

        try:
            result = await upgrade_schema(engine)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

        assert result.previous_revision == "schema_v1"
        assert result.current_revision == "schema_v2"
        assert result.upgraded is True
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT version_num FROM alembic_version"),
                )
                == "schema_v2"
            )
            assert await read_schema_v1_catalog_signature(connection) == FINAL_SCHEMA_V1_CATALOG_SIGNATURE
        migration_index = next(index for index, statement in enumerate(executed) if statement.startswith("UPDATE users SET token_version"))
        comment_indexes = [index for index, statement in enumerate(executed) if statement.startswith("COMMENT ON ")]
        marker_index = next(index for index, statement in enumerate(executed) if statement.startswith("UPDATE alembic_version SET version_num"))
        assert comment_indexes
        assert migration_index < min(comment_indexes) <= max(comment_indexes) < marker_index
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_target_catalog_failure_rolls_back_ddl_comments_and_marker(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        async with engine.connect() as connection:
            original_users_comment = await connection.scalar(
                text("SELECT obj_description('users'::regclass, 'pg_class')"),
            )
        await _install_future_v2_migration(
            engine,
            monkeypatch,
            tmp_path,
            statement="ALTER TABLE users ADD COLUMN future_example INTEGER;",
        )
        comment_statements = load_schema_comment_statements()
        changed_comment_statements = tuple("COMMENT ON TABLE users IS 'future users comment';" if statement.startswith("COMMENT ON TABLE users ") else statement for statement in comment_statements)
        monkeypatch.setattr(
            schema_upgrade_module,
            "load_schema_comment_statements",
            lambda: changed_comment_statements,
        )

        with pytest.raises(SchemaUpgradeError, match="packaged catalog"):
            await upgrade_schema(engine)

        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            future_column = await connection.scalar(
                text(
                    """SELECT 1
                       FROM information_schema.columns
                       WHERE table_schema=current_schema()
                         AND table_name='users'
                         AND column_name='future_example'""",
                ),
            )
            users_comment = await connection.scalar(
                text("SELECT obj_description('users'::regclass, 'pg_class')"),
            )
        assert marker == "schema_v1"
        assert future_column is None
        assert users_comment == original_users_comment
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_upgrade_rejects_source_catalog_drift_before_ddl(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        await _install_future_v2_migration(
            engine,
            monkeypatch,
            tmp_path,
            statement="ALTER TABLE users ADD COLUMN future_example INTEGER;",
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("COMMENT ON TABLE users IS 'drift before upgrade'"),
            )

        with pytest.raises(SchemaUpgradeError, match="migration source"):
            await upgrade_schema(engine)

        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            future_column = await connection.scalar(
                text(
                    """SELECT 1
                       FROM information_schema.columns
                       WHERE table_schema=current_schema()
                         AND table_name='users'
                         AND column_name='future_example'""",
                ),
            )
            users_comment = await connection.scalar(
                text("SELECT obj_description('users'::regclass, 'pg_class')"),
            )
        assert marker == "schema_v1"
        assert future_column is None
        assert users_comment == "drift before upgrade"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_classification_requires_an_exact_packaged_predecessor(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        await _install_future_v2_migration(
            engine,
            monkeypatch,
            tmp_path,
            statement="UPDATE users SET token_version = token_version WHERE false;",
        )

        async with engine.connect() as connection:
            with pytest.raises(SchemaUpgradeRequired):
                await classify_database(connection)

        async with engine.begin() as connection:
            await connection.execute(
                text("COMMENT ON TABLE users IS 'unsupported predecessor drift'"),
            )
        async with engine.connect() as connection:
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_upgrade_rejects_non_public_sql_before_database_mutation(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA shadow"))
        await _install_future_v2_migration(
            engine,
            monkeypatch,
            tmp_path,
            statement="CREATE TABLE shadow/**/.future_example (id INTEGER);",
        )

        with pytest.raises(SchemaUpgradeError, match="forbidden statement"):
            await upgrade_schema(engine)

        async with engine.connect() as connection:
            marker = await connection.scalar(
                text("SELECT version_num FROM alembic_version"),
            )
            shadow_table = await connection.scalar(
                text("SELECT to_regclass('shadow.future_example')"),
            )
        assert marker == "schema_v1"
        assert shadow_table is None
    finally:
        await engine.dispose()
