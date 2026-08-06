"""PostgreSQL-only synchronous LangGraph checkpointer provider."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig, get_app_config

POSTGRES_INSTALL = "PostgreSQL checkpointer dependencies are missing. Reinstall the default backend dependencies with: cd backend && uv sync --all-packages"


@contextlib.contextmanager
def _sync_checkpointer_cm(config: AppConfig) -> Iterator[Checkpointer]:
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise ImportError(POSTGRES_INSTALL) from exc
    with PostgresSaver.from_conn_string(config.database.checkpointer_url) as saver:
        saver.setup()
        yield saver


_checkpointer: Checkpointer | None = None
_checkpointer_ctx = None
_checkpointer_lock = threading.Lock()


def get_checkpointer() -> Checkpointer:
    global _checkpointer, _checkpointer_ctx
    if _checkpointer is not None:
        return _checkpointer
    config = get_app_config()
    with _checkpointer_lock:
        if _checkpointer is None:
            _checkpointer_ctx = _sync_checkpointer_cm(config)
            _checkpointer = _checkpointer_ctx.__enter__()
    return _checkpointer


def reset_checkpointer() -> None:
    global _checkpointer, _checkpointer_ctx
    with _checkpointer_lock:
        if _checkpointer_ctx is not None:
            _checkpointer_ctx.__exit__(None, None, None)
        _checkpointer_ctx = None
        _checkpointer = None


@contextlib.contextmanager
def checkpointer_context(app_config: AppConfig | None = None) -> Iterator[Checkpointer]:
    with _sync_checkpointer_cm(app_config or get_app_config()) as saver:
        yield saver
