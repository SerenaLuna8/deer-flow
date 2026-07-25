from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from app.gateway.app import create_app
from app.gateway.skill_version_body_limit import (
    SKILL_VERSION_REQUEST_BODY_LIMIT_BYTES,
    SkillVersionRequestBodyLimitMiddleware,
)

PROJECT_SKILL_VERSION_PATH = "/api/projects/11111111-1111-1111-1111-111111111111/skills/22222222-2222-2222-2222-222222222222/versions"
ADMIN_PROJECT_SKILL_VERSION_PATH = "/api/admin/projects/11111111-1111-1111-1111-111111111111/assets/skills/22222222-2222-2222-2222-222222222222/versions"


def _scope(
    path: str,
    *,
    method: str = "POST",
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _drive(
    path: str,
    request_messages: list[Message],
    *,
    method: str = "POST",
    headers: Iterable[tuple[bytes, bytes]] = (),
    limit: int = 10,
) -> tuple[list[Message], bytearray]:
    downstream_body = bytearray()
    incoming = iter(request_messages)

    async def receive() -> Message:
        return next(incoming)

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            downstream_body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = SkillVersionRequestBodyLimitMiddleware(
        downstream,
        max_body_bytes=limit,
    )
    await middleware(_scope(path, method=method, headers=headers), receive, send)
    return sent, downstream_body


@pytest.mark.parametrize(
    "path",
    [PROJECT_SKILL_VERSION_PATH, ADMIN_PROJECT_SKILL_VERSION_PATH],
)
def test_declared_oversized_skill_version_body_is_rejected_before_downstream(
    path: str,
) -> None:
    sent, downstream_body = asyncio.run(
        _drive(
            path,
            [{"type": "http.request", "body": b"must-not-be-read"}],
            headers=[(b"content-length", b"11")],
        )
    )

    assert downstream_body == b""
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    payload = json.loads(sent[1]["body"])
    assert payload["detail"]["code"] == "skill_version_request_body_too_large"
    assert payload["detail"]["message"] == "Skill version request body is too large."
    assert payload["detail"]["request_id"]


def test_chunked_oversized_skill_version_body_is_bounded_and_replaced_with_413() -> None:
    sent, downstream_body = asyncio.run(
        _drive(
            PROJECT_SKILL_VERSION_PATH,
            [
                {
                    "type": "http.request",
                    "body": b"123456",
                    "more_body": True,
                },
                {
                    "type": "http.request",
                    "body": b"789012",
                    "more_body": False,
                },
            ],
        )
    )

    # The chunk that crosses the limit is not forwarded into JSON parsing.
    assert downstream_body == b"123456"
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"]["code"] == ("skill_version_request_body_too_large")


def test_chunked_limit_runs_before_fastapi_json_parsing_and_endpoint_dispatch() -> None:
    endpoint_called = False
    app = FastAPI()
    app.add_middleware(SkillVersionRequestBodyLimitMiddleware, max_body_bytes=10)

    @app.post(PROJECT_SKILL_VERSION_PATH)
    async def create_version(body: dict[str, object]) -> dict[str, bool]:
        nonlocal endpoint_called
        endpoint_called = True
        return {"created": bool(body)}

    async def request() -> httpx.Response:
        async def chunks():
            yield b'{"files":'
            yield b"[{}]}"

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                PROJECT_SKILL_VERSION_PATH,
                content=chunks(),
                headers={"content-type": "application/json"},
            )

    response = asyncio.run(request())

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "skill_version_request_body_too_large"
    assert endpoint_called is False


def test_gateway_app_registers_limit_and_rejects_before_route_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    monkeypatch.delenv("DEER_FLOW_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    app = create_app()

    stack = app.build_middleware_stack()
    current = stack
    body_limit = None
    while hasattr(current, "app"):
        if isinstance(current, SkillVersionRequestBodyLimitMiddleware):
            body_limit = current
            break
        current = current.app

    assert body_limit is not None
    body_limit.max_body_bytes = 10
    app.middleware_stack = stack

    response = TestClient(app).post(
        PROJECT_SKILL_VERSION_PATH,
        content=b'{"files":[{"content_base64":"too-large"}]}',
        headers={"content-type": "application/json"},
    )

    # A failed limit would continue into project/DB dependencies and return a
    # different response; 413 proves the registered ASGI gate ran first.
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "skill_version_request_body_too_large"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/projects/not-a-project/skills/not-a-skill/versions/publish", "POST"),
        (PROJECT_SKILL_VERSION_PATH, "GET"),
        ("/api/projects/example/private-work/files", "POST"),
    ],
)
def test_body_limit_is_scoped_to_skill_version_archive_creation(
    path: str,
    method: str,
) -> None:
    sent, downstream_body = asyncio.run(
        _drive(
            path,
            [{"type": "http.request", "body": b"12345678901"}],
            method=method,
            headers=[(b"content-length", b"11")],
        )
    )

    assert downstream_body == b"12345678901"
    assert sent[0]["status"] == 204


def test_wire_limit_can_carry_a_maximum_decoded_skill_archive() -> None:
    # 100 MiB expands to ceil(n / 3) * 4 bytes in base64. The remaining
    # request allowance covers the bounded JSON file metadata and envelope.
    decoded_archive_bytes = 100 * 1024 * 1024
    encoded_archive_bytes = 4 * ((decoded_archive_bytes + 2) // 3)

    assert SKILL_VERSION_REQUEST_BODY_LIMIT_BYTES == 160 * 1024 * 1024
    assert SKILL_VERSION_REQUEST_BODY_LIMIT_BYTES - encoded_archive_bytes > 20 * 1024 * 1024


def test_all_nginx_entrypoints_match_the_gateway_wire_limit() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    location = "location ~ ^/api/(?:projects/[^/]+/skills/[^/]+/versions|admin/projects/[^/]+/assets/skills/[^/]+/versions)/?$ {"
    for relative_path in (
        "docker/nginx/nginx.conf",
        "docker/nginx/nginx.local.conf",
        "deploy/helm/deer-flow/templates/configmap-nginx.yaml",
    ):
        content = (repo_root / relative_path).read_text()
        lines = content.splitlines()
        location_index = next(index for index, line in enumerate(lines) if line.strip() == location)
        indentation = lines[location_index][: -len(lines[location_index].lstrip())]
        end_index = next(index for index in range(location_index + 1, len(lines)) if lines[index] == f"{indentation}}}")
        assert "client_max_body_size 160M;" in "\n".join(
            lines[location_index:end_index],
        ), relative_path
        assert content.count("client_max_body_size 160M;") == 1, relative_path
