from __future__ import annotations

import traceback
from collections.abc import Callable

import pytest

from scripts import setup_postgres

EXPECTED_COLUMNS = {
    "checkpoint_blobs": (
        "thread_id",
        "checkpoint_ns",
        "channel",
        "version",
        "type",
        "blob",
    ),
    "checkpoint_migrations": ("v",),
    "checkpoint_writes": (
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "task_id",
        "idx",
        "channel",
        "type",
        "blob",
        "task_path",
    ),
    "checkpoints": (
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "parent_checkpoint_id",
        "type",
        "checkpoint",
        "metadata",
    ),
    "store": (
        "prefix",
        "key",
        "value",
        "created_at",
        "updated_at",
        "expires_at",
        "ttl_minutes",
    ),
    "store_migrations": ("v",),
}


def _catalog_rows(schema_name: str = "public") -> list[tuple[str, str, str]]:
    return [(schema_name, table_name, column_name) for table_name, column_names in EXPECTED_COLUMNS.items() for column_name in column_names]


class _AsyncContext:
    def __init__(self, value, events: list[tuple]) -> None:
        self.value = value
        self.events = events

    async def __aenter__(self):
        self.events.append(("enter",))
        return self.value

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        self.events.append(("exit", exc_type))
        return False


class _Cursor:
    def __init__(
        self,
        rows: list[tuple[str, str, str]],
        *,
        fail_comment: str | None = None,
    ) -> None:
        self.rows = rows
        self.fail_comment = fail_comment
        self.executions: list[tuple[object, object | None]] = []
        self.context_events: list[tuple] = []
        self.last_statement = None

    async def __aenter__(self):
        self.context_events.append(("enter",))
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        self.context_events.append(("exit", exc_type))
        return False

    async def execute(self, statement, params=None) -> None:
        self.last_statement = statement
        self.executions.append((statement, params))
        if statement == setup_postgres._ROOT_TABLE_CATALOG_SQL or statement == setup_postgres._LANGGRAPH_CATALOG_SQL:
            return
        rendered = statement.as_string()
        if self.fail_comment is not None and rendered.startswith("COMMENT ON"):
            raise RuntimeError(self.fail_comment)

    async def fetchall(self):
        if self.last_statement == setup_postgres._ROOT_TABLE_CATALOG_SQL:
            schema_name = self.rows[0][0] if self.rows else "public"
            langgraph_tables = {table_name for _schema_name, table_name, _column_name in self.rows}
            table_names = (setup_postgres._EXPECTED_ROOT_TABLES - setup_postgres._ALLOWED_LANGGRAPH_TABLES) | langgraph_tables
            return [(schema_name, table_name) for table_name in sorted(table_names)]
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        rows: list[tuple[str, str, str]],
        *,
        fail_comment: str | None = None,
    ) -> None:
        self.cursor_value = _Cursor(rows, fail_comment=fail_comment)
        self.connection_events: list[tuple] = []
        self.transaction_events: list[tuple] = []

    async def __aenter__(self):
        self.connection_events.append(("enter",))
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        self.connection_events.append(("exit", exc_type))
        return False

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(None, self.transaction_events)

    def cursor(self) -> _Cursor:
        return self.cursor_value


def _connector(
    rows_factory: Callable[[], list[tuple[str, str, str]]],
    *,
    fail_comment: str | None = None,
):
    class Connector:
        urls: list[str] = []
        connections: list[_Connection] = []

        @classmethod
        async def connect(cls, url: str) -> _Connection:
            cls.urls.append(url)
            connection = _Connection(rows_factory(), fail_comment=fail_comment)
            cls.connections.append(connection)
            return connection

    return Connector


def _ddl(connection: _Connection) -> list[str]:
    return [statement.as_string() for statement, _params in connection.cursor_value.executions if statement != setup_postgres._ROOT_TABLE_CATALOG_SQL and statement != setup_postgres._LANGGRAPH_CATALOG_SQL]


def test_comment_inventory_is_exact_complete_and_chinese() -> None:
    assert setup_postgres._ALLOWED_LANGGRAPH_TABLES == frozenset(EXPECTED_COLUMNS)
    assert setup_postgres._validated_langgraph_comment_columns() == EXPECTED_COLUMNS
    assert sum(map(len, EXPECTED_COLUMNS.values())) == 31

    for item in setup_postgres._LANGGRAPH_COMMENT_INVENTORY:
        assert item.table_comment.strip()
        assert setup_postgres._CHINESE_TEXT_PATTERN.search(item.table_comment)
        for _column_name, comment in item.column_comments:
            assert comment.strip()
            assert setup_postgres._CHINESE_TEXT_PATTERN.search(comment)


