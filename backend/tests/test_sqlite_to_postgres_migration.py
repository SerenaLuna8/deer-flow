from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import DateTime, UniqueConstraint

from deerflow.persistence.base import Base
from deerflow.persistence.migration_ledger import MigrationLedgerRow
from scripts.migrate_sqlite_to_postgres import (
    DecodedCheckpoint,
    MigrationError,
    MigrationReport,
    TableMigrationReport,
    UnionPlan,
    _business_unique_keys,
    _json_canonical,
    _preflight_cross_source,
    _run_cli,
    _strict_insert_checkpoint,
    backup_source,
    decode_checkpoint_rows,
    inspect_source,
    main,
    migrate_source,
    normalize_business_rows,
)


def test_migrator_has_no_sqlite_provider_dependency() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    scanned = "\n".join(
        path.read_text()
        for path in (
            backend_root / "pyproject.toml",
            backend_root / "packages/harness/pyproject.toml",
            backend_root / "uv.lock",
            backend_root / "scripts/migrate_sqlite_to_postgres.py",
        )
    )
    assert "langgraph-checkpoint-sqlite" not in scanned
    assert "langgraph.checkpoint.sqlite" not in scanned
    assert "aiosqlite" not in scanned


def test_semantic_digest_handles_typed_langchain_values_without_repr() -> None:
    first = _json_canonical(HumanMessage(content="synthetic"))
    second = _json_canonical(HumanMessage(content="synthetic"))
    assert first == second
    assert "HumanMessage" not in first
    assert "synthetic" not in first


