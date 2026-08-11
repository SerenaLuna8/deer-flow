"""U1 迁移链契约：链 pinning、基线冻结、full_schema 与 基线+链 的 catalog 等价。

CI 等价契约（D1）：空库新装走 ``full_schema.sql``，存量库升级走 冻结基线快照 +
``alembic upgrade head``；两条路径产出的 catalog signature 必须完全相等，谁漂移
谁红灯。该文件同时把 ``KNOWN_CHAIN_REVISIONS`` 钉死到 ``backend/migrations``
下的真实脚本，防止链与代码常量各说各话。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy
from postgres_utils import temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import deerflow.persistence.bootstrap as bootstrap_module
from deerflow.persistence.bootstrap import (
    CURRENT_SCHEMA_REVISION,
    KNOWN_CHAIN_REVISIONS,
    M7RecreateRequired,
    SchemaUpgradeRequired,
    bootstrap_schema,
    classify_database,
    validate_schema,
)
from deerflow.persistence.final_schema_contract import (
    FINAL_M7_CATALOG_SIGNATURE,
    read_m7_catalog_signature,
)
from scripts import upgrade_postgres as upgrade_module
from scripts.upgrade_postgres import PostgresUpgradeError, upgrade_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = BACKEND_ROOT / "migrations"
BASELINE_SNAPSHOT_PATH = MIGRATIONS_PATH / "baseline" / "full_schema_v5.sql"
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"

pytestmark = pytest.mark.postgres


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    return ScriptDirectory.from_config(config)


def test_known_chain_revisions_pin_the_actual_migration_scripts() -> None:
    script = _script_directory()
    walked = tuple(revision.revision for revision in script.walk_revisions("base", "heads"))
    root_to_head = tuple(reversed(walked))
    assert root_to_head == KNOWN_CHAIN_REVISIONS
    assert script.get_heads() == [CURRENT_SCHEMA_REVISION]


def test_setup_and_upgrade_share_the_schema_mutation_advisory_lock() -> None:
    assert upgrade_module._UPGRADE_LOCK_KEY == bootstrap_module.SCHEMA_MUTATION_LOCK_KEY


def test_chain_root_is_a_noop_stamped_by_full_schema() -> None:
    script = _script_directory()
    root = script.get_revision(KNOWN_CHAIN_REVISIONS[0])
    assert root.down_revision is None
    # A fresh snapshot stamps the chain head directly. The root marker appears
    # only while root and head are the same revision.
    payload = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert payload.count(f"INSERT INTO alembic_version (version_num) VALUES ('{KNOWN_CHAIN_REVISIONS[0]}');") == (1 if CURRENT_SCHEMA_REVISION == KNOWN_CHAIN_REVISIONS[0] else 0)


def _baseline_sql() -> str:
    lines = BASELINE_SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("--"):
            return "".join(lines[index:])
    raise AssertionError("baseline snapshot contains no SQL body")


def test_baseline_snapshot_is_frozen_at_the_chain_root() -> None:
    body = _baseline_sql()
    assert body.startswith("BEGIN;\n")
    assert body.count(f"INSERT INTO alembic_version (version_num) VALUES ('{KNOWN_CHAIN_REVISIONS[0]}');") == 1
    if CURRENT_SCHEMA_REVISION == KNOWN_CHAIN_REVISIONS[0]:
        # While the head is still the root, the frozen snapshot must remain a
        # byte-copy of full_schema.sql; after the first real migration the two
        # diverge on purpose and the parity test below owns the equivalence.
        assert body == FULL_SCHEMA_PATH.read_text(encoding="utf-8")


async def _execute_sql_batch(database_url: str, payload: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute(payload)
    finally:
        await engine.dispose()


async def _catalog_signature(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            state = await classify_database(connection)
            signature = await read_m7_catalog_signature(connection)
        return state, signature
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_schema_and_baseline_plus_chain_produce_identical_catalogs(
    postgres_admin_url: str,
    postgres_database_url: str,
) -> None:
    # Path A — fresh install: full_schema.sql through the production installer.
    fresh_engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(fresh_engine)
    finally:
        await fresh_engine.dispose()
    fresh_state, fresh_signature = await _catalog_signature(postgres_database_url)
    assert fresh_state == "current"
    assert fresh_signature == FINAL_M7_CATALOG_SIGNATURE

    # Path B — existing install: frozen chain-root snapshot through the public
    # production upgrade entry (lock + Alembic + post-upgrade verification).
    async with temporary_postgres_database(postgres_admin_url) as upgraded_url:
        await _execute_sql_batch(upgraded_url, _baseline_sql())
        result = await upgrade_postgres(upgraded_url, assume_yes=True)
        assert result.applied is True
        assert result.from_revision == KNOWN_CHAIN_REVISIONS[0]
        assert result.to_revision == CURRENT_SCHEMA_REVISION
        upgraded_state, upgraded_signature = await _catalog_signature(upgraded_url)
        assert upgraded_state == "current"
        assert upgraded_signature == fresh_signature

        # Drift drill: any out-of-band DDL must trip the fail-closed gate.
        drift_engine = create_async_engine(upgraded_url, poolclass=NullPool)
        try:
            async with drift_engine.begin() as connection:
                await connection.execute(text("ALTER TABLE users ADD COLUMN migration_drift_drill text"))
            async with drift_engine.connect() as connection:
                with pytest.raises(M7RecreateRequired):
                    await classify_database(connection)
        finally:
            await drift_engine.dispose()


def _pretend_head_is(monkeypatch: pytest.MonkeyPatch, fake_head: str) -> None:
    """Simulate a released head one step past the real chain."""
    chain = (*KNOWN_CHAIN_REVISIONS, fake_head)
    monkeypatch.setattr(bootstrap_module, "KNOWN_CHAIN_REVISIONS", chain)
    monkeypatch.setattr(bootstrap_module, "CURRENT_SCHEMA_REVISION", fake_head)
    monkeypatch.setattr(upgrade_module, "CURRENT_SCHEMA_REVISION", fake_head)


@pytest.mark.asyncio
async def test_behind_database_is_recognized_and_gated_fail_closed(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        _pretend_head_is(monkeypatch, "full_schema_v12_drill")

        async with engine.connect() as connection:
            assert await classify_database(connection) == "behind"

        with pytest.raises(SchemaUpgradeRequired) as validate_error:
            await validate_schema(engine)
        assert "make upgrade-db" in str(validate_error.value)
        assert "full_schema_v12" in str(validate_error.value)

        # Setup never migrates a behind database (D3).
        with pytest.raises(SchemaUpgradeRequired):
            await bootstrap_schema(engine)

        # Unknown markers stay fail-closed exactly as before.
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num = 'mystery_marker'"))
        async with engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_runner_is_a_noop_on_a_current_database(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    result = await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert result.applied is False
    assert result.from_revision == CURRENT_SCHEMA_REVISION
    assert result.to_revision == CURRENT_SCHEMA_REVISION


@pytest.mark.asyncio
async def test_upgrade_runner_refuses_an_empty_database(
    postgres_database_url: str,
) -> None:
    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert "setup-db" in str(error.value)


def _stamp_marker_sync(database_url: str, marker: str) -> None:
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    engine = sqlalchemy.create_engine(sync_url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :marker"),
                {"marker": marker},
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_runner_upgrades_a_behind_database_and_verifies_the_result(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    fake_head = "full_schema_v12_drill"
    _pretend_head_is(monkeypatch, fake_head)

    applied_urls: list[str] = []

    def _drill_upgrade(url: str) -> None:
        applied_urls.append(url)
        _stamp_marker_sync(url, fake_head)

    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", _drill_upgrade)

    result = await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert applied_urls == [postgres_database_url]
    assert result.applied is True
    assert result.from_revision == "full_schema_v12"
    assert result.to_revision == fake_head

    _, signature = await _catalog_signature(postgres_database_url)
    assert signature == FINAL_M7_CATALOG_SIGNATURE


@pytest.mark.asyncio
async def test_upgrade_runner_fails_closed_when_the_migrated_catalog_does_not_verify(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()

    _pretend_head_is(monkeypatch, "full_schema_v12_drill")
    # A migration that "succeeds" without producing the head catalog must fail
    # the post-upgrade verification and instruct the operator to restore.
    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", lambda url: None)

    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert "升级后校验失败" in str(error.value)
