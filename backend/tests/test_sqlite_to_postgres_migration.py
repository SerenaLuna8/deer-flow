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
    USER_REFERENCE_ALLOWLIST,
    DecodedCheckpoint,
    DecodedWrite,
    MigrationError,
    MigrationErrorCode,
    MigrationReport,
    NormalizedRow,
    TableMigrationReport,
    UnionPlan,
    UserReconciliationRequest,
    _apply_user_reconciliation,
    _business_unique_keys,
    _decode_store_rows,
    _json_canonical,
    _migrate_business_table,
    _migrate_checkpoints,
    _migrate_writes_rows,
    _order_checkpoints,
    _pending_writes_contains,
    _preflight_cross_source,
    _preflight_foreign_keys,
    _run_cli,
    _strict_insert_checkpoint,
    _verify_store_rows_with_api,
    _verify_writes_with_saver,
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


def test_safe_error_context_enriches_without_overwriting_and_hashes_key() -> None:
    error = MigrationError("conflict", code=MigrationErrorCode.CONFLICT, table="users")
    error.enrich(table="runs", source_sha256="a" * 64, source_key='["private-business-key"]')
    rendered = error.safe_fields()
    assert "code=conflict" in rendered
    assert "table=users" in rendered
    assert "source=aaaaaaaaaaaa" in rendered
    assert "key=" in rendered
    assert "private-business-key" not in rendered


