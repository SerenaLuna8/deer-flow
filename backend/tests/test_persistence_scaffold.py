"""Tests for the persistence layer scaffolding.

Tests:
1. DatabaseConfig property derivation (paths, URLs)
2. MemoryRunStore CRUD + user_id filtering
3. Base.to_dict() via inspect mixin
4. Engine init/close lifecycle (memory + SQLite)
5. Postgres missing-dep error message
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError
from support.memory_run_store import MemoryRunStore

from deerflow.config.database_config import DatabaseConfig


def test_database_config_has_no_runtime_compatibility_aliases() -> None:
    config = DatabaseConfig(url="postgresql://localhost/test")
    for name in ("backend", "postgres_url", "app_sqlalchemy_url", "echo_sql"):
        assert not hasattr(config, name)


def test_database_config_repr_redacts_url_credentials() -> None:
    config = DatabaseConfig(url="postgresql://user:secret@localhost/test")
    assert "secret" not in repr(config)
    assert "postgresql://" not in repr(config)


@pytest.mark.asyncio
async def test_init_engine_only_accepts_database_config() -> None:
    from deerflow.persistence.engine import init_engine

    with pytest.raises(TypeError):
        await init_engine("sqlite")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await init_engine("memory", backend="memory")  # type: ignore[call-arg]


def test_session_factory_requires_initialization(monkeypatch) -> None:
    import deerflow.persistence.engine as engine_module

    monkeypatch.setattr(engine_module, "_session_factory", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        engine_module.get_session_factory()


@pytest.mark.asyncio
async def test_init_engine_uses_postgres_pool_and_statement_timeout() -> None:
    from deerflow.persistence.engine import close_engine, init_engine

    connection = SimpleNamespace(execute=AsyncMock())
    connect_cm = AsyncMock()
    connect_cm.__aenter__.return_value = connection
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    engine.dispose = AsyncMock()
    config = DatabaseConfig(
        url="postgresql://user:secret@localhost/test",
        pool_size=7,
        max_overflow=4,
        pool_timeout_seconds=12,
        statement_timeout_seconds=9,
    )
    with patch("deerflow.persistence.engine.create_async_engine", return_value=engine) as create, patch("deerflow.persistence.bootstrap.validate_schema", new=AsyncMock()):
        await init_engine(config)
    kwargs = create.call_args.kwargs
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_timeout"] == 12
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"]["server_settings"]["statement_timeout"] == "9000"
    connection.execute.assert_awaited_once()
    await close_engine()


@pytest.mark.asyncio
async def test_init_engine_does_not_create_missing_database() -> None:
    from deerflow.persistence.engine import init_engine

    connect_cm = AsyncMock()
    connect_cm.__aenter__.side_effect = RuntimeError("database does not exist")
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    engine.dispose = AsyncMock()
    config = DatabaseConfig(url="postgresql://user:secret@localhost/missing")
    with patch("deerflow.persistence.engine.create_async_engine", return_value=engine) as create:
        with pytest.raises(RuntimeError, match="create the target database") as exc_info:
            await init_engine(config)
    assert "secret" not in str(exc_info.value)
    assert create.call_count == 1


# -- DatabaseConfig --


class TestDatabaseConfig:
    def test_removed_backend_selector_is_rejected(self):
        with pytest.raises(ValidationError, match="backend"):
            DatabaseConfig(
                url="postgresql://localhost/deerflow",
                backend="memory",
            )

    def test_url_defaults_from_environment(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db:5432/deerflow")
        c = DatabaseConfig()
        assert c.url == "postgresql://user:pass@db:5432/deerflow"
        assert c.pool_size == 5

    def test_missing_url_and_environment_raises(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError, match="url"):
            DatabaseConfig()

    @pytest.mark.parametrize("url", ["", "sqlite+aiosqlite:///deerflow.db", "memory://", "mysql://localhost/deerflow"])
    def test_non_postgres_urls_are_rejected(self, url):
        with pytest.raises(ValidationError, match="PostgreSQL"):
            DatabaseConfig(url=url)

    def test_postgres_url_derives_driver_specific_urls(self):
        c = DatabaseConfig(url="postgresql://u:p@h:5432/db")
        assert c.sqlalchemy_url == "postgresql+asyncpg://u:p@h:5432/db"
        assert c.checkpointer_url == "postgresql://u:p@h:5432/db"

    def test_asyncpg_url_is_not_double_prefixed(self):
        c = DatabaseConfig(url="postgresql+asyncpg://u:p@h:5432/db")
        assert c.sqlalchemy_url == "postgresql+asyncpg://u:p@h:5432/db"
        assert c.checkpointer_url == "postgresql://u:p@h:5432/db"

    def test_derived_urls_preserve_encoded_credentials_and_query(self):
        url = "postgresql://user:p%40ss%2Fword@db.example:5433/deerflow?sslmode=require&application_name=deer%20flow"
        c = DatabaseConfig(url=url)
        assert c.sqlalchemy_url == url.replace("postgresql://", "postgresql+asyncpg://", 1)
        assert c.checkpointer_url == url

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("pool_size", 0),
            ("max_overflow", -1),
            ("pool_timeout_seconds", 0),
            ("statement_timeout_seconds", 0),
        ],
    )
    def test_pool_settings_enforce_lower_bounds(self, field, value):
        with pytest.raises(ValidationError):
            DatabaseConfig(url="postgresql://localhost/deerflow", **{field: value})

    def test_example_config_uses_postgres_only_contract(self):
        config_example = Path(__file__).resolve().parents[2] / "config.example.yaml"
        config = yaml.safe_load(config_example.read_text(encoding="utf-8"))

        assert config["database"] == {
            "url": "$DATABASE_URL",
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout_seconds": 30,
            "statement_timeout_seconds": 30,
        }


# -- MemoryRunStore --


class TestMemoryRunStore:
    @pytest.fixture
    def store(self):
        return MemoryRunStore()

    @pytest.mark.anyio
    async def test_put_and_get(self, store):
        await store.put("r1", thread_id="t1", status="pending")
        row = await store.get("r1")
        assert row is not None
        assert row["run_id"] == "r1"
        assert row["status"] == "pending"

    @pytest.mark.anyio
    async def test_get_missing_returns_none(self, store):
        assert await store.get("nope") is None

    @pytest.mark.anyio
    async def test_update_status(self, store):
        await store.put("r1", thread_id="t1")
        await store.update_status("r1", "running")
        assert (await store.get("r1"))["status"] == "running"

    @pytest.mark.anyio
    async def test_update_status_with_error(self, store):
        await store.put("r1", thread_id="t1")
        await store.update_status("r1", "error", error="boom")
        row = await store.get("r1")
        assert row["status"] == "error"
        assert row["error"] == "boom"

    @pytest.mark.anyio
    async def test_list_by_thread(self, store):
        await store.put("r1", thread_id="t1")
        await store.put("r2", thread_id="t1")
        await store.put("r3", thread_id="t2")
        rows = await store.list_by_thread("t1")
        assert len(rows) == 2
        assert all(r["thread_id"] == "t1" for r in rows)

    @pytest.mark.anyio
    async def test_list_by_thread_owner_filter(self, store):
        await store.put("r1", thread_id="t1", user_id="alice")
        await store.put("r2", thread_id="t1", user_id="bob")
        rows = await store.list_by_thread("t1", user_id="alice")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"

    @pytest.mark.anyio
    async def test_owner_none_returns_all(self, store):
        await store.put("r1", thread_id="t1", user_id="alice")
        await store.put("r2", thread_id="t1", user_id="bob")
        rows = await store.list_by_thread("t1", user_id=None)
        assert len(rows) == 2

    @pytest.mark.anyio
    async def test_delete(self, store):
        await store.put("r1", thread_id="t1")
        await store.delete("r1")
        assert await store.get("r1") is None

    @pytest.mark.anyio
    async def test_delete_nonexistent_is_noop(self, store):
        await store.delete("nope")  # should not raise

    @pytest.mark.anyio
    async def test_list_by_thread_unknown_thread_is_empty(self, store):
        await store.put("r1", thread_id="t1")
        assert await store.list_by_thread("missing") == []

    @pytest.mark.anyio
    async def test_list_by_thread_newest_first(self, store):
        await store.put("r1", thread_id="t1", created_at="2024-01-01T00:00:00+00:00")
        await store.put("r2", thread_id="t1", created_at="2024-01-03T00:00:00+00:00")
        await store.put("r3", thread_id="t1", created_at="2024-01-02T00:00:00+00:00")
        rows = await store.list_by_thread("t1")
        assert [r["run_id"] for r in rows] == ["r2", "r3", "r1"]

    @pytest.mark.anyio
    async def test_list_by_thread_respects_limit(self, store):
        for i in range(5):
            await store.put(f"r{i}", thread_id="t1", created_at=f"2024-01-0{i + 1}T00:00:00+00:00")
        rows = await store.list_by_thread("t1", limit=2)
        assert [r["run_id"] for r in rows] == ["r4", "r3"]

    @pytest.mark.anyio
    async def test_delete_keeps_thread_index_consistent(self, store):
        await store.put("r1", thread_id="t1")
        await store.put("r2", thread_id="t1")
        await store.delete("r1")
        rows = await store.list_by_thread("t1")
        assert [r["run_id"] for r in rows] == ["r2"]
        # deleting the last run in a thread drops the now-empty index bucket
        await store.delete("r2")
        assert await store.list_by_thread("t1") == []
        assert "t1" not in store._runs_by_thread

    @pytest.mark.anyio
    async def test_aggregate_tokens_by_thread_scopes_to_thread(self, store):
        await store.put("r1", thread_id="t1")
        await store.update_run_completion("r1", status="success", model_name="m-a", total_tokens=100)
        await store.put("r2", thread_id="t1")
        await store.update_run_completion("r2", status="error", model_name="m-a", total_tokens=20)
        await store.put("r3", thread_id="t2")
        await store.update_run_completion("r3", status="success", model_name="m-b", total_tokens=999)

        agg = await store.aggregate_tokens_by_thread("t1")
        assert agg["total_tokens"] == 120  # the other thread's run is excluded
        assert agg["total_runs"] == 2
        assert agg["by_model"]["m-a"] == {"tokens": 120, "runs": 2}
        assert "m-b" not in agg["by_model"]

    @pytest.mark.anyio
    async def test_aggregate_tokens_by_thread_excludes_active_unless_requested(self, store):
        await store.put("r1", thread_id="t1")
        await store.update_run_completion("r1", status="success", total_tokens=10)
        await store.put("r2", thread_id="t1")
        await store.update_run_completion("r2", status="running", total_tokens=5)

        assert (await store.aggregate_tokens_by_thread("t1"))["total_tokens"] == 10
        assert (await store.aggregate_tokens_by_thread("t1", include_active=True))["total_tokens"] == 15

    @pytest.mark.anyio
    async def test_aggregate_tokens_by_thread_unknown_thread_is_zero(self, store):
        await store.put("r1", thread_id="t1")
        await store.update_run_completion("r1", status="success", total_tokens=10)
        agg = await store.aggregate_tokens_by_thread("missing")
        assert agg["total_tokens"] == 0
        assert agg["total_runs"] == 0
        assert agg["by_model"] == {}

    @pytest.mark.anyio
    async def test_aggregate_tokens_by_thread_matches_full_scan_reference(self, store):
        plan = [
            ("r0", "t1", "success", "m-a", 10),
            ("r1", "t1", "error", "m-b", 20),
            ("r2", "t1", "running", "m-a", 7),
            ("r3", "t2", "success", "m-a", 999),
            ("r4", "t1", "pending", "m-a", 3),
        ]
        for run_id, thread_id, status, model, tokens in plan:
            await store.put(run_id, thread_id=thread_id)
            await store.update_run_completion(run_id, status=status, model_name=model, total_tokens=tokens)

        def _reference(thread_id, include_active):
            statuses = ("success", "error", "running") if include_active else ("success", "error")
            completed = [r for r in store._runs.values() if r["thread_id"] == thread_id and r.get("status") in statuses]
            return len(completed), sum(r.get("total_tokens", 0) for r in completed)

        for thread_id in ("t1", "t2", "missing"):
            for include_active in (False, True):
                agg = await store.aggregate_tokens_by_thread(thread_id, include_active=include_active)
                ref_runs, ref_tokens = _reference(thread_id, include_active)
                assert (agg["total_runs"], agg["total_tokens"]) == (ref_runs, ref_tokens), (thread_id, include_active)

    @pytest.mark.anyio
    async def test_list_pending(self, store):
        await store.put("r1", thread_id="t1", status="pending")
        await store.put("r2", thread_id="t1", status="running")
        await store.put("r3", thread_id="t2", status="pending")
        pending = await store.list_pending()
        assert len(pending) == 2
        assert all(r["status"] == "pending" for r in pending)

    @pytest.mark.anyio
    async def test_list_pending_respects_before(self, store):
        past = "2020-01-01T00:00:00+00:00"
        future = "2099-01-01T00:00:00+00:00"
        await store.put("r1", thread_id="t1", status="pending", created_at=past)
        await store.put("r2", thread_id="t1", status="pending", created_at=future)
        pending = await store.list_pending(before=datetime.now(UTC).isoformat())
        assert len(pending) == 1
        assert pending[0]["run_id"] == "r1"

    @pytest.mark.anyio
    async def test_list_pending_fifo_order(self, store):
        await store.put("r2", thread_id="t1", status="pending", created_at="2024-01-02T00:00:00+00:00")
        await store.put("r1", thread_id="t1", status="pending", created_at="2024-01-01T00:00:00+00:00")
        pending = await store.list_pending()
        assert pending[0]["run_id"] == "r1"


# -- Base.to_dict mixin --


class TestBaseToDictMixin:
    @pytest.mark.anyio
    async def test_to_dict_and_exclude(self, migrated_postgres_database_url):
        """Create a temporary PostgreSQL table and verify ``to_dict``."""
        from sqlalchemy import String
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.orm import Mapped, mapped_column

        from deerflow.persistence.base import Base

        class _Tmp(Base):
            __tablename__ = "_tmp_test"
            id: Mapped[str] = mapped_column(String(64), primary_key=True)
            name: Mapped[str] = mapped_column(String(128))

        engine = create_async_engine(migrated_postgres_database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            session.add(_Tmp(id="1", name="hello"))
            await session.commit()
            obj = await session.get(_Tmp, "1")

            assert obj.to_dict() == {"id": "1", "name": "hello"}
            assert obj.to_dict(exclude={"name"}) == {"id": "1"}
            assert "_Tmp" in repr(obj)

        await engine.dispose()
