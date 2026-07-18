"""Pytest hook that makes a release gate fail when any test is skipped."""

from __future__ import annotations

import os

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("DEER_FLOW_REQUIRE_ZERO_SKIPS") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {})
    passed = len(stats.get("passed", ()))
    skipped = len(stats.get("skipped", ()))
    reporter.write_line(f"M1-M6 release stats: collected={session.testscollected} passed={passed} skipped={skipped}")
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
