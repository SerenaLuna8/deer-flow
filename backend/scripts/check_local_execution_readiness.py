#!/usr/bin/env python3
"""Content-free local execution readiness for the five-process launcher."""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.final_schema import (
    FinalSchemaError,
    FinalSchemaProbe,
)
from deerflow.config import get_app_config
from deerflow.config.worker_config import (
    RELEASE_WORKER_MAX_CONCURRENT_JOBS,
    RELEASE_WORKER_PROCESS_COUNT,
)

READY = 0
DATABASE_OR_SCHEMA_UNAVAILABLE = 2
WORKER_TIMEOUT = 3


class _SchemaProbe(Protocol):
    async def require_ready(self, session: AsyncSession) -> object: ...


async def probe_local_execution_readiness(
    session: AsyncSession,
    *,
    worker_fresh_for_seconds: int,
    schema_probe: _SchemaProbe | None = None,
) -> bool:
    """Return whether an exact fresh ``private_run`` Worker is executable."""

    if type(worker_fresh_for_seconds) is not int or worker_fresh_for_seconds < 1:
        raise ValueError("worker freshness must be a positive integer")
    await (schema_probe or FinalSchemaProbe()).require_ready(session)
    return (
        await session.scalar(
            text(
                """SELECT count(*) = :expected_worker_count
                          AND COALESCE(min(max_concurrent_jobs), 0)
                              = :expected_worker_capacity
                          AND COALESCE(max(max_concurrent_jobs), 0)
                              = :expected_worker_capacity
                   FROM worker_nodes
                   WHERE draining=false
                     AND heartbeat_at >= clock_timestamp()
                         - make_interval(secs => :worker_fresh_for_seconds)
                     AND capabilities_json::jsonb
                         @> '["private_run"]'::jsonb"""
            ),
            {
                "expected_worker_capacity": (RELEASE_WORKER_MAX_CONCURRENT_JOBS),
                "expected_worker_count": RELEASE_WORKER_PROCESS_COUNT,
                "worker_fresh_for_seconds": worker_fresh_for_seconds,
            },
        )
        is True
    )


async def wait_for_local_execution_readiness(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_fresh_for_seconds: int,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.5,
) -> int:
    """Wait within a bound and return the documented content-free exit code."""

    if not callable(session_factory) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0 or not isinstance(poll_interval_seconds, (int, float)) or poll_interval_seconds <= 0:
        raise ValueError("local execution readiness configuration is invalid")

    deadline = time.monotonic() + float(timeout_seconds)
    schema_verified = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WORKER_TIMEOUT if schema_verified else DATABASE_OR_SCHEMA_UNAVAILABLE
        try:
            async with asyncio.timeout(remaining):
                async with session_factory() as session:
                    ready = await probe_local_execution_readiness(
                        session,
                        worker_fresh_for_seconds=worker_fresh_for_seconds,
                    )
            schema_verified = True
        except (FinalSchemaError, SQLAlchemyError, OSError, RuntimeError):
            return DATABASE_OR_SCHEMA_UNAVAILABLE
        except TimeoutError:
            return WORKER_TIMEOUT if schema_verified else DATABASE_OR_SCHEMA_UNAVAILABLE
        if ready:
            return READY
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WORKER_TIMEOUT
        await asyncio.sleep(min(float(poll_interval_seconds), remaining))


async def _run(
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> int:
    config = get_app_config()
    engine = create_async_engine(
        config.database.sqlalchemy_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(config.database.statement_timeout_seconds * 1000),
            },
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await wait_for_local_execution_readiness(
            factory,
            worker_fresh_for_seconds=config.worker.heartbeat_seconds * 3,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only local Skill execution readiness check.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=0.5,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        status = asyncio.run(
            _run(
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
        )
    except (OSError, RuntimeError, ValueError):
        status = DATABASE_OR_SCHEMA_UNAVAILABLE

    if status == READY:
        print("Local execution readiness: ready")
    elif status == WORKER_TIMEOUT:
        print("Local execution readiness: worker timeout")
    else:
        print("Local execution readiness: database or schema unavailable")
    return status


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATABASE_OR_SCHEMA_UNAVAILABLE",
    "READY",
    "WORKER_TIMEOUT",
    "probe_local_execution_readiness",
    "wait_for_local_execution_readiness",
]
