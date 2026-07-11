"""PostgreSQL-only asynchronous LangGraph checkpointer provider."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.checkpointer.provider import POSTGRES_INSTALL


def _build_postgres_pool(conn_string: str):
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    return AsyncConnectionPool(conn_string, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, check=AsyncConnectionPool.check_connection)


@contextlib.asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
    config = app_config or get_app_config()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise ImportError(POSTGRES_INSTALL) from exc
    pool = _build_postgres_pool(config.database.checkpointer_url)
    async with pool:
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()
        yield saver
