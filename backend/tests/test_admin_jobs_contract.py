from __future__ import annotations

import uuid

from app.gateway.routers.admin_jobs import _response
from app.reliability.operations import AdminJobRecord


def test_admin_job_response_exposes_human_readable_project_metadata() -> None:
    project_id = uuid.uuid4()
    record = AdminJobRecord(
        job_id=uuid.uuid4(),
        dead_job_id=None,
        project_id=project_id,
        project_slug="alpha-project",
        project_display_name="Alpha Project",
        job_type="private_run",
        status="succeeded",
        retry_safety="unknown",
        safe_to_requeue=False,
        public_error_code=None,
        predecessor_dead_job_id=None,
    )

    body = _response(record).model_dump(mode="json")

    assert body["project_id"] == str(project_id)
    assert body["project_slug"] == "alpha-project"
    assert body["project_display_name"] == "Alpha Project"
