from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.projects.quota_summary import project_quota_summary_columns, project_quota_summary_from_row
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.projects.model import ProjectRow


def test_project_quota_summary_uses_all_authoritative_dimensions() -> None:
    row = SimpleNamespace(
        quota_members_used=3,
        quota_members_reserved=1,
        quota_members_limit=20,
        quota_storage_bytes_used=1_024,
        quota_storage_bytes_reserved=512,
        quota_storage_bytes_limit=5_368_709_120,
        quota_concurrent_runs_used=1,
        quota_concurrent_runs_reserved=1,
        quota_concurrent_runs_limit=3,
        quota_mcp_calls_daily_used=25,
        quota_mcp_calls_daily_reserved=5,
        quota_mcp_calls_daily_limit=10_000,
    )

    summary = project_quota_summary_from_row(row)

    assert (summary.members.used, summary.members.reserved, summary.members.limit) == (3, 1, 20)
    assert summary.storage_bytes.reserved == 512
    assert summary.concurrent_runs.limit == 3
    assert summary.mcp_calls_daily.used == 25


def test_project_quota_summary_query_uses_current_utc_daily_bucket_and_deployment_defaults() -> None:
    columns = project_quota_summary_columns(
        ProjectRow.id,
        QuotaConfig(),
        now=datetime(2026, 7, 22, 1, 0, tzinfo=UTC),
    )
    statement = select(ProjectRow.id, *columns)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert "2026-07-22" in sql
    assert "5368709120" in sql
    assert "10000" in sql
    assert len(columns) == 12


def test_project_quota_summary_rejects_naive_daily_bucket_time() -> None:
    try:
        project_quota_summary_columns(ProjectRow.id, QuotaConfig(), now=datetime(2026, 7, 22, 1, 0))
    except ValueError as exc:
        assert str(exc) == "quota summary time must be timezone aware"
    else:
        raise AssertionError("naive quota summary time was accepted")
