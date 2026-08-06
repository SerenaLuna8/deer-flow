from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.run.model import RunRow

_TERMINAL_RUN_STATUSES = ("success", "error", "timeout", "interrupted")
_TOKEN_WINDOW_HOURS = 24


@dataclass(frozen=True, slots=True)
class ProjectTokenUsagePoint:
    bucket_start: datetime
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ProjectTokenUsageSeries:
    window_start: datetime
    window_end: datetime
    bucket_minutes: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    points: tuple[ProjectTokenUsagePoint, ...]


def _utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("project token usage time must include a timezone")
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


async def read_project_token_usage_24h(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ProjectTokenUsageSeries:
    """Return 24 UTC hour buckets by durable job settlement time."""

    checked_at = now or datetime.now(UTC)
    current_hour = _utc_hour(checked_at)
    window_start = current_hour - timedelta(hours=_TOKEN_WINDOW_HOURS - 1)
    window_end = checked_at.astimezone(UTC)
    bucket = sa.func.date_trunc(
        "hour",
        JobRow.completed_at,
        "UTC",
    ).label("bucket_start")
    rows = (
        await session.execute(
            sa.select(
                bucket,
                sa.func.coalesce(sa.func.sum(RunRow.total_input_tokens), 0),
                sa.func.coalesce(sa.func.sum(RunRow.total_output_tokens), 0),
                sa.func.coalesce(sa.func.sum(RunRow.total_tokens), 0),
            )
            .join(JobRow, JobRow.id == RunRow.job_id)
            .where(
                RunRow.project_id == project_id,
                RunRow.status.in_(_TERMINAL_RUN_STATUSES),
                JobRow.completed_at >= window_start,
                JobRow.completed_at <= window_end,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()
    by_hour = {
        _utc_hour(bucket_start): (
            int(input_tokens),
            int(output_tokens),
            int(total_tokens),
        )
        for bucket_start, input_tokens, output_tokens, total_tokens in rows
    }
    points = tuple(
        ProjectTokenUsagePoint(
            bucket_start=bucket_start,
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=usage[2],
        )
        for bucket_start in (window_start + timedelta(hours=offset) for offset in range(_TOKEN_WINDOW_HOURS))
        for usage in (by_hour.get(bucket_start, (0, 0, 0)),)
    )
    return ProjectTokenUsageSeries(
        window_start=window_start,
        window_end=window_end,
        bucket_minutes=60,
        input_tokens=sum(point.input_tokens for point in points),
        output_tokens=sum(point.output_tokens for point in points),
        total_tokens=sum(point.total_tokens for point in points),
        points=points,
    )


__all__ = [
    "ProjectTokenUsagePoint",
    "ProjectTokenUsageSeries",
    "read_project_token_usage_24h",
]
