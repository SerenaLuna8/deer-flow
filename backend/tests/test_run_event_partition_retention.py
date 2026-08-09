from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.prune_run_event_partitions import (
    RunEventRetentionError,
    parse_utc_month_start,
    prune_run_event_partitions,
)


def test_retention_cutoff_requires_an_exact_utc_month_boundary() -> None:
    assert parse_utc_month_start("2024-02-01T00:00:00Z") == datetime(
        2024,
        2,
        1,
        tzinfo=UTC,
    )
    for value in (
        "2024-02-02T00:00:00Z",
        "2024-02-01T00:00:01Z",
        "2024-02-01T00:00:00+08:00",
        "2024-02-01",
        "not-a-date",
    ):
        with pytest.raises(RunEventRetentionError):
            parse_utc_month_start(value)
    future_year = datetime.now(UTC).year + 1
    with pytest.raises(RunEventRetentionError):
        parse_utc_month_start(f"{future_year}-01-01T00:00:00Z")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_operator_entry_previews_then_drops_only_whole_old_months(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    old_month = datetime(2024, 1, 1, tzinfo=UTC)
    cutoff = datetime(2024, 2, 1, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            partition = await connection.scalar(
                text("SELECT ensure_run_events_month_partition(:target)"),
                {"target": old_month},
            )
        assert partition == "run_events_p202401"

        preview = await prune_run_event_partitions(
            migrated_postgres_database_url,
            cutoff,
            apply=False,
        )
        assert preview.applied is False
        assert preview.eligible_partitions == ("run_events_p202401",)
        assert preview.dropped_partitions == 0

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401') IS NOT NULL")) is True

        applied = await prune_run_event_partitions(
            migrated_postgres_database_url,
            cutoff,
            apply=True,
        )
        assert applied.applied is True
        assert applied.eligible_partitions == ("run_events_p202401",)
        assert applied.dropped_partitions == 1

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401') IS NULL")) is True
            retained_from = await connection.scalar(text("SELECT retained_from FROM run_event_partition_state WHERE singleton"))
        assert retained_from == cutoff
    finally:
        await engine.dispose()
