from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router


class _FileService:
    def __init__(self, *, deleted: bool) -> None:
        self.deleted = deleted
        self.calls: list[dict[str, object]] = []

    async def delete_ready(self, context, **kwargs):
        self.calls.append({"context": context, **kwargs})
        return object() if self.deleted else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("only_if_unreferenced", "deleted"),
    [(False, True), (True, False)],
)
async def test_delete_upload_reports_conditional_discard_outcome(
    monkeypatch: pytest.MonkeyPatch,
    only_if_unreferenced: bool,
    deleted: bool,
) -> None:
    app = FastAPI()
    app.include_router(private_work_router.router)
    context = SimpleNamespace(request_id="upload-discard-api")
    service = _FileService(deleted=deleted)
    app.dependency_overrides[private_work_context] = lambda: context
    app.dependency_overrides[require_project_private_open] = lambda: None
    monkeypatch.setattr(
        private_work_router,
        "_file_service",
        lambda _request, _request_id: service,
    )

    project_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    file_id = uuid.uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.delete(
            f"/api/projects/{project_id}/private-work/threads/{thread_id}/uploads",
            params={
                "file_id": str(file_id),
                "only_if_unreferenced": str(only_if_unreferenced).lower(),
            },
        )

    assert response.status_code == 200
    assert response.json() == {"success": True, "deleted": deleted}
    assert service.calls == [
        {
            "context": context,
            "thread_id": str(thread_id),
            "file_id": file_id,
            "only_if_unreferenced": only_if_unreferenced,
        }
    ]
