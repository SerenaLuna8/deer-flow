"""Alembic environment for the ActWeave incremental migration chain.

The connection URL comes from exactly two explicit sources, in order:

1. a programmatic ``config.attributes["sqlalchemy_url"]`` (tests and
   ``scripts/upgrade_postgres.py`` pass the already-resolved URL this way,
   avoiding configparser ``%`` interpolation of credential characters);
2. the ``DATABASE_URL`` process environment variable — the same environment
   discipline as ``scripts/run_runtime.py``. There is never an implicit
   dotenv load here.

Migration scripts write explicit DDL only and must not import ORM models
(D3: the result of a migration must not drift with the current code), so
this environment deliberately configures no target metadata and supports no
autogenerate.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def _sync_database_url() -> str:
    url = context.config.attributes.get("sqlalchemy_url", "")
    if not url:
        url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL must be set explicitly to run migrations (no implicit dotenv load)")
    # Alembic runs synchronously; psycopg is the installed sync driver.
    for async_prefix in ("postgresql+asyncpg://", "postgresql://"):
        if url.startswith(async_prefix):
            return "postgresql+psycopg://" + url[len(async_prefix) :]
    if url.startswith("postgresql+psycopg://"):
        return url
    raise RuntimeError("DATABASE_URL must be a PostgreSQL URL")


def run_migrations_offline() -> None:
    """Render SQL to stdout without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_sync_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_database_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
