from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.prepare_run_event_partitions import (
    RunEventPartitionPreparationError,
    partition_month_starts,
    prepare_run_event_partitions,
)


def test_partition_month_starts_covers_current_month_through_n_plus_two() -> None:
    assert partition_month_starts(datetime(2026, 12, 31, 23, tzinfo=UTC)) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
        datetime(2027, 2, 1, tzinfo=UTC),
    )
    assert partition_month_starts(datetime(2027, 1, 1, 7, tzinfo=timezone(timedelta(hours=8))))[0] == datetime(2026, 12, 1, tzinfo=UTC)


def test_partition_month_starts_rejects_naive_timestamp() -> None:
    with pytest.raises(RunEventPartitionPreparationError, match="timezone-aware"):
        partition_month_starts(datetime(2026, 12, 1))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_operator_prepares_current_month_through_n_plus_two(
    migrated_postgres_database_url: str,
) -> None:
    as_of = datetime(2035, 12, 20, tzinfo=UTC)

    result = await prepare_run_event_partitions(
        migrated_postgres_database_url,
        as_of=as_of,
    )
    repeated = await prepare_run_event_partitions(
        migrated_postgres_database_url,
        as_of=as_of,
    )

    assert result.partitions == (
        "run_events_p203512",
        "run_events_p203601",
        "run_events_p203602",
    )
    assert repeated.partitions == result.partitions
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            for partition in result.partitions:
                assert await connection.scalar(
                    text("SELECT to_regclass(:partition) IS NOT NULL"),
                    {"partition": partition},
                )
    finally:
        await engine.dispose()
