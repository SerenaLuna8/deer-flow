from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def validate_timezone(timezone_name: str) -> str:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    return timezone_name


def normalize_cron_expression(expr: str) -> str:
    parts = [part for part in expr.split() if part]
    if len(parts) != 5:
        raise ValueError("Cron expression must contain exactly 5 fields")
    return " ".join(parts)


def next_run_at(
    schedule_type: str,
    schedule_spec: dict[str, object],
    timezone_name: str,
    *,
    now: datetime,
) -> datetime | None:
    validate_timezone(timezone_name)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    if schedule_type == "once":
        run_at_raw = schedule_spec.get("run_at")
        if not isinstance(run_at_raw, str):
            raise ValueError("once schedule requires run_at")
        run_at = datetime.fromisoformat(run_at_raw)
        if run_at.tzinfo is None:
            # A naive run_at means "wall-clock time in the task's declared
            # timezone", matching how cron schedules interpret it.
            run_at = run_at.replace(tzinfo=ZoneInfo(timezone_name))
        return run_at if run_at > now else None

    if schedule_type == "cron":
        cron_expr = normalize_cron_expression(str(schedule_spec.get("cron", "")))
        zone = ZoneInfo(timezone_name)
        local_now = now.astimezone(zone)
        next_local = croniter(cron_expr, local_now).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=zone)
        return next_local.astimezone(UTC)

    raise ValueError(f"Unsupported schedule_type: {schedule_type}")


def next_scheduled_occurrence(
    schedule_type: str,
    schedule_spec: Mapping[str, object],
    timezone: str,
    *,
    now: datetime,
    coalesce: bool = True,
) -> datetime | None:
    """Return the one future occurrence used by the durable scheduler.

    M5 has a fixed coalescing policy: missed cron ticks are never replayed, so
    both policy branches select the first tick strictly after ``now``.  The
    explicit argument keeps that policy visible at occurrence call sites.
    """

    if not isinstance(coalesce, bool):
        raise ValueError("coalesce must be a boolean")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(schedule_spec, Mapping):
        raise ValueError("schedule_spec must be a mapping")
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("timezone must be a non-empty string")

    validate_timezone(timezone)
    now_utc = now.astimezone(UTC)
    zone = ZoneInfo(timezone)

    if schedule_type == "once":
        if set(schedule_spec) != {"run_at"}:
            raise ValueError("once schedule requires only run_at")
        run_at_raw = schedule_spec.get("run_at")
        if not isinstance(run_at_raw, str) or not run_at_raw:
            raise ValueError("once schedule requires run_at")
        try:
            run_at = datetime.fromisoformat(run_at_raw)
        except ValueError:
            raise ValueError("once schedule run_at is invalid") from None
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=zone)
        run_at_utc = run_at.astimezone(UTC)
        return run_at_utc if run_at_utc > now_utc else None

    if schedule_type == "cron":
        if set(schedule_spec) != {"cron"}:
            raise ValueError("cron schedule requires only cron")
        cron_raw = schedule_spec.get("cron")
        if not isinstance(cron_raw, str):
            raise ValueError("cron schedule requires cron")
        cron_expr = normalize_cron_expression(cron_raw)
        local_now = now_utc.astimezone(zone)
        iterator = croniter(cron_expr, local_now)
        while True:
            next_local = iterator.get_next(datetime)
            if next_local.tzinfo is None:
                next_local = next_local.replace(tzinfo=zone)
            candidate = next_local.astimezone(UTC)
            if candidate > now_utc:
                return candidate

    raise ValueError("Unsupported schedule_type")
