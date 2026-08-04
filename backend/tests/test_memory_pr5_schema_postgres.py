from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_live_fact_evidence_allows_missing_run_event_sequence(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            definition = await connection.scalar(
                text(
                    """SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid='memory_fact_evidence'::regclass
                      AND conname='ck_memory_fact_evidence_source_state'"""
                )
            )
        assert definition is not None
        assert "run_event_sequence IS NOT NULL" not in definition
        assert "thread_id IS NOT NULL" in definition
        assert "run_id IS NOT NULL" in definition
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_job_persists_one_exact_cutoff(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            column_type = await connection.scalar(
                text(
                    """SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema=current_schema()
                      AND table_name='jobs'
                      AND column_name='memory_retention_cutoff_at'"""
                )
            )
            definition = await connection.scalar(
                text(
                    """SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid='jobs'::regclass
                      AND conname='ck_jobs_memory_retention_cutoff'"""
                )
            )
        assert column_type == "timestamp with time zone"
        assert definition is not None
        assert "memory_retention_purge" in definition
        assert "memory_retention_cutoff_at IS NOT NULL" in definition
        assert "memory_retention_cutoff_at <= created_at" in definition
        assert "memory_retention_cutoff_at IS NULL" in definition
    finally:
        await engine.dispose()
