"""PostgreSQL-only synchronous LangGraph Store provider."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import AppConfig, get_app_config

POSTGRES_STORE_INSTALL = "PostgreSQL store dependencies are missing. Reinstall the default backend dependencies with: cd backend && uv sync --all-packages"


@contextlib.contextmanager
def _sync_store_cm(config: AppConfig) -> Iterator[BaseStore]:
    try:
        from langgraph.store.postgres import PostgresStore
    except ImportError as exc:
        raise ImportError(POSTGRES_STORE_INSTALL) from exc
    with PostgresStore.from_conn_string(config.database.checkpointer_url) as store:
        store.setup()
        yield store


_store: BaseStore | None = None
_store_ctx = None
_store_lock = threading.Lock()


def get_store() -> BaseStore:
    global _store, _store_ctx
    if _store is not None:
        return _store
    config = get_app_config()
    with _store_lock:
        if _store is None:
            _store_ctx = _sync_store_cm(config)
            _store = _store_ctx.__enter__()
    return _store


def reset_store() -> None:
    global _store, _store_ctx
    with _store_lock:
        if _store_ctx is not None:
            _store_ctx.__exit__(None, None, None)
        _store_ctx = None
        _store = None


@contextlib.contextmanager
def store_context(app_config: AppConfig | None = None) -> Iterator[BaseStore]:
    with _sync_store_cm(app_config or get_app_config()) as store:
        yield store
