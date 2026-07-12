"""Generate a new alembic revision against an isolated PostgreSQL database.

Used by ``make migrate-rev MSG="..."``. Avoids two pitfalls:

1. Autogenerate must never depend on a developer's persistent application DB.
2. A persistent DB might be at an unknown revision (or at no revision at all),
   producing a noisy autogenerate diff that mixes "real" changes with
   accidentally-detected drift.

This script creates a random PostgreSQL database from an explicit
``POSTGRES_ADMIN_URL``, runs the existing alembic chain to ``head`` against it,
then runs ``alembic revision --autogenerate`` against that. The database must
be built from migration history -- not from
``Base.metadata.create_all`` -- so newly edited ORM fields that do not yet have
a revision remain visible to autogenerate as a real diff.

The generated file lands in
``packages/harness/deerflow/persistence/migrations/versions/`` -- exactly
where alembic puts it by default. The random database is terminated and dropped
in a ``finally`` block, including when upgrade or revision generation fails.
Review the generated revision and switch raw ``op.add_column`` /
``op.drop_column`` calls to the idempotent helpers in ``migrations/_helpers.py``
before committing.

Run from the ``backend/`` directory:
    PYTHONPATH=. uv run python scripts/_autogen_revision.py "MESSAGE"
or via Makefile:
    make migrate-rev MSG="..."
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

import deerflow.persistence.models  # noqa: F401  -- registers ORM models with Base.metadata
from deerflow.persistence.bootstrap import _escape_url_for_alembic

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = BACKEND_DIR / "packages/harness/deerflow/persistence/migrations"
_AUTOGEN_DATABASE_PATTERN = re.compile(r"deerflow_autogen_[0-9]+_[0-9a-f]{32}\Z")
_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Shared with ``bootstrap._alembic_safe_url`` so the ConfigParser ``%``
    # interpolation rule lives in one place.
    cfg.set_main_option("sqlalchemy.url", _escape_url_for_alembic(url))
    return cfg


def _postgres_url_with_database(url: str, database: str) -> str:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("autogen requires a PostgreSQL URL")
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.set(database=database).render_as_string(hide_password=False)


def _require_admin_url(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or parsed.database != "postgres":
        raise ValueError("POSTGRES_ADMIN_URL must target the PostgreSQL maintenance database")


def _require_disposable_database_url(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("autogen requires PostgreSQL")
    database = parsed.database or ""
    if not (_AUTOGEN_DATABASE_PATTERN.fullmatch(database) or _TEST_DATABASE_PATTERN.fullmatch(database)):
        raise ValueError("autogen requires a disposable PostgreSQL database")


def _asyncpg_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _create_database(admin_url: str, database: str) -> None:
    connection = None
    try:
        connection = await asyncpg.connect(_asyncpg_dsn(_postgres_url_with_database(admin_url, "postgres")))
        await connection.execute(f'CREATE DATABASE "{database}"')
    except Exception:
        raise RuntimeError("autogen could not create an isolated PostgreSQL database") from None
    finally:
        if connection is not None:
            await connection.close()


async def _drop_database(admin_url: str, database: str) -> None:
    connection = None
    try:
        connection = await asyncpg.connect(_asyncpg_dsn(_postgres_url_with_database(admin_url, "postgres")))
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}"')
    except Exception:
        raise RuntimeError("autogen could not clean up its isolated PostgreSQL database") from None
    finally:
        if connection is not None:
            await connection.close()


@contextmanager
def _temporary_postgres_database(admin_url: str) -> Iterator[str]:
    _require_admin_url(admin_url)
    database = f"deerflow_autogen_{os.getpid()}_{uuid.uuid4().hex}"
    if _AUTOGEN_DATABASE_PATTERN.fullmatch(database) is None:
        raise RuntimeError("autogen generated an unsafe PostgreSQL database name")
    asyncio.run(_create_database(admin_url, database))
    try:
        yield _postgres_url_with_database(admin_url, database)
    finally:
        asyncio.run(_drop_database(admin_url, database))


def _build_temp_db_at_head(database_url: str) -> str:
    _require_disposable_database_url(database_url)
    command.upgrade(_alembic_config(database_url), "head")
    return database_url


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('usage: python scripts/_autogen_revision.py "describe the change"', file=sys.stderr)
        sys.exit(2)
    message = sys.argv[1]

    admin_url = os.getenv("POSTGRES_ADMIN_URL")
    if not admin_url:
        print("autogen: POSTGRES_ADMIN_URL is required", file=sys.stderr)
        sys.exit(2)

    with _temporary_postgres_database(admin_url) as url:
        _build_temp_db_at_head(url)
        print("autogen: built isolated PostgreSQL database at head", file=sys.stderr)
        command.revision(_alembic_config(url), message=message, autogenerate=True)


if __name__ == "__main__":
    main()
