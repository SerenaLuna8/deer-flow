"""Authenticated attachment HTTP dispatch, byte response, and tempfile lifetime."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from actweave_knowledge import KNOWLEDGE_CONFLICT, KNOWLEDGE_NOT_FOUND, KnowledgeError
from fastapi import FastAPI

from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class AttachmentModule:
    def __init__(self):
        self.project_id, self.actor_id = uuid4(), uuid4()
        self.base_id, self.document_id, self.segment_id, self.attachment_id = (uuid4() for _ in range(4))
        self.calls = []
        self.paths = []
        self.error = None

    async def _download(self, kind, scope, target_path, expected):
        self.calls.append((kind, scope, expected))
        self.paths.append(target_path)
        target_path.write_bytes(b"image-content")
        if self.error:
            raise self.error
        return SimpleNamespace(media_type="image/png", size_bytes=13)

    async def download_segment_attachment(self, project_id, document_id, segment_id, attachment_id, target_path, **expected):
        return await self._download("managed", (project_id, document_id, segment_id, attachment_id), target_path, expected)

    async def download_citation_attachment(self, project_id, base_id, document_id, segment_id, attachment_id, target_path, **expected):
        return await self._download("citation", (project_id, base_id, document_id, segment_id, attachment_id), target_path, expected)

    def url(self, kind):
        base = f"/bases/{self.base_id}" if kind == "citation" else ""
        return f"/api/projects/{self.project_id}/knowledge{base}/documents/{self.document_id}/segments/{self.segment_id}/attachments/{self.attachment_id}"


def app_for(module):
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(user_id=module.actor_id, project_id=module.project_id, membership_id=uuid4(), role=ProjectRole.ADMIN, capabilities=frozenset(Capability), membership_version=1, request_id="attachment-read")
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.state.knowledge_module = module
    return app


EXPECTED = {"expected_document_version": 1, "expected_content_digest": "a" * 64}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
async def test_attachment_http_uses_distinct_entrypoints_and_private_bytes(kind):
    module = AttachmentModule()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(module)), base_url="http://test") as client:
        response = await client.get(module.url(kind), params=EXPECTED | {"manage": "true"})
    assert response.status_code == 200
    assert response.content == b"image-content"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    selected, scope, expected = module.calls[0]
    assert selected == kind
    assert scope == ((module.project_id, module.base_id) if kind == "citation" else (module.project_id,)) + (module.document_id, module.segment_id, module.attachment_id)
    authority = expected.pop("authority")
    assert authority.project_id == module.project_id
    assert authority.actor_user_id == module.actor_id
    assert expected == EXPECTED
    assert all(not path.exists() for path in module.paths)
    for secret in ("projects/", "bucket", "storage_key", "Signature", str(module.paths[0])):
        assert secret not in str(response.headers) + response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
@pytest.mark.parametrize("params", [{}, {"expected_document_version": 1}, {"expected_content_digest": "a" * 64}, EXPECTED | {"expected_document_version": 0}, EXPECTED | {"expected_content_digest": "invalid"}])
async def test_attachment_http_requires_valid_expectations(kind, params):
    module = AttachmentModule()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(module)), base_url="http://test") as client:
        response = await client.get(module.url(kind), params=params)
    assert response.status_code == 422
    assert module.paths == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
@pytest.mark.parametrize("code,status", [(KNOWLEDGE_NOT_FOUND, 404), (KNOWLEDGE_CONFLICT, 409)])
async def test_attachment_http_removes_partial_output_on_safe_error(kind, code, status):
    module = AttachmentModule()
    module.error = KnowledgeError(code, "资源不存在或已变化")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(module)), base_url="http://test") as client:
        response = await client.get(module.url(kind), params=EXPECTED)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert len(module.paths) == 1 and not module.paths[0].exists()
    for secret in ("projects/", "bucket", "storage_key", "Signature", str(module.paths[0])):
        assert secret not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
async def test_attachment_http_cancellation_removes_partial_output(kind):
    module = AttachmentModule()
    module.error = asyncio.CancelledError()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(module)), base_url="http://test") as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get(module.url(kind), params=EXPECTED)
    assert len(module.paths) == 1 and not module.paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
async def test_attachment_http_disconnect_removes_response_file(kind):
    module = AttachmentModule()
    app = app_for(module)
    request = httpx.Request("GET", "http://test" + module.url(kind), params=EXPECTED)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": request.url.path,
        "raw_path": request.url.raw_path.split(b"?")[0],
        "query_string": request.url.query,
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await app(scope, receive, send)
    assert len(module.paths) == 1 and not module.paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
async def test_attachment_http_uses_live_membership_and_capability_admission(postgres_database_url, monkeypatch, kind):
    from extraction_test_helpers import extraction_harness
    from sqlalchemy import text

    from app.projects.capabilities import PROJECT_ROLE_CAPABILITIES

    async with extraction_harness(postgres_database_url) as h:
        module = AttachmentModule()
        module.project_id = h.project_id
        app = app_for(module)
        del app.dependency_overrides[gateway.require_project_knowledge_read]

        async def session_dependency():
            async with h.session_factory() as session:
                yield session

        app.dependency_overrides[gateway.project_session] = session_dependency
        app.dependency_overrides[gateway._authenticated_identity] = lambda: (module.actor_id, "attachment-read")
        async with h.session_factory() as session, session.begin():
            user_id = await session.scalar(text("SELECT created_by_user_id FROM projects WHERE id=:id"), {"id": h.project_id})
            from uuid import UUID

            module.actor_id = UUID(user_id)
            membership_id = uuid4()
            await session.execute(text("INSERT INTO project_memberships (id,project_id,user_id,role,status,version) VALUES (:id,:project,:user,'viewer','active',1)"), {"id": membership_id, "project": h.project_id, "user": user_id})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            allowed = await client.get(module.url(kind), params=EXPECTED)
            assert allowed.status_code == 200
            # Current roles all include read. Simulate the host capability
            # policy removing it while retaining real membership resolution.
            monkeypatch.setitem(PROJECT_ROLE_CAPABILITIES, ProjectRole.VIEWER, frozenset())
            forbidden = await client.get(module.url(kind), params=EXPECTED)
            assert forbidden.status_code == 403
            assert forbidden.json()["detail"]["code"] == "KNOWLEDGE_FORBIDDEN"
            async with h.session_factory() as session, session.begin():
                await session.execute(text("UPDATE project_memberships SET status='removed',version=version+1 WHERE id=:id"), {"id": membership_id})
            revoked = await client.get(module.url(kind), params=EXPECTED)
            assert revoked.status_code == 404
            module.actor_id = uuid4()
            outsider = await client.get(module.url(kind), params=EXPECTED)
            assert outsider.status_code == 404
        assert len(module.paths) == 1  # no copy ever begins for rejected callers
