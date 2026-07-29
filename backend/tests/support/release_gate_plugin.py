"""Pytest hook that makes a release gate fail when any test is skipped."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import pytest

_LABELS = frozenset({"M1-M7"})


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("DEER_FLOW_REQUIRE_ZERO_SKIPS") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {})
    passed = len(stats.get("passed", ()))
    failed = len(stats.get("failed", ())) + len(stats.get("error", ())) + len(stats.get("errors", ()))
    skipped = len(stats.get("skipped", ()))
    label = os.environ["DEER_FLOW_RELEASE_GATE_LABEL"]
    reporter.write_line(f"{label} release stats: collected={session.testscollected} passed={passed} failed={failed} skipped={skipped}")
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    if not os.getenv("POSTGRES_TEST_URL", "").strip():
        print("POSTGRES_TEST_URL is required for the PostgreSQL release gate", file=sys.stderr)
        return int(pytest.ExitCode.USAGE_ERROR)
    if os.getenv("DEER_FLOW_RELEASE_GATE_LABEL") not in _LABELS:
        print("DEER_FLOW_RELEASE_GATE_LABEL must be M1-M7", file=sys.stderr)
        return int(pytest.ExitCode.USAGE_ERROR)
    os.environ["DEER_FLOW_REQUIRE_ZERO_SKIPS"] = "1"
    return int(pytest.main(list(argv) if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
