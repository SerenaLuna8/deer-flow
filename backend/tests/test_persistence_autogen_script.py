"""PostgreSQL tests for ``scripts/_autogen_revision.py``."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import deerflow.persistence.models  # noqa: F401
from deerflow.persistence.base import Base


@pytest.fixture(scope="module")
def autogen_module():
    """Load ``scripts/_autogen_revision.py`` as an importable module.

    The file lives outside the package tree (under ``backend/scripts/``) so we
    load it directly via ``spec_from_file_location``.
    """
    script_path = Path(__file__).resolve().parents[1] / "scripts/_autogen_revision.py"
    assert script_path.exists(), f"missing autogen script at {script_path}"
    spec = importlib.util.spec_from_file_location("_autogen_revision_under_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_autogen_builder_requires_explicit_postgres_url(autogen_module, monkeypatch) -> None:
    upgrades: list[tuple[str, str]] = []
    monkeypatch.setattr(
        autogen_module.command,
        "upgrade",
        lambda config, revision: upgrades.append((config.get_main_option("sqlalchemy.url"), revision)),
    )

    url = f"postgresql+asyncpg://user:password@localhost/deerflow_test_1_{'a' * 32}"
    assert autogen_module._build_temp_db_at_head(url) == url
    assert upgrades == [(url, "head")]
    with pytest.raises(ValueError, match="PostgreSQL"):
        autogen_module._build_temp_db_at_head("sqlite+aiosqlite:///tmp/autogen.db")
    with pytest.raises(ValueError, match="disposable"):
        autogen_module._build_temp_db_at_head("postgresql+asyncpg://user:password@localhost/deerflow")


def test_autogen_main_never_prints_credentials(autogen_module, monkeypatch, capsys) -> None:
    secret = "do-not-print-this-password"
    disposable_url = f"postgresql+asyncpg://user:{secret}@localhost/deerflow_autogen_1_{'b' * 32}"

    @contextmanager
    def fake_temporary_database(_admin_url):
        yield disposable_url

    monkeypatch.setenv("POSTGRES_ADMIN_URL", f"postgresql+asyncpg://user:{secret}@localhost/postgres")
    monkeypatch.setattr(sys, "argv", ["_autogen_revision.py", "test revision"])
    monkeypatch.setattr(autogen_module, "_temporary_postgres_database", fake_temporary_database)
    monkeypatch.setattr(autogen_module, "_build_temp_db_at_head", lambda url: url)
    monkeypatch.setattr(autogen_module.command, "revision", lambda *_args, **_kwargs: None)

    autogen_module.main()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert disposable_url not in captured.out
    assert disposable_url not in captured.err


def test_temporary_postgres_database_always_drops_generated_database(autogen_module, monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    async def fake_create(_admin_url: str, database: str) -> None:
        events.append(("create", database))

    async def fake_drop(_admin_url: str, database: str) -> None:
        events.append(("drop", database))

    monkeypatch.setattr(autogen_module, "_create_database", fake_create)
    monkeypatch.setattr(autogen_module, "_drop_database", fake_drop)
    admin_url = "postgresql+asyncpg://admin:secret@localhost/postgres"

    with pytest.raises(RuntimeError, match="body failed"):
        with autogen_module._temporary_postgres_database(admin_url) as database_url:
            database = make_url(database_url).database
            assert database is not None
            assert autogen_module._AUTOGEN_DATABASE_PATTERN.fullmatch(database)
            events.append(("yield", database))
            raise RuntimeError("body failed")

    assert [event for event, _database in events] == ["create", "yield", "drop"]
    assert len({database for _event, database in events}) == 1


@pytest.mark.asyncio
async def test_drop_database_terminates_connections_before_drop(autogen_module, monkeypatch) -> None:
    statements: list[str] = []

    class FakeConnection:
        async def execute(self, statement: str, *_args) -> None:
            statements.append(statement)

        async def close(self) -> None:
            statements.append("close")

    async def fake_connect(_dsn: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr(autogen_module.asyncpg, "connect", fake_connect)
    database = f"deerflow_autogen_1_{'c' * 32}"
    await autogen_module._drop_database("postgresql+asyncpg://admin:secret@localhost/postgres", database)

    assert "pg_terminate_backend" in statements[0]
    assert statements[1] == f'DROP DATABASE IF EXISTS "{database}"'
    assert statements[2] == "close"


@pytest.mark.asyncio
async def test_autogen_builds_temp_db_at_head_without_data_dir(autogen_module, monkeypatch, postgres_database_url: str) -> None:
    """The temp-DB builder must succeed even when ``./data/`` does not exist.

    We chdir to an empty directory to mimic a clean checkout where the
    alembic.ini default URL would explode.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    workdir = tempfile.mkdtemp(prefix="deerflow-autogen-test-")
    monkeypatch.chdir(workdir)
    # Sanity: this directory has no ``./data/`` -- so the alembic.ini default
    # URL would fail if used.
    assert not os.path.exists("data")

    url = await asyncio.to_thread(autogen_module._build_temp_db_at_head, postgres_database_url)
    assert make_url(url).get_backend_name() == "postgresql"


@pytest.mark.asyncio
async def test_autogen_temp_db_is_at_head(autogen_module, postgres_database_url: str) -> None:
    """The temp DB the autogen script builds must be at head, so the
    autogenerate diff against current models is empty (or only reflects
    intentional, in-progress model changes)."""
    url = await asyncio.to_thread(autogen_module._build_temp_db_at_head, postgres_database_url)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert revision == "0005_project_foundation"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_autogen_temp_db_comes_from_migration_history_not_current_metadata(autogen_module, postgres_database_url: str) -> None:
    """Pending ORM changes must remain visible to autogenerate.

    If the helper accidentally uses runtime ``bootstrap_schema`` /
    ``Base.metadata.create_all`` again, this probe table would be created in
    the temp DB and the test would fail. A temp DB built from alembic history
    only contains objects that committed revisions know how to create.
    """
    probe_name = "__autogen_probe_pending_migration__"
    probe_table = sa.Table(probe_name, Base.metadata, sa.Column("id", sa.Integer, primary_key=True))
    try:
        url = await asyncio.to_thread(autogen_module._build_temp_db_at_head, postgres_database_url)
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            assert probe_name not in tables, "temp DB was built from current ORM metadata instead of migration history"
        finally:
            await engine.dispose()
    finally:
        Base.metadata.remove(probe_table)
