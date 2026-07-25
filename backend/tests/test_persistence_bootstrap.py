from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from deerflow.persistence import bootstrap
from deerflow.persistence.final_schema_contract import ALEMBIC_INDEXES, FINAL_APP_SEQUENCES

CURRENT_REVISION = "0001_project_saas_baseline"


def _exact_app_only_objects() -> frozenset[str]:
    return frozenset({f"relation:r:{name}" for name in bootstrap._FINAL_APP_TABLES | {"alembic_version"}} | {f"sequence:{name}:{owner}" for name, owner in FINAL_APP_SEQUENCES} | {f"index:{name}:{owner}" for name, owner in ALEMBIC_INDEXES})


def _engine_with_connection(connection: AsyncMock) -> MagicMock:
    engine = MagicMock(spec=AsyncEngine)

    @asynccontextmanager
    async def connect():
        yield connection

    engine.connect.side_effect = connect
    return engine


@pytest.mark.asyncio
async def test_classify_database_accepts_only_truly_empty_schema(monkeypatch) -> None:
    connection = AsyncMock()
    monkeypatch.setattr(bootstrap, "inventory_user_schema_objects", AsyncMock(return_value=frozenset()))

    assert await bootstrap.classify_database(connection) == "empty"
    connection.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_database_accepts_exact_current_schema(monkeypatch) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = CURRENT_REVISION
    monkeypatch.setattr(
        bootstrap,
        "inventory_user_schema_objects",
        AsyncMock(return_value=_exact_app_only_objects()),
    )
    monkeypatch.setattr(bootstrap, "verify_m7_catalog", AsyncMock(return_value=True))

    assert await bootstrap.classify_database(connection) == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "objects,revision",
    [
        ({"relation:r:alembic_version"}, "0015_project_reliability_finalize"),
        ({"relation:r:unknown_table"}, None),
        (
            set(_exact_app_only_objects()) | {"relation:r:unknown_table"},
            CURRENT_REVISION,
        ),
    ],
)
async def test_classify_database_rejects_old_or_unknown_nonempty_schema_before_mutation(
    monkeypatch,
    objects: set[str] | frozenset[str],
    revision: str | None,
) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = revision
    monkeypatch.setattr(
        bootstrap,
        "inventory_user_schema_objects",
        AsyncMock(return_value=frozenset(objects)),
    )

    with pytest.raises(bootstrap.M7RecreateRequired) as captured:
        await bootstrap.classify_database(connection)
    assert captured.value.code == "M7_RECREATE_REQUIRED"


def test_migration_graph_has_single_merged_0001_head() -> None:
    assert bootstrap._get_head_revision() == CURRENT_REVISION


@pytest.mark.asyncio
async def test_explicit_migrate_installs_empty_database_to_head(
    monkeypatch,
) -> None:
    connection = AsyncMock()
    engine = _engine_with_connection(connection)
    config = object()
    classify = AsyncMock(side_effect=["empty", "current"])
    offload = AsyncMock()

    @asynccontextmanager
    async def lock(_engine):
        yield

    monkeypatch.setattr(bootstrap, "_postgres_lock", lock)
    monkeypatch.setattr(bootstrap, "classify_database", classify)
    monkeypatch.setattr(bootstrap, "_get_alembic_config", lambda _engine: config)
    monkeypatch.setattr(bootstrap, "_run_alembic_offload", offload)

    await bootstrap.migrate_schema(engine)

    offload.assert_awaited_once_with(bootstrap._upgrade, config, "head")
    assert classify.await_count == 2


@pytest.mark.asyncio
async def test_explicit_migrate_is_noop_for_current_database(
    monkeypatch,
) -> None:
    connection = AsyncMock()
    engine = _engine_with_connection(connection)
    classify = AsyncMock(side_effect=["current", "current"])
    offload = AsyncMock()

    @asynccontextmanager
    async def lock(_engine):
        yield

    monkeypatch.setattr(bootstrap, "_postgres_lock", lock)
    monkeypatch.setattr(bootstrap, "classify_database", classify)
    monkeypatch.setattr(bootstrap, "_run_alembic_offload", offload)

    await bootstrap.migrate_schema(engine)

    offload.assert_not_awaited()
    assert classify.await_count == 2


@pytest.mark.asyncio
async def test_runtime_validation_rejects_empty_schema_without_running_alembic(
    monkeypatch,
) -> None:
    connection = AsyncMock()
    engine = _engine_with_connection(connection)
    classify = AsyncMock(return_value="empty")
    offload = AsyncMock()

    monkeypatch.setattr(bootstrap, "classify_database", classify)
    monkeypatch.setattr(bootstrap, "_run_alembic_offload", offload)

    with pytest.raises(bootstrap.SchemaSetupRequired) as captured:
        await bootstrap.validate_schema(engine)

    assert captured.value.code == "DATABASE_SETUP_REQUIRED"
    offload.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_requires_an_async_engine() -> None:
    with pytest.raises(TypeError, match="AsyncEngine"):
        await bootstrap.bootstrap_schema(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_postgres_lock_preserves_password_bearing_engine_url(monkeypatch) -> None:
    source_url = make_url("postgresql+asyncpg://release_user:fake%40password@127.0.0.1/release_db")
    captured_urls: list[object] = []

    class FakeConnection:
        async def execute(self, *_args, **_kwargs) -> None:
            return None

        async def scalar(self, statement, *_args, **_kwargs):
            sql = str(statement)
            if "idle_session_timeout" in sql:
                return None
            if "pg_try_advisory_lock" in sql:
                return True
            if "pg_advisory_unlock" in sql:
                return True
            raise AssertionError(sql)

    class FakeLockEngine:
        @asynccontextmanager
        async def connect(self):
            yield FakeConnection()

        async def dispose(self) -> None:
            return None

    def create_lock_engine(url, **_kwargs):
        captured_urls.append(url)
        return FakeLockEngine()

    monkeypatch.setattr(bootstrap, "create_async_engine", create_lock_engine)

    async with bootstrap._postgres_lock(SimpleNamespace(url=source_url)):
        pass

    assert len(captured_urls) == 1
    assert make_url(captured_urls[0]).password == "fake@password"
