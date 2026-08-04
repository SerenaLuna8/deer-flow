"""Run the compact backend core suite with PostgreSQL and zero skips."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import pytest
from sqlalchemy.engine import make_url


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("DEER_FLOW_CORE_REQUIRE_ZERO_SKIPS") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {})
    passed = len(stats.get("passed", ()))
    failed = len(stats.get("failed", ())) + len(stats.get("error", ())) + len(stats.get("errors", ()))
    skipped = len(stats.get("skipped", ()))
    reporter.write_line(f"backend core stats: collected={session.testscollected} passed={passed} failed={failed} skipped={skipped}")
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    raw_url = os.getenv("POSTGRES_TEST_URL", "").strip()
    if not raw_url:
        print("POSTGRES_TEST_URL is required for the backend core suite", file=sys.stderr)
        return int(pytest.ExitCode.USAGE_ERROR)
    try:
        parsed_url = make_url(raw_url)
    except Exception:
        parsed_url = None
    if parsed_url is None or parsed_url.get_backend_name() != "postgresql" or parsed_url.database != "postgres":
        print("POSTGRES_TEST_URL must target the PostgreSQL maintenance database named postgres", file=sys.stderr)
        return int(pytest.ExitCode.USAGE_ERROR)
    os.environ["DEER_FLOW_CORE_REQUIRE_ZERO_SKIPS"] = "1"
    return int(pytest.main(list(argv) if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
