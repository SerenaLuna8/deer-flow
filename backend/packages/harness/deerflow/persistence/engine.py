"""PostgreSQL-only async SQLAlchemy engine lifecycle."""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from deerflow.config.database_config import DatabaseConfig

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _json_serializer(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


async def init_engine(config: DatabaseConfig) -> None:
    """Initialize and probe the configured PostgreSQL database."""
    global _engine, _session_factory

    if not isinstance(config, DatabaseConfig):
        raise TypeError("init_engine() requires a DatabaseConfig")

    engine = create_async_engine(
        config.sqlalchemy_url,
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"server_settings": {"statement_timeout": str(config.statement_timeout_seconds * 1000)}},
        json_serializer=_json_serializer,
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        from deerflow.persistence.bootstrap import validate_schema

        await validate_schema(engine)
    except Exception as exc:
        await engine.dispose()
        _engine = None
        _session_factory = None
        raise RuntimeError("Unable to initialize PostgreSQL database. Verify database.url and create the target database before starting DeerFlow.") from exc

    if _engine is not None:
        await _engine.dispose()
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("PostgreSQL persistence engine initialized")


async def init_engine_from_config(config: DatabaseConfig) -> None:
    await init_engine(config)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Persistence engine is not initialized")
    return _session_factory


def get_engine() -> AsyncEngine | None:
    return _engine


async def close_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
