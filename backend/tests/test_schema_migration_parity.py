"""首个正式数据库基线契约：单 head、fresh catalog、未来升级闸门。

空库新装走 ``full_schema.sql`` 并直接 stamp ``initial_schema``。所有发布前
marker 都不是正式祖先，必须重建；未来正式 revision 才能接入显式升级链。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy
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
    assert root_to_head == KNOWN_CHAIN_REVISIONS == ("initial_schema",)
    assert script.get_heads() == [CURRENT_SCHEMA_REVISION]


def test_setup_and_upgrade_share_the_schema_mutation_advisory_lock() -> None:
    assert upgrade_module._UPGRADE_LOCK_KEY == bootstrap_module.SCHEMA_MUTATION_LOCK_KEY


def test_initial_chain_root_is_a_noop_and_fresh_schema_stamps_the_head() -> None:
    script = _script_directory()
    root = script.get_revision(KNOWN_CHAIN_REVISIONS[0])
    assert root.down_revision is None
    payload = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert payload.count(f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');") == 1
    assert CURRENT_SCHEMA_REVISION == "initial_schema"


def test_full_schema_is_the_only_install_snapshot() -> None:
    payload = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert payload.startswith("BEGIN;\n")
    assert payload.count(f"INSERT INTO alembic_version (version_num) VALUES ('{CURRENT_SCHEMA_REVISION}');") == 1
    assert not list((MIGRATIONS_PATH / "baseline").glob("*.sql"))
    assert sorted(path.name for path in (MIGRATIONS_PATH / "versions").glob("*.py")) == ["initial_schema.py"]


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
async def test_fresh_install_matches_frozen_catalog_and_detects_drift(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
    finally:
        await engine.dispose()
    state, signature = await _catalog_signature(postgres_database_url)
    assert state == "current"
    assert signature == FINAL_M7_CATALOG_SIGNATURE

    drift_engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        async with drift_engine.begin() as connection:
            await connection.execute(text("ALTER TABLE users ADD COLUMN migration_drift_drill text"))
        async with drift_engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await drift_engine.dispose()


def _pretend_head_is(monkeypatch: pytest.MonkeyPatch, fake_head: str) -> None:
    """Simulate a future released head one step past the initial baseline."""
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
        _pretend_head_is(monkeypatch, "future_schema")

        async with engine.connect() as connection:
            assert await classify_database(connection) == "behind"

        with pytest.raises(SchemaUpgradeRequired) as validate_error:
            await validate_schema(engine)
        assert "make upgrade-db" in str(validate_error.value)
        assert "future_schema" in str(validate_error.value)

        with pytest.raises(SchemaUpgradeRequired):
            await bootstrap_schema(engine)

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
@pytest.mark.parametrize("provisional_marker", ["full_schema", "execution_approvals"])
async def test_pre_release_markers_are_not_supported_upgrade_ancestors(
    postgres_database_url: str,
    provisional_marker: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :marker"),
                {"marker": provisional_marker},
            )
        async with engine.connect() as connection:
            with pytest.raises(M7RecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()

    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert "显式重建" in str(error.value)


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

    fake_head = "future_schema"
    _pretend_head_is(monkeypatch, fake_head)

    applied_urls: list[str] = []

    def _drill_upgrade(url: str) -> None:
        applied_urls.append(url)
        _stamp_marker_sync(url, fake_head)

    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", _drill_upgrade)

    result = await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert applied_urls == [postgres_database_url]
    assert result.applied is True
    assert result.from_revision == CURRENT_SCHEMA_REVISION
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

    _pretend_head_is(monkeypatch, "future_schema")
    monkeypatch.setattr(upgrade_module, "_run_alembic_upgrade_sync", lambda url: None)

    with pytest.raises(PostgresUpgradeError) as error:
        await upgrade_postgres(postgres_database_url, assume_yes=True)
    assert "升级后校验失败" in str(error.value)
