from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import check_postgres


def test_task5_health_check_requires_migration_ledger() -> None:
    assert "migration_ledger" in check_postgres.REQUIRED_TABLES


def _connection(*, revision: str | None = "0003_scheduled_tasks", present_tables=None):
    connection = AsyncMock()
    connection.fetchval.side_effect = ["PostgreSQL 17.5", revision]
    connection.fetch.return_value = [{"table_name": table} for table in (present_tables or check_postgres.REQUIRED_TABLES)]
    return connection


@pytest.mark.asyncio
async def test_check_is_read_only_parameterized_and_healthy(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(check_postgres.asyncpg, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(check_postgres, "get_head_revision", lambda: "0003_scheduled_tasks")

    result = await check_postgres.check_postgres("postgresql://owner:p%40ss@127.0.0.1:5432/deerflow_test_1_abc")

    assert result.healthy is True
    assert result.revision_matches is True
    assert result.missing_tables == ()
    connection.fetch.assert_awaited_once()
    sql, tables = connection.fetch.await_args.args
    assert "information_schema.tables" in sql
    assert "$1::text[]" in sql
    assert set(tables) == set(check_postgres.REQUIRED_TABLES)
    assert all(not call.args[0].lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for call in connection.method_calls if call.args)


@pytest.mark.asyncio
async def test_check_distinguishes_missing_revision_and_tables(monkeypatch) -> None:
    connection = _connection(revision=None, present_tables={"users"})
    missing_relation = RuntimeError("alembic_version missing")
    missing_relation.sqlstate = "42P01"
    connection.fetchval.side_effect = ["PostgreSQL 17.5", missing_relation]
    monkeypatch.setattr(check_postgres.asyncpg, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(check_postgres, "get_head_revision", lambda: "0003_scheduled_tasks")

    result = await check_postgres.check_postgres("postgresql://owner:secret@localhost/deerflow_test_1_abc")

    assert result.connected is True
    assert result.current_revision is None
    assert result.revision_matches is False
    assert "users" not in result.missing_tables
    assert result.missing_tables
    assert result.healthy is False


@pytest.mark.asyncio
async def test_check_connection_failure_is_sanitized(monkeypatch) -> None:
    connect = AsyncMock(side_effect=RuntimeError("cannot connect postgresql://owner:database-secret@localhost/deerflow_test_1_abc"))
    monkeypatch.setattr(check_postgres.asyncpg, "connect", connect)

    result = await check_postgres.check_postgres("postgresql://owner:database-secret@localhost/deerflow_test_1_abc")

    assert result.connected is False
    assert result.healthy is False
    rendered = repr(result) + result.error
    assert "database-secret" not in rendered
    assert "postgresql://" not in rendered


def test_check_result_contains_only_safe_connection_metadata() -> None:
    result = check_postgres.PostgresCheckResult(
        host="127.0.0.1",
        port=5432,
        database="deerflow_test_1_abc",
        server_version="PostgreSQL 17.5",
        current_revision="0003_scheduled_tasks",
        head_revision="0003_scheduled_tasks",
        revision_matches=True,
        missing_tables=(),
    )
    fields = result.__dataclass_fields__
    assert "username" not in fields
    assert "password" not in fields
    assert "url" not in fields
    assert result.healthy is True


def test_check_cli_returns_nonzero_for_unhealthy_result(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:cli-secret@localhost/deerflow_test_1_abc")
    unhealthy = check_postgres.PostgresCheckResult(
        host="localhost",
        port=5432,
        database="deerflow_test_1_abc",
        connected=False,
        error="无法连接 PostgreSQL 数据库",
    )
    monkeypatch.setattr(check_postgres, "run_check", lambda _url: unhealthy)

    assert check_postgres.main([]) != 0
    rendered = capsys.readouterr().out
    assert "不健康" in rendered
    assert "cli-secret" not in rendered
    assert "postgresql://" not in rendered
