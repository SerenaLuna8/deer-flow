from __future__ import annotations

import uuid
from typing import Literal

import pytest

from app.gateway.routers.admin_jobs import _response
from app.reliability.operations import AdminJobRecord


@pytest.mark.parametrize("terminal_status", ["succeeded", "cancelled"])
def test_admin_job_projection_hides_legacy_error_on_non_failure_terminal_status(
    terminal_status: Literal["succeeded", "cancelled"],
) -> None:
    record = AdminJobRecord(
        job_id=uuid.uuid4(),
        dead_job_id=None,
        project_id=uuid.uuid4(),
        project_slug="memory-project",
        project_display_name="Memory Project",
        job_type="memory_dream",
        status=terminal_status,
        retry_safety="safe",
        safe_to_requeue=False,
        public_error_code="MEMORY_DREAM_MODEL_FAILED",
        predecessor_dead_job_id=None,
    )

    assert _response(record).public_error_code is None


def test_admin_job_projection_keeps_error_on_failed_status() -> None:
    record = AdminJobRecord(
        job_id=uuid.uuid4(),
        dead_job_id=None,
        project_id=uuid.uuid4(),
        project_slug="memory-project",
        project_display_name="Memory Project",
        job_type="memory_dream",
        status="failed",
        retry_safety="safe",
        safe_to_requeue=False,
        public_error_code="MEMORY_DREAM_MODEL_FAILED",
        predecessor_dead_job_id=None,
    )

    assert _response(record).public_error_code == "MEMORY_DREAM_MODEL_FAILED"
