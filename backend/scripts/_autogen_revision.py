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
    MIGRATION_MESSAGE="MESSAGE" POSTGRES_ADMIN_URL="postgresql://.../postgres" \
      PYTHONPATH=. uv run python scripts/_autogen_revision.py
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
_EMPTY_FINALIZE_DOMAINS = (
    "threads",
    "runs",
    "run_events",
    "feedback",
    "checkpoints",
    "files",
    "memory",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
    "counts_probe",
    "scope_probe",
)


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


async def _seed_empty_finalize_prerequisites(database_url: str) -> None:
    """Record the explicit zero-source migration proof required by 0009.

    Autogeneration builds from migration history, so it cannot use the runtime
    empty-install ``create_all + stamp`` shortcut.  Revision 0009 intentionally
    refuses to finalize without an execute run and all probe ledger domains;
    an isolated, freshly-created autogen database has zero source rows, making
    this deterministic empty proof the migration-history equivalent.
    """
    connection = await asyncpg.connect(_asyncpg_dsn(database_url))
    migration_run_id = uuid.uuid4()
    empty_digest = "0" * 64
    try:
        async with connection.transaction():
            await connection.execute(
                """INSERT INTO private_work_migration_runs
                (id,mode,status,source_fingerprint,owner_map_digest,
                 legacy_source_probe_complete,checkpoint_marker_probe_complete,
                 cross_scope_probe_complete,completed_at)
                VALUES ($1,'execute','completed',$2,$2,true,true,true,now())""",
                migration_run_id,
                empty_digest,
            )
            await connection.executemany(
                """INSERT INTO private_work_migration_ledger
                (migration_run_id,domain,source_key_hash,source_fingerprint,
                 target_digest,status,row_count,byte_count)
                VALUES ($1,$2,$3,$3,$3,'complete',0,0)""",
                [(migration_run_id, domain, empty_digest) for domain in _EMPTY_FINALIZE_DOMAINS],
            )
    finally:
        await connection.close()


@contextmanager
def _temporary_postgres_database(admin_url: str) -> Iterator[str]:
    _require_admin_url(admin_url)
    database = f"deerflow_autogen_{os.getpid()}_{uuid.uuid4().hex}"
    if _AUTOGEN_DATABASE_PATTERN.fullmatch(database) is None:
        raise RuntimeError("autogen generated an unsafe PostgreSQL database name")
    try:
        asyncio.run(_create_database(admin_url, database))
    except BaseException:
        try:
            asyncio.run(_drop_database(admin_url, database))
        except BaseException:
            pass
        raise
    try:
        yield _postgres_url_with_database(admin_url, database)
    except BaseException:
        try:
            asyncio.run(_drop_database(admin_url, database))
        except BaseException:
            pass
        raise
    else:
        asyncio.run(_drop_database(admin_url, database))


def _build_temp_db_at_head(database_url: str) -> str:
    _require_disposable_database_url(database_url)
    config = _alembic_config(database_url)
    command.upgrade(config, "0008_project_private_work_expand")
    asyncio.run(_seed_empty_finalize_prerequisites(database_url))
    command.upgrade(config, "head")
    return database_url


def _validate_migration_message(raw_message: str) -> str:
    message = raw_message.strip()
    if not message or len(message) > 200:
        raise ValueError("invalid migration message")
    if any(ord(character) < 32 or ord(character) == 127 for character in message):
        raise ValueError("invalid migration message")
    return message


def _migration_message_from_env() -> str:
    return _validate_migration_message(os.getenv("MIGRATION_MESSAGE", ""))


def main() -> None:
    try:
        message = _migration_message_from_env()
    except ValueError:
        print("autogen: MIGRATION_MESSAGE must be 1-200 characters without control characters", file=sys.stderr)
        raise SystemExit(2) from None

    admin_url = os.getenv("POSTGRES_ADMIN_URL")
    if not admin_url:
        print("autogen: POSTGRES_ADMIN_URL is required", file=sys.stderr)
        raise SystemExit(2)

    with _temporary_postgres_database(admin_url) as url:
        _build_temp_db_at_head(url)
        print("autogen: built isolated PostgreSQL database at head", file=sys.stderr)
        command.revision(_alembic_config(url), message=message, autogenerate=True)


if __name__ == "__main__":
    main()