def test_real_normalize_error_reaches_cli_as_safe_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "private-path.db"
    _user_source(source, [("private-user-id", "private@example.invalid", 2)])
    monkeypatch.setenv("SAFE_DATABASE_URL", "postgresql://owner:private-password@localhost/db")

    async def run_real_normalize(args, _target):
        normalize_business_rows(args.source[0], "users")

    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres._run_cli", run_real_normalize)
    result = main(
        [
            "--source",
            str(source),
            "--target-url-env",
            "SAFE_DATABASE_URL",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert result == 1
    assert "code=decode" in rendered
    assert "table=users" in rendered
    assert "source=" in rendered and "key=" in rendered
    for secret in ("private-user-id", "private@example.invalid", "private-path.db", "private-password", "postgresql://"):
        assert secret not in rendered


def _production_cli_failure(tmp_path: Path, monkeypatch, capsys, run_real_path) -> str:
    monkeypatch.setenv("SAFE_DATABASE_URL", "postgresql://owner:private-password@localhost/db")
    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres._run_cli", run_real_path)
    result = main(
        [
            "--source",
            str(tmp_path / "private-path.db"),
            "--target-url-env",
            "SAFE_DATABASE_URL",
            "--backup-dir",
            str(tmp_path / "backup"),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    return captured.out + captured.err


def test_checkpoint_saver_provider_error_reaches_cli_as_safe_row_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    row = DecodedCheckpoint(
        "private-thread",
        "",
        "private-checkpoint",
        None,
        {"id": "private-checkpoint", "channel_values": {}, "channel_versions": {}},
        {},
    )

    class Connection:
        async def fetchrow(self, *_args):
            return None

    class Saver:
        async def aget_tuple(self, *_args):
            raise RuntimeError("private-provider-detail")

    async def run_real_checkpoint(_args, _target):
        await _migrate_checkpoints(Connection(), Saver(), "a" * 64, [row], dry_run=True)

    rendered = _production_cli_failure(tmp_path, monkeypatch, capsys, run_real_checkpoint)
    assert "code=migration" in rendered
    assert "table=checkpoints" in rendered
    assert "source=aaaaaaaaaaaa" in rendered and "key=" in rendered
    for secret in ("private-thread", "private-checkpoint", "private-provider-detail", "private-password", "postgresql://"):
        assert secret not in rendered


def test_write_saver_provider_error_reaches_cli_as_safe_row_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    row = DecodedWrite("private-thread", "", "private-checkpoint", "private-task", 0, "private-channel", {"secret": True})

    class SaverContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aget_tuple(self, *_args):
            raise RuntimeError("private-provider-detail")

    class SaverProvider:
        @classmethod
        def from_conn_string(cls, _target):
            return SaverContext()

    monkeypatch.setattr("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", SaverProvider)

    async def run_real_write(_args, target):
        await _verify_writes_with_saver(target, [row], "b" * 64)

    rendered = _production_cli_failure(tmp_path, monkeypatch, capsys, run_real_write)
    assert "code=conflict" in rendered
    assert "table=writes" in rendered
    assert "source=bbbbbbbbbbbb" in rendered and "key=" in rendered
    for secret in ("private-thread", "private-checkpoint", "private-task", "private-channel", "private-provider-detail", "private-password", "postgresql://"):
        assert secret not in rendered


def test_store_ttl_error_reaches_cli_as_safe_row_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "private-path.db"
    _sqlite(
        source,
        """CREATE TABLE store (
        prefix TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
        expires_at TIMESTAMP, ttl_minutes REAL,
        PRIMARY KEY (prefix, key));
        INSERT INTO store VALUES (
        'private.namespace','private-key','{\"secret\":true}',
        '2026-07-12T00:00:00+00:00','2026-07-12T00:00:00+00:00',NULL,1.5);""",
    )

    async def run_real_store(args, _target):
        _decode_store_rows(args.source[0])

    rendered = _production_cli_failure(tmp_path, monkeypatch, capsys, run_real_store)
    assert "code=decode" in rendered
    assert "table=store" in rendered
    assert "source=" in rendered and "key=" in rendered
    for secret in ("private.namespace", "private-key", "secret", "private-password", "postgresql://"):
        assert secret not in rendered


def test_invalid_checkpoint_semantic_reaches_cli_as_safe_row_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "private-path.db"
    serde = JsonPlusSerializer()
    type_, blob = serde.dumps_typed(["private-business-value"])
    with sqlite3.connect(source) as connection:
        connection.execute(
            """CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
            type TEXT, checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"""
        )
        connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            ("private-thread", "", "private-checkpoint", None, type_, blob, b"{}"),
        )

    async def run_real_decode(args, _target):
        decode_checkpoint_rows(args.source[0])

    rendered = _production_cli_failure(tmp_path, monkeypatch, capsys, run_real_decode)
    assert "code=decode" in rendered
    assert "table=checkpoints" in rendered
    assert "source=" in rendered and "key=" in rendered
    for secret in (
        "private-business-value",
        "private-thread",
        "private-checkpoint",
        "private-path.db",
        "invalid checkpoint semantic value",
        "private-password",
        "postgresql://",
    ):
        assert secret not in rendered


def test_checkpoint_parent_cycle_reaches_cli_as_safe_identity_set_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "private-path.db"
    serde = JsonPlusSerializer()
    first_type, first_blob = serde.dumps_typed({"id": "private-first", "channel_values": {}, "channel_versions": {}})
    second_type, second_blob = serde.dumps_typed({"id": "private-second", "channel_values": {}, "channel_versions": {}})
    with sqlite3.connect(source) as connection:
        connection.execute(
            """CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
            type TEXT, checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"""
        )
        connection.executemany(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
            [
                ("private-thread", "", "private-first", "private-second", first_type, first_blob, b"{}"),
                ("private-thread", "", "private-second", "private-first", second_type, second_blob, b"{}"),
            ],
        )

    async def run_real_decode(args, _target):
        decode_checkpoint_rows(args.source[0])

    rendered = _production_cli_failure(tmp_path, monkeypatch, capsys, run_real_decode)
    assert "code=decode" in rendered
    assert "table=checkpoints" in rendered
    assert "source=" in rendered and "key=" in rendered
    for secret in (
        "private-thread",
        "private-first",
        "private-second",
        "private-path.db",
        "checkpoint parent cycle detected",
        "private-password",
        "postgresql://",
    ):
        assert secret not in rendered


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


def _user_source_with_role(path: Path, user_id: str, email: str, role: str) -> None:
    _user_source(path, [(user_id, email, 0)])
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE users SET system_role=?", (role,))


def _add_scheduled_task(path: Path, task_id: str, user_id: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE scheduled_tasks (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT,
            context_mode TEXT NOT NULL, assistant_id TEXT, title TEXT NOT NULL,
            prompt TEXT NOT NULL, schedule_type TEXT NOT NULL, schedule_spec JSON NOT NULL,
            timezone TEXT NOT NULL, status TEXT NOT NULL, overlap_policy TEXT NOT NULL,
            next_run_at TIMESTAMP, last_run_at TIMESTAMP, last_run_id TEXT,
            last_thread_id TEXT, last_error TEXT, lease_owner TEXT,
            lease_expires_at TIMESTAMP, run_count INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO scheduled_tasks VALUES (?,?,NULL,'fresh_thread_per_run',NULL,?,?,?,?,'Asia/Shanghai','enabled','skip',NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,?,?)",
            (
                task_id,
                user_id,
                "synthetic task",
                "synthetic prompt",
                "cron",
                '{"cron":"0 9 * * *"}',
                "2026-07-12T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ),
        )


def _reconciliation_request(sources: list[Path], expected: int = 1) -> UserReconciliationRequest:
    return UserReconciliationRequest(
        source_sha256=tuple(hashlib.sha256(source.read_bytes()).hexdigest() for source in sources),
        expected_conflicts=expected,
    )


def test_explicit_user_reconciliation_builds_one_ordered_absorption(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _user_source_with_role(first, "canonical-user", "same@example.invalid", "system_admin")
    _user_source_with_role(second, "legacy-admin", "same@example.invalid", "admin")
    _add_scheduled_task(first, "task-first", "canonical-user")
    _add_scheduled_task(second, "task-second", "legacy-admin")

    plan = _preflight_cross_source([first, second], _reconciliation_request([first, second]))

    assert plan.per_source_reconciliations[0].user_id_remap == ()
    assert plan.per_source_reconciliations[1].user_id_remap == (("legacy-admin", "canonical-user"),)
    assert len(plan.per_source_reconciliations[1].absorbed_users) == 1


@pytest.mark.parametrize("failure", ["fingerprint", "count", "canonical-role", "later-role"])
def test_user_reconciliation_gates_fail_closed(tmp_path: Path, failure: str) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _user_source_with_role(
        first,
        "canonical-user",
        "same@example.invalid",
        "user" if failure == "canonical-role" else "system_admin",
    )
    _user_source_with_role(
        second,
        "legacy-admin",
        "same@example.invalid",
        "user" if failure == "later-role" else "admin",
    )
    request = _reconciliation_request([first, second], expected=2 if failure == "count" else 1)
    if failure == "fingerprint":
        request = UserReconciliationRequest(("0" * 64, request.source_sha256[1]), 1)

    with pytest.raises(MigrationError):
        _preflight_cross_source([first, second], request)


def test_user_reconciliation_rejects_ambiguous_later_email(tmp_path: Path) -> None:
    sources = [tmp_path / f"source-{index}.db" for index in range(3)]
    _user_source_with_role(sources[0], "canonical-user", "same@example.invalid", "system_admin")
    _user_source_with_role(sources[1], "legacy-one", "same@example.invalid", "admin")
    _user_source_with_role(sources[2], "legacy-two", "same@example.invalid", "admin")

    with pytest.raises(MigrationError):
        _preflight_cross_source(sources, _reconciliation_request(sources, expected=2))


def test_reconciliation_cli_dry_run_requires_bound_flags_and_never_backs_up(tmp_path: Path, monkeypatch, capsys) -> None:
    sources = [tmp_path / "private-first.db", tmp_path / "private-second.db"]
    _user_source_with_role(sources[0], "canonical-user", "same@example.invalid", "system_admin")
    _user_source_with_role(sources[1], "legacy-admin", "same@example.invalid", "admin")
    request = _reconciliation_request(sources)
    observed = []

    async def fake_migrate(source, _target, dry_run, *, source_reconciliation, **_kwargs):
        observed.append((source, dry_run, source_reconciliation))
        inventory = inspect_source(source).inventory
        return MigrationReport(inventory.sha256, True, {}, (), True, inventory.size_bytes)

    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres.migrate_source", fake_migrate)
    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres.backup_source", lambda *_args: pytest.fail("dry-run backup"))
    monkeypatch.setenv("SAFE_DATABASE_URL", "postgresql://owner:private-password@localhost/deerflow")
    argv = [
        "--source",
        str(sources[0]),
        "--source",
        str(sources[1]),
        "--target-url-env",
        "SAFE_DATABASE_URL",
        "--backup-dir",
        str(tmp_path / "private-backups"),
        "--dry-run",
        "--reconcile-users-by-email",
        "--reconcile-expected-conflicts",
        "1",
    ]
    for fingerprint in request.source_sha256:
        argv.extend(["--reconcile-source-sha256", fingerprint])

    assert main(argv) == 0
    rendered = capsys.readouterr().out
    assert len(observed) == 2
    assert observed[1][2].user_id_remap == (("legacy-admin", "canonical-user"),)
    for secret in (
        "same@example.invalid",
        "canonical-user",
        "legacy-admin",
        "private-first.db",
        "private-second.db",
        "private-password",
        "postgresql://",
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    "extra",
    [
        ["--reconcile-users-by-email", "--reconcile-expected-conflicts", "1"],
        ["--reconcile-expected-conflicts", "1", "--reconcile-source-sha256", "0" * 64],
    ],
)
def test_incomplete_or_non_opted_reconciliation_stops_before_target_connect(tmp_path: Path, monkeypatch, capsys, extra) -> None:
    async def forbidden_connect(*_args, **_kwargs):
        pytest.fail("target connect")

    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres.asyncpg.connect", forbidden_connect)
    monkeypatch.setenv("SAFE_DATABASE_URL", "postgresql://owner:private-password@localhost/deerflow")
    result = main(
        [
            "--source",
            str(tmp_path / "private-source.db"),
            "--target-url-env",
            "SAFE_DATABASE_URL",
            "--backup-dir",
            str(tmp_path / "private-backup"),
            "--dry-run",
            *extra,
        ]
    )
    rendered = capsys.readouterr().err
    assert result == 1
    assert "code=conflict" in rendered
    for secret in ("private-source.db", "private-backup", "private-password", "postgresql://"):
        assert secret not in rendered


def test_fixed_user_reference_allowlist_remaps_owner_but_never_external_bot() -> None:
    assert ("channel_connections", "owner_user_id") in USER_REFERENCE_ALLOWLIST
    assert ("channel_connections", "bot_user_id") not in USER_REFERENCE_ALLOWLIST
    row = NormalizedRow(
        source_key='["connection"]',
        target_key='["connection"]',
        values={"id": "connection", "owner_user_id": "legacy-admin", "bot_user_id": "external-bot"},
        digest="unused",
    )
    decision = SimpleNamespace(user_id_remap=(("legacy-admin", "canonical-user"),), absorbed_users=())

    transformed = _apply_user_reconciliation("channel_connections", [row], decision)

    assert transformed[0].values["owner_user_id"] == "canonical-user"
    assert transformed[0].values["bot_user_id"] == "external-bot"
    assert transformed[0].source_key == row.source_key


@pytest.mark.parametrize("table,column", sorted(USER_REFERENCE_ALLOWLIST))
def test_every_fixed_internal_user_reference_is_remapped(table: str, column: str) -> None:
    primary_key = {
        "threads_meta": "thread_id",
        "runs": "run_id",
        "run_events": "id",
        "feedback": "feedback_id",
        "scheduled_tasks": "id",
        "channel_connections": "id",
        "channel_oauth_states": "state_hash",
        "channel_conversations": "id",
    }[table]
    row = NormalizedRow(
        source_key='["source-row"]',
        target_key='["target-row"]',
        values={primary_key: "target-row", column: "legacy-admin"},
        digest="unused",
    )
    decision = SimpleNamespace(user_id_remap=(("legacy-admin", "canonical-user"),), absorbed_users=())

    transformed = _apply_user_reconciliation(table, [row], decision)

    assert transformed[0].values[column] == "canonical-user"
    assert transformed[0].source_key == row.source_key


def test_reconciliation_preserves_two_scheduled_tasks_with_canonical_user() -> None:
    rows = [
        NormalizedRow('["task-first"]', '["task-first"]', {"id": "task-first", "user_id": "canonical-user"}, "unused"),
        NormalizedRow('["task-second"]', '["task-second"]', {"id": "task-second", "user_id": "legacy-admin"}, "unused"),
    ]
    decision = SimpleNamespace(user_id_remap=(("legacy-admin", "canonical-user"),), absorbed_users=())

    transformed = _apply_user_reconciliation("scheduled_tasks", rows, decision)

    assert [row.values["id"] for row in transformed] == ["task-first", "task-second"]
    assert [row.values["user_id"] for row in transformed] == ["canonical-user", "canonical-user"]


@pytest.mark.asyncio
async def test_absorbed_user_ledger_is_auditable_and_idempotent(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _user_source_with_role(first, "canonical-user", "same@example.invalid", "system_admin")
    _user_source_with_role(second, "legacy-admin", "same@example.invalid", "admin")
    plan = _preflight_cross_source([first, second], _reconciliation_request([first, second]))
    decision = plan.per_source_reconciliations[1]
    canonical = decision.absorbed_users[0]

    class Transaction:
        async def start(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class Connection:
        def __init__(self):
            self.ledger = None

        def transaction(self):
            return Transaction()

        async def fetchrow(self, sql, *_args):
            if "FROM migration_ledger" in sql:
                return self.ledger
            if 'FROM "users"' in sql:
                return dict(canonical.canonical_values)
            raise AssertionError(sql)

        async def execute(self, sql, *args):
            assert "INSERT INTO migration_ledger" in sql
            self.ledger = {
                "target_table": args[4],
                "target_key": args[5],
                "row_digest": args[6],
                "status": args[7],
            }

    connection = Connection()
    source_sha = hashlib.sha256(second.read_bytes()).hexdigest()
    first_report = await _migrate_business_table(
        connection,
        second,
        source_sha,
        "users",
        dry_run=False,
        union_reference_keys=plan.per_source_reference_keys[1],
        reconciliation=decision,
    )
    second_report = await _migrate_business_table(
        connection,
        second,
        source_sha,
        "users",
        dry_run=False,
        union_reference_keys=plan.per_source_reference_keys[1],
        reconciliation=decision,
    )

    assert connection.ledger["status"] == "reconciled"
    assert connection.ledger["target_key"] == canonical.target_key
    assert connection.ledger["row_digest"] == canonical.audit_digest
    assert first_report.adopted == 1
    assert second_report.already_migrated == 1


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
    with pytest.raises(MigrationError, match="unsupported users source schema") as captured:
        inspect_source(source)
    assert captured.value.code == MigrationErrorCode.SCHEMA
    assert captured.value.table == "users"
    assert captured.value.source_sha256_prefix is not None


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
            (("a" * 64, 0), ("a" * 64, 0)),
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

    assert calls == [
        ("one.db", True),
        ("two.db", True),
        ("one.bak", True),
        ("two.bak", True),
        ("one.bak", False),
        ("two.bak", False),
    ]
    assert backups == ["one.db", "two.db"]


@pytest.mark.asyncio
async def test_backup_snapshot_plan_fingerprint_mismatch_stops_before_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.db"
    plans = iter(
        [
            UnionPlan(frozenset(), (frozenset(),), (frozenset(),), (("a" * 64, 1),)),
            UnionPlan(frozenset(), (frozenset(),), (frozenset(),), (("b" * 64, 1),)),
        ]
    )
    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres._preflight_cross_source", lambda _sources: next(plans))

    async def fake_migrate(_source, _target, dry_run, **_kwargs):
        assert dry_run is True
        return MigrationReport("a" * 64, True, {}, (), True, source_size_bytes=1)

    monkeypatch.setattr("scripts.migrate_sqlite_to_postgres.migrate_source", fake_migrate)
    monkeypatch.setattr(
        "scripts.migrate_sqlite_to_postgres.backup_source",
        lambda *_args: SimpleNamespace(path=tmp_path / "snapshot.bak", sha256="a" * 64),
    )
    with pytest.raises(MigrationError, match="backup snapshot plan fingerprint mismatch"):
        await _run_cli(SimpleNamespace(source=[source], dry_run=False, backup_dir=tmp_path), "postgresql://safe")


def test_cross_source_preflight_stops_conflicting_target_primary_key(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _user_source(first, [("u-shared", "one@example.invalid", 0)])
    _user_source(second, [("u-shared", "two@example.invalid", 0)])
    with pytest.raises(MigrationError, match="cross-source target conflict in users"):
        _preflight_cross_source([first, second])


def test_real_two_source_preflight_conflict_reaches_cli_as_safe_fields(tmp_path: Path, monkeypatch, capsys) -> None:
    first = tmp_path / "private-first.db"
    second = tmp_path / "private-second.db"
    _user_source(first, [("private-shared-id", "private-first@example.invalid", 0)])
    _user_source(second, [("private-shared-id", "private-second@example.invalid", 0)])
    second_sha = hashlib.sha256(second.read_bytes()).hexdigest()
    monkeypatch.setenv("SAFE_DATABASE_URL", "postgresql://owner:private-password@localhost/deerflow")

    result = main(
        [
            "--source",
            str(first),
            "--source",
            str(second),
            "--target-url-env",
            "SAFE_DATABASE_URL",
            "--backup-dir",
            str(tmp_path / "private-backup"),
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert result == 1
    assert "code=conflict" in rendered
    assert "table=users" in rendered
    assert f"source={second_sha[:12]}" in rendered
    assert "key=" in rendered
    for secret in (
        "private-shared-id",
        "private-first@example.invalid",
        "private-second@example.invalid",
        "private-first.db",
        "private-second.db",
        "private-backup",
        "private-password",
        "postgresql://",
        "cross-source target conflict",
    ):
        assert secret not in rendered


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


def test_saver_pending_writes_verifier_allows_extra_and_requires_multiplicity() -> None:
    expected = [("task-a", "result", {"ok": True}), ("task-a", "result", {"ok": True})]
    actual = [("earlier-task", "result", 1), *expected]
    assert _pending_writes_contains(actual, expected) is True
    assert _pending_writes_contains(actual[:-1], expected) is False
    assert _pending_writes_contains([("task-a", "result", {"ok": False}), *actual[:1]], expected) is False


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_real_postgres_two_source_user_reconciliation_and_ledger_replay(
    tmp_path: Path,
    migrated_postgres_database_url: str,
) -> None:
    from scripts.setup_postgres import _asyncpg_url

    sources = [tmp_path / "first.db", tmp_path / "second.db"]
    _user_source_with_role(sources[0], "canonical-user", "same@example.invalid", "system_admin")
    _add_scheduled_task(sources[0], "task-first", "canonical-user")
    _user_source_with_role(sources[1], "legacy-admin", "same@example.invalid", "admin")
    _add_scheduled_task(sources[1], "task-second", "legacy-admin")
    request = _reconciliation_request(sources)
    plan = _preflight_cross_source(sources, request)

    for index, source in enumerate(sources):
        await migrate_source(
            source,
            migrated_postgres_database_url,
            dry_run=True,
            union_reference_keys=plan.per_source_reference_keys[index],
            source_reconciliation=plan.per_source_reconciliations[index],
        )
    for _replay in range(2):
        for index, source in enumerate(sources):
            await migrate_source(
                source,
                migrated_postgres_database_url,
                dry_run=False,
                union_reference_keys=plan.per_source_reference_keys[index],
                source_reconciliation=plan.per_source_reconciliations[index],
            )

    connection = await asyncpg.connect(_asyncpg_url(migrated_postgres_database_url))
    try:
        assert await connection.fetchval("SELECT COUNT(*) FROM users") == 1
        assert await connection.fetchval("SELECT system_role FROM users") == "system_admin"
        assert await connection.fetchval("SELECT COUNT(*) FROM scheduled_tasks") == 2
        assert await connection.fetchval("SELECT COUNT(DISTINCT user_id) FROM scheduled_tasks") == 1
        second_sha = hashlib.sha256(sources[1].read_bytes()).hexdigest()
        absorbed_key = plan.per_source_reconciliations[1].absorbed_users[0].source_key
        ledger = await connection.fetchrow(
            "SELECT target_table,target_key,row_digest,status FROM migration_ledger WHERE source_sha256=$1 AND source_table='users' AND source_key=$2",
            second_sha,
            absorbed_key,
        )
        assert ledger is not None
        assert ledger["status"] == "reconciled"
        assert ledger["target_key"] == plan.per_source_reconciliations[1].absorbed_users[0].target_key
        assert (
            await connection.fetchval(
                "SELECT COUNT(*) FROM migration_ledger WHERE source_sha256=$1 AND source_table='scheduled_tasks'",
                second_sha,
            )
            == 1
        )
    finally:
        await connection.close()


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


@pytest.mark.asyncio
async def test_checkpoint_conflict_rolls_back_new_blob_state() -> None:
    class Transaction:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            self.snapshot = (
                dict(self.connection.blobs),
                dict(self.connection.checkpoints),
                list(self.connection.ledger),
            )
            return self

        async def __aexit__(self, exc_type, *_args):
            if exc_type:
                self.connection.blobs, self.connection.checkpoints, self.connection.ledger = self.snapshot
            return False

    class Connection:
        def __init__(self):
            self.blobs = {}
            self.checkpoints = {"cp-1": "existing"}
            self.ledger = ["existing-ledger"]

        def transaction(self):
            return Transaction(self)

        async def fetchval(self, sql, *args):
            if "checkpoint_blobs" in sql:
                self.blobs[(args[2], args[3])] = args[5]
                return args[3]
            return None

        async def fetchrow(self, sql, *_args):
            if "parent_checkpoint_id" in sql:
                return {"parent_checkpoint_id": None, "checkpoint": {"id": "cp-1", "channel_values": {}, "channel_versions": {}}, "metadata": {}}
            raise AssertionError(sql)

    connection = Connection()
    row = DecodedCheckpoint("t", "", "cp-1", None, {"id": "cp-1", "channel_values": {"messages": HumanMessage(content="source")}, "channel_versions": {"messages": "1"}}, {})
    with pytest.raises(MigrationError, match="transactional semantic conflict"):
        await _strict_insert_checkpoint(connection, row)
    assert connection.blobs == {}
    assert connection.checkpoints == {"cp-1": "existing"}
    assert connection.ledger == ["existing-ledger"]


@pytest.mark.asyncio
async def test_planned_checkpoint_write_dry_run_has_no_checkpoint_lookup_or_mutation() -> None:
    class Connection:
        async def fetchval(self, *_args):
            raise AssertionError("checkpoint lookup")

        async def fetchrow(self, *_args):
            return None

        async def execute(self, *_args):
            raise AssertionError("mutation")

    write = DecodedWrite("t", "", "cp", "task", 0, "result", {"ok": True})
    report = await _migrate_writes_rows(Connection(), "a" * 64, [write], dry_run=True, planned_checkpoint_keys=frozenset({("t", "", "cp")}))
    assert report.planned_insert == 1


@pytest.mark.asyncio
async def test_ordered_fk_plan_accepts_earlier_and_rejects_later() -> None:
    target = Base.metadata.tables["channel_credentials"]
    values = {column.name: None for column in target.columns}
    values["connection_id"] = "connection-1"
    row = SimpleNamespace(values=values)

    class Connection:
        def __init__(self):
            self.lookups = 0

        async def fetchval(self, *_args):
            self.lookups += 1
            return None

    key = ("channel_connections", ("id",), _json_canonical(["connection-1"]))
    earlier = Connection()
    await _preflight_foreign_keys(earlier, target, row, frozenset({key}))
    assert earlier.lookups == 0
    with pytest.raises(MigrationError, match="missing foreign key target"):
        await _preflight_foreign_keys(Connection(), target, row, frozenset())


def test_langgraph_source_rejects_bad_checkpoint_primary_key(tmp_path: Path) -> None:
    source = tmp_path / "bad-checkpoint-pk.db"
    _sqlite(source, "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, PRIMARY KEY (thread_id, checkpoint_id));")
    with pytest.raises(MigrationError, match="unsupported checkpoints source primary key"):
        inspect_source(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["green", "missing", "mismatch"])
async def test_store_public_api_semantic_readback(mode: str) -> None:
    row = SimpleNamespace(
        values={"prefix": "synthetic.namespace", "key": "key", "value": {"answer": 42}},
        source_key='["synthetic.namespace","key"]',
    )

    class Store:
        async def aget(self, namespace, key, refresh_ttl=False):
            assert namespace == ("synthetic", "namespace")
            assert key == "key"
            assert refresh_ttl is False
            if mode == "missing":
                return None
            return SimpleNamespace(value={"answer": 42 if mode == "green" else 7})

    if mode == "green":
        await _verify_store_rows_with_api(Store(), [row], "a" * 64)
    else:
        with pytest.raises(MigrationError) as captured:
            await _verify_store_rows_with_api(Store(), [row], "a" * 64)
        assert captured.value.table == "store"
        assert captured.value.source_sha256_prefix == "a" * 12
        assert captured.value.key_hash is not None


@pytest.mark.asyncio
async def test_ordered_two_source_checkpoint_parent_visibility() -> None:
    child = DecodedCheckpoint("t", "", "child", "parent", {"id": "child", "channel_values": {}, "channel_versions": {}}, {})

    class Connection:
        async def fetchrow(self, *_args):
            return None

    class Saver:
        async def aget_tuple(self, *_args):
            return None

    report = await _migrate_checkpoints(
        Connection(),
        Saver(),
        "a" * 64,
        [child],
        dry_run=True,
        planned_checkpoint_keys=frozenset({("t", "", "parent")}),
    )
    assert report.planned_insert == 1
    with pytest.raises(MigrationError, match="parent missing"):
        await _migrate_checkpoints(Connection(), Saver(), "a" * 64, [child], dry_run=True)


def test_duplicate_checkpoint_and_write_identities_fail_closed() -> None:
    checkpoint = DecodedCheckpoint("t", "", "cp", None, {"id": "cp", "channel_values": {}, "channel_versions": {}}, {})
    with pytest.raises(MigrationError, match="duplicate checkpoint source key"):
        _order_checkpoints([checkpoint, checkpoint])


@pytest.mark.asyncio
async def test_duplicate_write_identity_fails_before_target_access() -> None:
    class Connection:
        async def fetchval(self, *_args):
            raise AssertionError("must fail before target access")

    write = DecodedWrite("t", "", "cp", "task", 0, "result", 1)
    with pytest.raises(MigrationError, match="duplicate checkpoint write source identity"):
        await _migrate_writes_rows(Connection(), "a" * 64, [write, write], dry_run=True)
