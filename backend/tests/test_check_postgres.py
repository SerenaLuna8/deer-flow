from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from deerflow.persistence.base import Base
from scripts import check_postgres


def test_required_tables_exactly_cover_final_application_and_langgraph_schema() -> None:
    langgraph_tables = {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
        "store",
        "store_migrations",
    }
    assert set(check_postgres.REQUIRED_TABLES) == set(Base.metadata.tables) | langgraph_tables
    assert not {
        "migration_ledger",
        "private_work_cutover_state",
        "automation_cutover_state",
        "reliability_cutover_state",
    } & set(check_postgres.REQUIRED_TABLES)


def _connection(
    *,
    revision: str | None = "0001_project_saas_baseline",
    present_tables=None,
):
    connection = AsyncMock()
    connection.scalar.side_effect = ["PostgreSQL 17.5", revision is not None] + ([revision] if revision is not None else [])
    rows = MagicMock()
    rows.scalars.return_value = present_tables if present_tables is not None else check_postgres.REQUIRED_TABLES
    connection.execute.return_value = rows
    return connection


class _Engine:
    def __init__(self, connection=None, *, error: Exception | None = None) -> None:
        self.connection = connection
        self.error = error
        self.dispose = AsyncMock()

    @asynccontextmanager
    async def connect(self):
        if self.error is not None:
            raise self.error
        yield self.connection


@pytest.mark.asyncio
async def test_check_is_read_only_parameterized_and_healthy(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(check_postgres, "create_async_engine", lambda *_args, **_kwargs: _Engine(connection))
    monkeypatch.setattr(check_postgres, "classify_database", AsyncMock(return_value="m7"))
    monkeypatch.setattr(check_postgres, "get_head_revision", lambda: "0001_project_saas_baseline")

    result = await check_postgres.check_postgres("postgresql://owner:p%40ss@127.0.0.1:5432/deerflow_test_1_abc")

    assert result.healthy is True
    assert result.revision_matches is True
    assert result.missing_tables == ()
    sql, parameters = connection.execute.await_args.args
    assert "information_schema.tables" in str(sql)
    assert "CAST(:required_tables AS text[])" in str(sql)
    assert set(parameters["required_tables"]) == set(check_postgres.REQUIRED_TABLES)
    statements = [str(call.args[0]).lstrip().upper() for call in connection.method_calls if call.args]
    assert all(not statement.startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for statement in statements)


@pytest.mark.asyncio
async def test_check_distinguishes_missing_revision_and_tables(monkeypatch) -> None:
    connection = _connection(revision=None, present_tables={"users"})
    monkeypatch.setattr(check_postgres, "create_async_engine", lambda *_args, **_kwargs: _Engine(connection))
    monkeypatch.setattr(
        check_postgres,
        "classify_database",
        AsyncMock(side_effect=check_postgres.M7RecreateRequired()),
    )
    monkeypatch.setattr(check_postgres, "get_head_revision", lambda: "0001_project_saas_baseline")

    result = await check_postgres.check_postgres("postgresql://owner:secret@localhost/deerflow_test_1_abc")

    assert result.connected is True
    assert result.current_revision is None
    assert result.revision_matches is False
    assert "users" not in result.missing_tables
    assert result.missing_tables
    assert result.healthy is False


@pytest.mark.asyncio
async def test_check_is_unhealthy_for_old_revision_or_missing_final_table(monkeypatch) -> None:
    connection = _connection(
        revision="0015_project_reliability_finalize",
        present_tables=set(check_postgres.REQUIRED_TABLES) - {"projects"},
    )
    monkeypatch.setattr(check_postgres, "create_async_engine", lambda *_args, **_kwargs: _Engine(connection))
    monkeypatch.setattr(
        check_postgres,
        "classify_database",
        AsyncMock(side_effect=check_postgres.M7RecreateRequired()),
    )
    monkeypatch.setattr(check_postgres, "get_head_revision", lambda: "0001_project_saas_baseline")

    result = await check_postgres.check_postgres("postgresql://owner:secret@localhost/deerflow_test_1_abc")

    assert result.current_revision == "0015_project_reliability_finalize"
    assert result.revision_matches is False
    assert result.missing_tables == ("projects",)
    assert result.healthy is False


@pytest.mark.asyncio
async def test_check_connection_failure_is_sanitized(monkeypatch) -> None:
    error = RuntimeError("cannot connect postgresql://owner:database-secret@localhost/deerflow_test_1_abc")
    monkeypatch.setattr(
        check_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: _Engine(error=error),
    )

    result = await check_postgres.check_postgres("postgresql://owner:database-secret@localhost/deerflow_test_1_abc")

    assert result.connected is False
    assert result.healthy is False
    assert "database-secret" not in repr(result) + result.error


def test_check_result_contains_only_safe_connection_metadata() -> None:
    result = check_postgres.PostgresCheckResult(
        host="127.0.0.1",
        port=5432,
        database="deerflow_test_1_abc",
        server_version="PostgreSQL 17.5",
        current_revision="0001_project_saas_baseline",
        head_revision="0001_project_saas_baseline",
        revision_matches=True,
        missing_tables=(),
    )
    fields = result.__dataclass_fields__
    assert not {"username", "password", "url", "automation_status", "reliability_status"} & set(fields)
    assert result.healthy is True