def test_cli_redacts_target_url_and_business_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "credential-must-not-render"
    monkeypatch.setenv("TASK5_DATABASE_URL", f"postgresql://owner:{secret}@localhost/deerflow_test_1_abc")

    async def fail(_args: object, _target: str) -> None:
        raise MigrationError(f"synthetic business value and {secret}")

    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres._run_cli", fail)
    result = main(
        [
            "--source",
            str(tmp_path / "synthetic.db"),
            "--target-url-env",
            "TASK5_DATABASE_URL",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert result == 1
    assert secret not in rendered
    assert "synthetic business value" not in rendered
    assert "postgresql://" not in rendered


def _sqlite(path: Path, statements: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(statements)


def _user_source(path: Path, rows: list[tuple[str, str, int]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT,
            system_role TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
            oauth_provider TEXT, oauth_id TEXT, needs_setup BOOLEAN NOT NULL,
            token_version INTEGER NOT NULL)"""
        )
        connection.executemany(
            "INSERT INTO users VALUES (?,?,NULL,'user','2026-07-12T00:00:00+00:00',NULL,NULL,?,0)",
            rows,
        )


def test_migration_ledger_is_registered_with_unique_source_identity() -> None:
    table = Base.metadata.tables["migration_ledger"]
    assert MigrationLedgerRow.__table__ is table
    unique = next(item for item in table.constraints if isinstance(item, UniqueConstraint))
    assert unique.name == "uq_migration_source_row"
    assert tuple(column.name for column in unique.columns) == (
        "source_sha256",
        "source_table",
        "source_key",
    )
    assert isinstance(table.c.migrated_at.type, DateTime)
    assert table.c.migrated_at.type.timezone is True


def test_backup_is_atomic_digest_checked_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _user_source(source, [("u-1", "u1@example.invalid", 0)])
    before = source.read_bytes()

    first = backup_source(source, tmp_path / "backup")
    second = backup_source(source, tmp_path / "backup")

    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.size_bytes == second.size_bytes
    assert first.reused is False
    assert second.reused is True
    assert first.path.read_bytes() == before
    assert first.sha256 == hashlib.sha256(before).hexdigest()
    assert not list((tmp_path / "backup").glob("*.tmp"))
    assert source.read_bytes() == before


def test_source_with_nonempty_wal_or_shm_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "active.db"
    _user_source(source, [])
    source.with_name(f"{source.name}-wal").write_bytes(b"active")
    with pytest.raises(MigrationError, match="active SQLite WAL/SHM"):
        inspect_source(source)


def test_backup_rejects_source_mutated_after_pinned_preflight(tmp_path: Path) -> None:
    source = tmp_path / "mutated.db"
    _user_source(source, [("u-1", "one@example.invalid", 0)])
    pinned = inspect_source(source).inventory
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE users SET email='two@example.invalid' WHERE id='u-1'")
    with pytest.raises(MigrationError, match="fingerprint changed"):
        backup_source(source, tmp_path / "backup", (pinned.sha256, pinned.size_bytes))


def test_inspect_source_rejects_unknown_table_even_when_empty(tmp_path: Path) -> None:
    source = tmp_path / "unknown.db"
    _sqlite(source, "CREATE TABLE mystery (id TEXT PRIMARY KEY);")

    with pytest.raises(MigrationError, match="unknown source table: mystery"):
        inspect_source(source)


def test_inspect_source_allows_only_empty_deferred_project_tables(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    _sqlite(empty, "CREATE TABLE projects (id TEXT PRIMARY KEY);")
    report = inspect_source(empty)
    assert report.deferred_empty == ("projects",)

    nonempty = tmp_path / "nonempty.db"
    _sqlite(nonempty, "CREATE TABLE project_memberships (id TEXT PRIMARY KEY); INSERT INTO project_memberships VALUES ('m-1');")
    with pytest.raises(MigrationError, match="deferred source table is not empty"):
        inspect_source(nonempty)


def test_inspect_source_rejects_unknown_columns_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "columns.db"
    _sqlite(source, "CREATE TABLE users (id TEXT PRIMARY KEY, unexpected TEXT);")
    before_bytes = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns

    with pytest.raises(MigrationError, match="users has unknown columns: unexpected"):
        inspect_source(source)

    assert source.read_bytes() == before_bytes
    assert source.stat().st_mtime_ns == before_mtime


def test_inspect_source_rejects_incomplete_schema_even_when_empty(tmp_path: Path) -> None:
    source = tmp_path / "incomplete-empty.db"
    _sqlite(source, "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL);")
    with pytest.raises(MigrationError, match="unsupported users source schema"):
        inspect_source(source)


def test_ledger_default_timestamp_is_timezone_aware() -> None:
    row = MigrationLedgerRow(
        source_sha256="a" * 64,
        source_table="users",
        source_key='["u-1"]',
        target_table="users",
        target_key='["u-1"]',
        row_digest="b" * 64,
        status="migrated",
        migrated_at=datetime.now(UTC),
    )
    assert row.migrated_at.tzinfo is UTC


def test_business_normalization_decodes_json_bool_utc_and_stable_key(tmp_path: Path) -> None:
    source = tmp_path / "users.db"
    _sqlite(
        source,
        """
        CREATE TABLE threads_meta (
            thread_id TEXT PRIMARY KEY, assistant_id TEXT, user_id TEXT,
            display_name TEXT, status TEXT, metadata_json JSON,
            created_at TIMESTAMP, updated_at TIMESTAMP
        );
        INSERT INTO threads_meta VALUES
            ('t-1', NULL, 'u-1', NULL, 'idle', '{"b":2,"a":1}',
             '2026-07-12T01:02:03', '2026-07-12T01:02:04+00:00');
        """,
    )

    rows = normalize_business_rows(source, "threads_meta")
    repeated = normalize_business_rows(source, "threads_meta")
    assert len(rows) == 1
    assert rows[0].source_key == '["t-1"]'
    assert rows[0].values["metadata_json"] == {"a": 1, "b": 2}
    assert rows[0].values["created_at"].tzinfo is UTC
    assert rows[0].values["updated_at"].tzinfo is not None
    assert len(rows[0].digest) == 64
    assert repeated[0].digest == rows[0].digest


def test_business_normalization_rejects_unapproved_nullable_composite_key(tmp_path: Path) -> None:
    source = tmp_path / "feedback.db"
    _sqlite(
        source,
        """
        CREATE TABLE feedback (
            feedback_id TEXT, run_id TEXT, thread_id TEXT, user_id TEXT,
            message_id TEXT, rating INTEGER, comment TEXT, created_at TIMESTAMP,
            PRIMARY KEY (feedback_id, user_id)
        );
        INSERT INTO feedback VALUES ('f-1','r','t',NULL,NULL,1,NULL,'2026-07-12T00:00:00Z');
        INSERT INTO feedback VALUES ('f-1','r','t',NULL,NULL,1,NULL,'2026-07-12T00:00:00Z');
        """,
    )

    with pytest.raises(MigrationError, match="unsupported feedback source primary key"):
        normalize_business_rows(source, "feedback")


def test_business_normalization_rejects_nonempty_table_without_primary_key(tmp_path: Path) -> None:
    source = tmp_path / "no-pk.db"
    _sqlite(source, "CREATE TABLE users (id TEXT, email TEXT); INSERT INTO users VALUES ('u','e@example.invalid');")
    with pytest.raises(MigrationError, match="unsupported users source schema"):
        normalize_business_rows(source, "users")


def test_business_normalization_rejects_missing_required_and_invalid_typed_values(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    _sqlite(missing, "CREATE TABLE users (id TEXT PRIMARY KEY); INSERT INTO users VALUES ('u');")
    with pytest.raises(MigrationError, match="unsupported users source schema"):
        normalize_business_rows(missing, "users")

    invalid_bool = tmp_path / "bool.db"
    _user_source(invalid_bool, [("u", "e@example.invalid", 2)])
    with pytest.raises(MigrationError, match="invalid boolean"):
        normalize_business_rows(invalid_bool, "users")

    invalid_json = tmp_path / "json.db"
    _sqlite(
        invalid_json,
        "CREATE TABLE threads_meta (thread_id TEXT PRIMARY KEY, assistant_id TEXT, user_id TEXT, "
        "display_name TEXT, status TEXT, metadata_json JSON, created_at TIMESTAMP, updated_at TIMESTAMP); "
        "INSERT INTO threads_meta VALUES ('t',NULL,NULL,NULL,'idle','{broken',"
        "'2026-07-12T00:00:00Z','2026-07-12T00:00:00Z');",
    )
    with pytest.raises(MigrationError, match="invalid JSON"):
        normalize_business_rows(invalid_json, "threads_meta")


def test_checkpoint_decoder_round_trips_typed_checkpoint_and_writes(tmp_path: Path) -> None:
    source = tmp_path / "checkpoint.db"
    serde = JsonPlusSerializer()
    checkpoint = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-07-12T00:00:00+00:00",
        "channel_values": {"messages": [{"kind": "human", "text": "synthetic"}]},
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "updated_channels": ["messages"],
    }
    checkpoint_type, checkpoint_blob = serde.dumps_typed(checkpoint)
    write_type, write_blob = serde.dumps_typed({"result": [1, 2, 3]})
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
                type TEXT, checkpoint BLOB, metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            );
            CREATE TABLE writes (
                thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
                channel TEXT NOT NULL, type TEXT, value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            );
            """
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("t-1", "", "cp-1", None, checkpoint_type, checkpoint_blob, b'{"source":"synthetic"}'),
        )
        connection.execute(
            "INSERT INTO writes VALUES (?,?,?,?,?,?,?,?)",
            ("t-1", "", "cp-1", "task-1", 0, "result", write_type, write_blob),
        )

    checkpoints, writes = decode_checkpoint_rows(source)
    assert checkpoints[0].checkpoint == checkpoint
    assert checkpoints[0].metadata == {"source": "synthetic"}
    assert writes[0].value == {"result": [1, 2, 3]}


def test_checkpoint_decoder_orders_parent_before_lexically_earlier_child(tmp_path: Path) -> None:
    source = tmp_path / "checkpoint-order.db"
    serde = JsonPlusSerializer()
    parent = {"id": "z-parent", "channel_values": {}, "channel_versions": {}}
    child = {"id": "a-child", "channel_values": {}, "channel_versions": {}}
    parent_type, parent_blob = serde.dumps_typed(parent)
    child_type, child_blob = serde.dumps_typed(child)
    with sqlite3.connect(source) as connection:
        connection.execute(
            """CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"""
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("t", "", "a-child", "z-parent", child_type, child_blob, b"{}"),
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("t", "", "z-parent", None, parent_type, parent_blob, b"{}"),
        )

    checkpoints, _writes = decode_checkpoint_rows(source)
    assert [row.checkpoint_id for row in checkpoints] == ["z-parent", "a-child"]


@pytest.mark.asyncio
async def test_strict_checkpoint_blob_collision_compares_and_never_updates() -> None:
    serde = JsonPlusSerializer()
    value = HumanMessage(content="synthetic")
    type_, blob = serde.dumps_typed(value)

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.fetchval_results = iter([None, "cp-1"])
            self.sql: list[str] = []

        def transaction(self):
            return Transaction()

        async def fetchval(self, sql, *_args):
            self.sql.append(sql)
            return next(self.fetchval_results)

        async def fetchrow(self, sql, *_args):
            self.sql.append(sql)
            if "parent_checkpoint_id" in sql:
                return {
                    "parent_checkpoint_id": None,
                    "checkpoint": {"id": "cp-1", "channel_values": {}, "channel_versions": {"messages": "1"}},
                    "metadata": {},
                }
            return {"type": type_, "blob": blob}

    connection = Connection()
    row = DecodedCheckpoint(
        "t",
        "",
        "cp-1",
        None,
        {"id": "cp-1", "channel_values": {"messages": value}, "channel_versions": {"messages": "1"}},
        {},
    )
    assert await _strict_insert_checkpoint(connection, row) is True
    assert all("UPDATE" not in sql.upper() for sql in connection.sql)


@pytest.mark.asyncio
async def test_strict_checkpoint_blob_collision_stops_on_different_semantics() -> None:
    serde = JsonPlusSerializer()
    type_, blob = serde.dumps_typed(HumanMessage(content="different"))

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def transaction(self):
            return Transaction()

        async def fetchval(self, _sql, *_args):
            return None

        async def fetchrow(self, _sql, *_args):
            return {"type": type_, "blob": blob}

    row = DecodedCheckpoint(
        "t",
        "",
        "cp-1",
        None,
        {"id": "cp-1", "channel_values": {"messages": HumanMessage(content="source")}, "channel_versions": {"messages": "1"}},
        {},
    )
    with pytest.raises(MigrationError, match="checkpoint blob conflict"):
        await _strict_insert_checkpoint(Connection(), row)


@pytest.mark.asyncio
async def test_multi_source_cli_preflights_every_source_before_backup_or_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = [tmp_path / "one.db", tmp_path / "two.db"]
    calls: list[tuple[str, bool]] = []

    async def fake_migrate(source: Path, _target: str, dry_run: bool, **_kwargs: object) -> MigrationReport:
        calls.append((source.name, dry_run))
        return MigrationReport(
            source_sha256="a" * 64,
            dry_run=dry_run,
            tables={"users": TableMigrationReport(0, verified=True)},
            deferred_empty=(),
            verified=True,
        )

    backups: list[str] = []
    monkeypatch.setattr(
        "scripts.migrate_sqlite_to_postgres._preflight_cross_source",
        lambda _sources: UnionPlan(
            frozenset(),
            (frozenset(), frozenset()),
            (frozenset(), frozenset()),
        ),
    )
    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres.migrate_source", fake_migrate)
    monkeypatch.setattr(
        "scripts.migrate_sqlite_to_postgres.backup_source",
        lambda source, _directory, _fingerprint=None: backups.append(source.name) or SimpleNamespace(path=source.with_suffix(".bak"), sha256="b" * 64),
    )

    await _run_cli(
        SimpleNamespace(source=sources, dry_run=False, backup_dir=tmp_path / "backups"),
        "postgresql://credential-must-not-render@localhost/deerflow_test_1_abc",
    )

    assert calls == [("one.db", True), ("two.db", True), ("one.bak", False), ("two.bak", False)]
    assert backups == ["one.db", "two.db"]


def test_cross_source_preflight_stops_conflicting_target_primary_key(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _user_source(first, [("u-shared", "one@example.invalid", 0)])
    _user_source(second, [("u-shared", "two@example.invalid", 0)])
    with pytest.raises(MigrationError, match="cross-source target conflict in users"):
        _preflight_cross_source([first, second])


def test_channel_active_identity_partial_unique_ignores_revoked_only() -> None:
    base = {
        "id": "c",
        "owner_user_id": "u",
        "provider": "slack",
        "external_account_id": "a",
        "workspace_id": "w",
    }
    revoked = dict(base, status="revoked")
    connected = dict(base, status="connected")
    assert "uq_channel_connection_active_identity" not in {name for name, _key in _business_unique_keys("channel_connections", revoked)}
    assert "uq_channel_connection_active_identity" in {name for name, _key in _business_unique_keys("channel_connections", connected)}


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_real_postgres_semantic_migration_and_idempotent_replay(
    tmp_path: Path,
    migrated_postgres_database_url: str,
) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    from scripts.setup_postgres import _asyncpg_url

    source = tmp_path / "synthetic.db"
    checkpoint = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-07-12T00:00:00+00:00",
        "channel_values": {"messages": [{"kind": "human", "text": "synthetic"}]},
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "updated_channels": ["messages"],
    }
    serde = JsonPlusSerializer()
    checkpoint_type, checkpoint_blob = serde.dumps_typed(checkpoint)
    write_type, write_blob = serde.dumps_typed({"ok": True})
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT,
                system_role TEXT NOT NULL, created_at TIMESTAMP NOT NULL, oauth_provider TEXT,
                oauth_id TEXT, needs_setup BOOLEAN NOT NULL, token_version INTEGER NOT NULL);
            INSERT INTO users VALUES ('u-synthetic','synthetic@example.invalid',NULL,'user',
                '2026-07-12T00:00:00+00:00',NULL,NULL,0,0);
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
                type TEXT, checkpoint BLOB, metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            );
            CREATE TABLE writes (
                thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL,
                idx INTEGER NOT NULL, channel TEXT NOT NULL, type TEXT, value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            );
            CREATE TABLE store (
                prefix TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                created_at TIMESTAMP, updated_at TIMESTAMP,
                expires_at TIMESTAMP, ttl_minutes REAL,
                PRIMARY KEY (prefix, key)
            );
            CREATE TABLE store_migrations (v INTEGER PRIMARY KEY);
            INSERT INTO store VALUES (
                'synthetic.namespace', 'key', '{"answer":42}',
                '2026-07-12T00:00:00+00:00', '2026-07-12T00:01:00+00:00',
                '2026-07-12T00:30:00+00:00', 30
            );
            """
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            (
                "t-synthetic",
                "",
                "cp-1",
                None,
                checkpoint_type,
                checkpoint_blob,
                b'{"source":"synthetic"}',
            ),
        )
        connection.execute(
            "INSERT INTO writes VALUES (?,?,?,?,?,?,?,?)",
            (
                "t-synthetic",
                "",
                "cp-1",
                "task-synthetic",
                0,
                "result",
                write_type,
                write_blob,
            ),
        )

    dsn = _asyncpg_url(migrated_postgres_database_url)
    async with AsyncPostgresSaver.from_conn_string(dsn) as target_saver:
        await target_saver.setup()
    async with AsyncPostgresStore.from_conn_string(dsn) as target_store:
        await target_store.setup()

    dry_run = await migrate_source(source, migrated_postgres_database_url, dry_run=True)
    assert dry_run.tables["users"].planned_insert == 1
    probe = await asyncpg.connect(dsn)
    try:
        assert await probe.fetchval("SELECT COUNT(*) FROM users") == 0
        assert await probe.fetchval("SELECT COUNT(*) FROM migration_ledger") == 0
    finally:
        await probe.close()

    first = await migrate_source(source, migrated_postgres_database_url, dry_run=False)
    second = await migrate_source(source, migrated_postgres_database_url, dry_run=False)

    assert first.verified is True
    assert first.tables["users"].inserted == 1
    assert first.tables["checkpoints"].inserted == 1
    assert first.tables["writes"].inserted == 1
    assert first.tables["store"].inserted == 1
    assert second.tables["users"].already_migrated == 1
    assert second.tables["checkpoints"].already_migrated == 1
    assert second.tables["writes"].already_migrated == 1
    assert second.tables["store"].already_migrated == 1

    async with AsyncPostgresSaver.from_conn_string(dsn) as target_saver:
        loaded = await target_saver.aget_tuple({"configurable": {"thread_id": "t-synthetic", "checkpoint_ns": "", "checkpoint_id": "cp-1"}})
        assert loaded is not None
        assert loaded.checkpoint == checkpoint
        assert loaded.pending_writes == [("task-synthetic", "result", {"ok": True})]
    async with AsyncPostgresStore.from_conn_string(dsn) as target_store:
        item = await target_store.aget(("synthetic", "namespace"), "key", refresh_ttl=False)
        assert item is not None
        assert item.value == {"answer": 42}

    conflict_source = tmp_path / "conflict.db"
    with sqlite3.connect(conflict_source) as connection:
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT,
                system_role TEXT NOT NULL, created_at TIMESTAMP NOT NULL, oauth_provider TEXT,
                oauth_id TEXT, needs_setup BOOLEAN NOT NULL, token_version INTEGER NOT NULL);
            INSERT INTO users VALUES ('u-new','new@example.invalid',NULL,'user',
                '2026-07-12T00:00:00+00:00',NULL,NULL,0,0);
            INSERT INTO users VALUES ('u-synthetic','conflict@example.invalid',NULL,'user',
                '2026-07-12T00:00:00+00:00',NULL,NULL,0,0);
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('synthetic-conflict');
            """
        )
    with pytest.raises(MigrationError, match="target row conflict"):
        await migrate_source(conflict_source, migrated_postgres_database_url, dry_run=False)
    probe = await asyncpg.connect(dsn)
    try:
        assert await probe.fetchval("SELECT COUNT(*) FROM users WHERE id='u-new'") == 0
        conflict_sha = hashlib.sha256(conflict_source.read_bytes()).hexdigest()
        assert (
            await probe.fetchval(
                "SELECT COUNT(*) FROM migration_ledger WHERE source_sha256=$1",
                conflict_sha,
            )
            == 0
        )
    finally:
        await probe.close()
