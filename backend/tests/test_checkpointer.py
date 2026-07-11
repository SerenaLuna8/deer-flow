from __future__ import annotations

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


def test_dependency_errors_do_not_reference_extras_or_sqlite() -> None:
    from deerflow.runtime.checkpointer.provider import POSTGRES_INSTALL
    from deerflow.runtime.store.provider import POSTGRES_STORE_INSTALL

    message = POSTGRES_INSTALL + POSTGRES_STORE_INSTALL
    assert "extra" not in message.lower()
    assert "sqlite" not in message.lower()
