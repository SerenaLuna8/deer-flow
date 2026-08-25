from __future__ import annotations

import inspect

import pytest

from app.private_work import run_skill_writer_cohort as cohort_module
from app.private_work.snapshot_repository import RunSnapshotRepository

pytestmark = pytest.mark.run_skill_writer_cohort_control


def test_production_admission_has_no_cohort_bypass_or_injection_port() -> None:
    constructor = inspect.signature(RunSnapshotRepository.__init__)
    admission_source = inspect.getsource(
        RunSnapshotRepository.create_run_with_snapshot_in_session,
    )
    cohort_source = inspect.getsource(cohort_module)

    assert all("cohort" not in parameter for parameter in constructor.parameters)
    assert "require_active_run_skill_writer_cohort" in admission_source
    assert "PrivateWorkUnavailable" in admission_source
    assert "PYTEST_CURRENT_TEST" not in cohort_source
    assert "os.getenv" not in cohort_source
    assert "support.run_skill_writer_cohort" not in cohort_source
