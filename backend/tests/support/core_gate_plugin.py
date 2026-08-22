"""Run the compact backend core suite with PostgreSQL and zero skips."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def validate_development_database_url(raw_url: str) -> str:
    """Validate the development ``DATABASE_URL`` without exposing credentials."""

    normalized = raw_url.strip()
    if not normalized:
        raise ValueError("DATABASE_URL is required for the backend core suite")
    try:
        parsed_url = make_url(normalized)
    except Exception:
        parsed_url = None
    if parsed_url is None or parsed_url.get_backend_name() != "postgresql" or not parsed_url.database:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL with a database name")
    return normalized


def resolve_development_database_url(
    *,
    environment: Mapping[str, str] | None = None,
    env_file: Path = REPOSITORY_ENV_FILE,
) -> str:
    """Resolve only ``DATABASE_URL`` from the caller or root development env."""

    source = os.environ if environment is None else environment
    raw_url = source.get("DATABASE_URL", "").strip()
    if not raw_url and env_file.is_file():
        value = dotenv_values(env_file).get("DATABASE_URL")
        raw_url = value.strip() if isinstance(value, str) else ""
    return validate_development_database_url(raw_url)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("ACT_WEAVE_CORE_REQUIRE_ZERO_SKIPS") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {})
    passed = len(stats.get("passed", ()))
    failed = len(stats.get("failed", ())) + len(stats.get("error", ())) + len(stats.get("errors", ()))
    skipped = len(stats.get("skipped", ()))
    reporter.write_line(f"backend core stats: collected={session.testscollected} passed={passed} failed={failed} skipped={skipped}")
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def main(
    argv: Sequence[str] | None = None,
    *,
    env_file: Path = REPOSITORY_ENV_FILE,
) -> int:
    try:
        database_url = resolve_development_database_url(env_file=env_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return int(pytest.ExitCode.USAGE_ERROR)
    # Pass only the selected development connection into pytest. conftest
    # captures it before replacing the application-facing value with a safe
    # nonexistent target; no other root .env values are exported to pytest.
    os.environ["DATABASE_URL"] = database_url
    os.environ["ACT_WEAVE_CORE_REQUIRE_ZERO_SKIPS"] = "1"
    return int(pytest.main(list(argv) if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
