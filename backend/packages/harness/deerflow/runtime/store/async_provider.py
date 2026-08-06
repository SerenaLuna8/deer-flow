"""PostgreSQL-only asynchronous LangGraph Store provider."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from langgraph.store.base import BaseStore

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.store.provider import POSTGRES_STORE_INSTALL


@contextlib.asynccontextmanager
async def make_store(app_config: AppConfig | None = None) -> AsyncIterator[BaseStore]:
    config = app_config or get_app_config()
    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore
    except ImportError as exc:
        raise ImportError(POSTGRES_STORE_INSTALL) from exc
    async with AsyncPostgresStore.from_conn_string(config.database.checkpointer_url) as store:
        await store.setup()
        yield store
