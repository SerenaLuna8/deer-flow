"""PostgreSQL-only asynchronous LangGraph checkpointer provider."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langgraph.types import Checkpointer

from deerflow.config.app_config import AppConfig, get_app_config
from deerflow.runtime.checkpointer.provider import POSTGRES_INSTALL


def _load_async_dependencies():
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        raise RuntimeError(POSTGRES_INSTALL) from exc
    return AsyncPostgresSaver, dict_row, AsyncConnectionPool


def _build_postgres_pool(conn_string: str, *, dict_row, pool_class):
    return pool_class(conn_string, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, check=pool_class.check_connection)


@dataclass(frozen=True, slots=True)
class PostgresCheckpointTransaction:
    """One raw saver and connection bound to the same SQL transaction."""

    saver: Checkpointer
    connection: Any


@contextlib.asynccontextmanager
async def postgres_checkpoint_transaction(
    saver: Checkpointer,
) -> AsyncIterator[PostgresCheckpointTransaction | None]:
    """Bind LangGraph checkpoint operations to one explicit psycopg transaction.

    ``AsyncPostgresSaver`` normally runs on an autocommit connection or pool.
    The bound saver yielded here uses the exact connection whose transaction
    also carries application-owned lease fencing. Non-PostgreSQL savers are
    yielded as ``None`` for isolated SDK/unit-test use only.
    """

    AsyncPostgresSaver, _dict_row, _pool_class = _load_async_dependencies()
    if not isinstance(saver, AsyncPostgresSaver):
        yield None
        return

    from langgraph.checkpoint.postgres._ainternal import get_connection

    async with saver.lock, get_connection(saver.conn) as connection:
        async with connection.transaction():
            yield PostgresCheckpointTransaction(
                saver=AsyncPostgresSaver(
                    conn=connection,
                    serde=saver.serde,
                ),
                connection=connection,
            )


@contextlib.asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
    config = app_config or get_app_config()
    AsyncPostgresSaver, dict_row, pool_class = _load_async_dependencies()
    pool = _build_postgres_pool(config.database.checkpointer_url, dict_row=dict_row, pool_class=pool_class)
    async with pool:
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()
        yield saver


__all__ = [
    "PostgresCheckpointTransaction",
    "make_checkpointer",
    "postgres_checkpoint_transaction",
]
