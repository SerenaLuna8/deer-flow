"""Smoke test: the strict Blockbuster gate is wired up and actively catching.

Independent of any specific production code path, asserts that calling a
known blocking IO function directly from an `async def` (without an
`asyncio.to_thread` wrapper) raises `BlockingError`. If this test ever
stops raising, the gate machinery itself is broken — typical causes are
`scanned_modules` misconfiguration, accidental removal of the Blockbuster
dev dependency, or the conftest hookwrapper no longer firing.

This is the meta-test that protects every other test in this directory
from silent regressions (a green gate that no longer catches anything is
worse than no gate at all).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from blockbuster import BlockingError
from support.detectors.blocking_io_runtime import detect_blocking_io_strict

pytestmark = pytest.mark.asyncio


async def test_gate_catches_unoffloaded_blocking_io_in_deerflow_module(tmp_path: Path) -> None:
    from deerflow.skills.validation import _validate_skill_frontmatter

    with pytest.raises(BlockingError):
        _validate_skill_frontmatter(tmp_path / "missing-skill")


async def test_gate_restores_blockbuster_patches_after_exceptions() -> None:
    original_stat = os.stat

    with pytest.raises(RuntimeError, match="boom"):
        with detect_blocking_io_strict():
            raise RuntimeError("boom")

    assert os.stat is original_stat


async def test_gate_catches_unoffloaded_blocking_io_in_scheduler_path() -> None:
    from app.scheduler.service import ScheduledTaskService

    async def blocking_reserve_due(**_kwargs) -> None:
        time.sleep(0.01)

    service = ScheduledTaskService(
        app=SimpleNamespace(),
        occurrences=SimpleNamespace(
            reserve_due=blocking_reserve_due,
            claim_next=AsyncMock(return_value=None),
        ),
        dispatcher=SimpleNamespace(dispatch=AsyncMock()),
        reconciler=SimpleNamespace(reconcile_restart=AsyncMock()),
        poll_interval_seconds=60,
        lease_seconds=120,
        max_concurrent_runs=3,
    )

    with pytest.raises(BlockingError):
        await service.run_once(now=service._clock())


@pytest.mark.allow_blocking_io
async def test_allow_blocking_io_marker_opts_out_of_gate(tmp_path: Path) -> None:
    """Verify the @pytest.mark.allow_blocking_io opt-out actually disables the gate."""
    from deerflow.skills.validation import _validate_skill_frontmatter

    valid, message, name = _validate_skill_frontmatter(tmp_path / "missing-skill")
    assert (valid, name) == (False, None)
    assert "not found" in message
