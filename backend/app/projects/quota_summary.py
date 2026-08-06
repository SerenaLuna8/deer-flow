from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.projects.models import ProjectQuotaSummary, QuotaDimensionSummary
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.quotas.model import ProjectQuotaRow, ProjectUsageCounterRow


def _usage_column(
    project_id: ColumnElement[uuid.UUID],
    *,
    dimension: str,
    bucket: str,
    field: ColumnElement[int],
    label: str,
) -> ColumnElement[int]:
    value = (
        select(field)
        .where(
            ProjectUsageCounterRow.project_id == project_id,
            ProjectUsageCounterRow.dimension == dimension,
            ProjectUsageCounterRow.bucket == bucket,
        )
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    return func.coalesce(value, literal(0)).label(label)


def _limit_column(
    project_id: ColumnElement[uuid.UUID],
    *,
    field: ColumnElement[int | None],
    default: int,
    label: str,
) -> ColumnElement[int]:
    configured = select(field).where(ProjectQuotaRow.project_id == project_id).correlate(ProjectRow).scalar_subquery()
    return func.least(func.coalesce(configured, literal(default)), literal(default)).label(label)


def project_quota_summary_columns(
    project_id: ColumnElement[uuid.UUID],
    config: QuotaConfig,
    *,
    now: datetime | None = None,
) -> tuple[ColumnElement[int], ...]:
    selected_time = now or datetime.now(UTC)
    if selected_time.tzinfo is None or selected_time.utcoffset() is None:
        raise ValueError("quota summary time must be timezone aware")
    daily_bucket = selected_time.astimezone(UTC).date().isoformat()
    dimensions = (
        ("members", "lifetime", ProjectQuotaRow.member_limit, config.default_member_limit),
        ("storage_bytes", "lifetime", ProjectQuotaRow.storage_bytes_limit, config.default_storage_bytes_limit),
        ("concurrent_runs", "lifetime", ProjectQuotaRow.concurrent_run_limit, config.default_concurrent_run_limit),
        ("mcp_calls_daily", daily_bucket, ProjectQuotaRow.mcp_calls_daily_limit, config.default_mcp_calls_daily_limit),
    )
    columns: list[ColumnElement[int]] = []
    for dimension, bucket, limit_field, default_limit in dimensions:
        columns.extend(
            (
                _usage_column(
                    project_id,
                    dimension=dimension,
                    bucket=bucket,
                    field=ProjectUsageCounterRow.used,
                    label=f"quota_{dimension}_used",
                ),
                _usage_column(
                    project_id,
                    dimension=dimension,
                    bucket=bucket,
                    field=ProjectUsageCounterRow.reserved,
                    label=f"quota_{dimension}_reserved",
                ),
                _limit_column(
                    project_id,
                    field=limit_field,
                    default=default_limit,
                    label=f"quota_{dimension}_limit",
                ),
            )
        )
    return tuple(columns)


def project_quota_summary_from_row(row: object) -> ProjectQuotaSummary:
    def dimension(name: str) -> QuotaDimensionSummary:
        return QuotaDimensionSummary(
            used=int(getattr(row, f"quota_{name}_used")),
            reserved=int(getattr(row, f"quota_{name}_reserved")),
            limit=int(getattr(row, f"quota_{name}_limit")),
        )

    return ProjectQuotaSummary(
        members=dimension("members"),
        storage_bytes=dimension("storage_bytes"),
        concurrent_runs=dimension("concurrent_runs"),
        mcp_calls_daily=dimension("mcp_calls_daily"),
    )


async def load_project_quota_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    config: QuotaConfig,
    *,
    now: datetime | None = None,
) -> ProjectQuotaSummary:
    columns = project_quota_summary_columns(literal(project_id), config, now=now)
    row = (await session.execute(select(*columns))).one()
    return project_quota_summary_from_row(row)


__all__ = [
    "load_project_quota_summary",
    "project_quota_summary_columns",
    "project_quota_summary_from_row",
]
