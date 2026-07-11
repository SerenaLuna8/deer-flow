from __future__ import annotations

import builtins
import importlib
import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _config(url: str = "postgresql://user:secret@localhost/test"):
    return SimpleNamespace(database=SimpleNamespace(checkpointer_url=url))


def test_legacy_checkpointer_shim_is_removed() -> None:
    assert importlib.util.find_spec("deerflow.config.checkpointer_config") is None


def test_sql_user_repository_is_the_only_public_sql_repository() -> None:
    from app.gateway.auth.repositories.sql import SQLUserRepository

    assert SQLUserRepository.__name__ == "SQLUserRepository"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.gateway.auth.repositories.sqlite")


def test_sync_checkpointer_uses_postgres_url() -> None:
    from deerflow.runtime.checkpointer.provider import _sync_checkpointer_cm

    saver = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = saver
    manager.__exit__.return_value = None
    factory = MagicMock(return_value=manager)
    with patch("langgraph.checkpoint.postgres.PostgresSaver.from_conn_string", factory):
        with _sync_checkpointer_cm(_config()) as actual:
            assert actual is saver
    factory.assert_called_once_with("postgresql://user:secret@localhost/test")
    saver.setup.assert_called_once_with()
    manager.__exit__.assert_called_once()


@pytest.mark.asyncio
async def test_async_checkpointer_uses_postgres_pool() -> None:
    from deerflow.runtime.checkpointer import async_provider

    pool = AsyncMock()
    pool.__aenter__.return_value = pool
    saver = SimpleNamespace(setup=AsyncMock())
    saver_cls = MagicMock(return_value=saver)
    with patch.object(async_provider, "_build_postgres_pool", return_value=pool), patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", saver_cls):
        async with async_provider.make_checkpointer(_config()) as actual:
            assert actual is saver
    saver_cls.assert_called_once_with(conn=pool)
    saver.setup.assert_awaited_once()


def test_async_checkpointer_pool_configuration() -> None:
    from deerflow.runtime.checkpointer.async_provider import _build_postgres_pool

    row_factory = object()

    class FakePool:
        check_connection = object()

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    pool = _build_postgres_pool("postgresql://localhost/test", dict_row=row_factory, pool_class=FakePool)
    assert pool.args == ("postgresql://localhost/test",)
    assert pool.kwargs["kwargs"] == {"autocommit": True, "prepare_threshold": 0, "row_factory": row_factory}
    assert pool.kwargs["check"] is FakePool.check_connection


def test_sync_store_uses_postgres_url_setup_and_cleanup() -> None:
    from deerflow.runtime.store.provider import _sync_store_cm

    store = MagicMock()
    manager = MagicMock()
    manager.__enter__.return_value = store
    factory = MagicMock(return_value=manager)
    with patch("langgraph.store.postgres.PostgresStore.from_conn_string", factory):
        with _sync_store_cm(_config()) as actual:
            assert actual is store
    factory.assert_called_once_with("postgresql://user:secret@localhost/test")
    store.setup.assert_called_once_with()
    manager.__exit__.assert_called_once()


@pytest.mark.asyncio
async def test_async_store_uses_postgres_url_setup_and_cleanup() -> None:
    from deerflow.runtime.store.async_provider import make_store

    store = SimpleNamespace(setup=AsyncMock())
    manager = AsyncMock()
    manager.__aenter__.return_value = store
    factory = MagicMock(return_value=manager)
    with patch("langgraph.store.postgres.aio.AsyncPostgresStore.from_conn_string", factory):
        async with make_store(_config()) as actual:
            assert actual is store
    factory.assert_called_once_with("postgresql://user:secret@localhost/test")
    store.setup.assert_awaited_once()
    manager.__aexit__.assert_awaited_once()


def test_dependency_errors_do_not_reference_extras_or_sqlite() -> None:
    from deerflow.runtime.checkpointer.provider import POSTGRES_INSTALL
    from deerflow.runtime.store.provider import POSTGRES_STORE_INSTALL

    message = POSTGRES_INSTALL + POSTGRES_STORE_INSTALL
    assert "extra" not in message.lower()
    assert "sqlite" not in message.lower()


@pytest.mark.parametrize(
    "missing_module",
    ["langgraph.checkpoint.postgres.aio", "psycopg.rows", "psycopg_pool"],
)
@pytest.mark.asyncio
async def test_async_checkpointer_dependency_failures_are_actionable_runtime_errors(missing_module: str, monkeypatch) -> None:
    from deerflow.runtime.checkpointer.async_provider import make_checkpointer

    original_import = builtins.__import__

    def fail_selected_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == missing_module:
            raise ModuleNotFoundError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_selected_import)
    with pytest.raises(RuntimeError, match="default backend dependencies") as exc_info:
        async with make_checkpointer(_config()):
            pass
    message = str(exc_info.value).lower()
    assert "extra" not in message
    assert "sqlite" not in message