@pytest.mark.asyncio
async def test_comments_are_schema_qualified_atomic_and_idempotent(monkeypatch) -> None:
    schema_name = 'tenant"; DROP TABLE store; --'
    connector = _connector(lambda: _catalog_rows(schema_name))
    monkeypatch.setattr(setup_postgres, "AsyncConnection", connector)
    connection_url = "postgresql://owner:private-password@localhost/deerflow"

    await setup_postgres._comment_langgraph_schemas(connection_url)
    await setup_postgres._comment_langgraph_schemas(connection_url)

    assert connector.urls == [connection_url, connection_url]
    assert len(connector.connections) == 2
    first, second = connector.connections
    assert _ddl(first) == _ddl(second)
    assert len(_ddl(first)) == len(EXPECTED_COLUMNS) + 31
    assert _ddl(first)[0].startswith('COMMENT ON TABLE "tenant""; DROP TABLE store; --"."checkpoint_blobs" IS ')
    assert first.cursor_value.executions[0] == (
        setup_postgres._ROOT_TABLE_CATALOG_SQL,
        None,
    )
    assert first.cursor_value.executions[1] == (
        setup_postgres._LANGGRAPH_CATALOG_SQL,
        (sorted(EXPECTED_COLUMNS),),
    )
    assert first.transaction_events == [("enter",), ("exit", None)]
    assert first.connection_events == [("enter",), ("exit", None)]
    assert first.cursor_value.context_events == [("enter",), ("exit", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["missing_table", "unknown_table", "unknown_column"])
async def test_commenting_rejects_catalog_drift_without_writing_ddl(monkeypatch, drift: str) -> None:
    rows = _catalog_rows()
    if drift == "missing_table":
        rows = [row for row in rows if row[1] != "store_migrations"]
    elif drift == "unknown_table":
        rows.append(("public", "unexpected_langgraph_table", "id"))
    else:
        rows.append(("public", "store", "future_column"))
    connector = _connector(lambda: rows)
    monkeypatch.setattr(setup_postgres, "AsyncConnection", connector)

    with pytest.raises(setup_postgres.PostgresSetupError, match="注释清单不一致"):
        await setup_postgres._comment_langgraph_schemas("postgresql://owner:private-password@localhost/deerflow")

    connection = connector.connections[0]
    assert _ddl(connection) == []
    assert connection.transaction_events == [
        ("enter",),
        ("exit", setup_postgres.PostgresSetupError),
    ]


@pytest.mark.asyncio
async def test_comment_failure_rolls_back_and_sanitizes_error(monkeypatch) -> None:
    leaked_error = "postgresql://owner:private-password@localhost/private-db"
    connection_url = "postgresql://owner:private-password@localhost/deerflow"
    connector = _connector(lambda: _catalog_rows(), fail_comment=leaked_error)
    monkeypatch.setattr(setup_postgres, "AsyncConnection", connector)

    with pytest.raises(setup_postgres.PostgresSetupError) as captured:
        await setup_postgres._comment_langgraph_schemas(connection_url)

    rendered = "".join(traceback.format_exception(captured.value))
    assert "private-password" not in rendered
    assert "postgresql://" not in rendered
    assert str(captured.value) == ("LangGraph PostgreSQL 表和字段注释写入失败；请检查 DATABASE_URL、目标 role 权限和数据库状态")
    connection = connector.connections[0]
    assert connection.transaction_events == [("enter",), ("exit", RuntimeError)]


@pytest.mark.asyncio
async def test_langgraph_setup_comments_only_after_saver_and_store(monkeypatch) -> None:
    calls: list[str] = []

    class Context:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aenter__(self):
            calls.append(f"{self.name}:enter")
            return self

        async def setup(self) -> None:
            calls.append(f"{self.name}:setup")

        async def __aexit__(self, *_args) -> None:
            calls.append(f"{self.name}:exit")

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name

        def from_conn_string(self, _connection_url: str) -> Context:
            calls.append(f"{self.name}:factory")
            return Context(self.name)

    async def comment(_connection_url: str) -> None:
        calls.append("comments")

    monkeypatch.setattr(setup_postgres, "AsyncPostgresSaver", Provider("saver"))
    monkeypatch.setattr(setup_postgres, "AsyncPostgresStore", Provider("store"))
    monkeypatch.setattr(setup_postgres, "_comment_langgraph_schemas", comment)

    await setup_postgres._bootstrap_langgraph_schemas("postgresql://owner:private-password@localhost/deerflow")

    assert calls == [
        "saver:factory",
        "saver:enter",
        "saver:setup",
        "saver:exit",
        "store:factory",
        "store:enter",
        "store:setup",
        "store:exit",
        "comments",
    ]
