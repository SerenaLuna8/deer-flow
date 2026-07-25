"""Bound Skill-version archive requests before FastAPI parses their JSON body."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from deerflow.trace_context import generate_trace_id, get_current_trace_id

# A maximum 100 MiB decoded archive expands to just over 133 MiB in base64.
# 160 MiB leaves bounded room for the 4,096 JSON file entries, paths, media
# types, and envelope while still placing a hard ceiling on request memory.
SKILL_VERSION_REQUEST_BODY_LIMIT_BYTES = 160 * 1024 * 1024

_ERROR_CODE = "skill_version_request_body_too_large"
_ERROR_MESSAGE = "Skill version request body is too large."


def _is_skill_version_archive_request(scope: Scope) -> bool:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False

    segments = str(scope.get("path", "")).strip("/").split("/")
    return (len(segments) == 6 and segments[0:2] == ["api", "projects"] and segments[3] == "skills" and segments[5] == "versions") or (
        len(segments) == 8 and segments[0:3] == ["api", "admin", "projects"] and segments[4:6] == ["assets", "skills"] and segments[7] == "versions"
    )


def _declared_body_exceeds_limit(scope: Scope, limit: int) -> bool:
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() != b"content-length":
            continue
        try:
            declared = int(raw_value)
        except (TypeError, ValueError):
            continue
        if declared > limit:
            return True
    return False


def _too_large_response() -> JSONResponse:
    request_id = get_current_trace_id() or generate_trace_id()
    return JSONResponse(
        status_code=413,
        content={
            "detail": {
                "code": _ERROR_CODE,
                "message": _ERROR_MESSAGE,
                "request_id": request_id,
            }
        },
        headers={"Cache-Control": "no-store"},
    )


class SkillVersionRequestBodyLimitMiddleware:
    """Apply a wire-byte ceiling only to Skill archive creation routes.

    ``Content-Length`` is rejected without reading the body. For HTTP/1.1
    chunked or otherwise undeclared bodies, the receive wrapper counts bytes
    and stops forwarding as soon as the cumulative limit is crossed.

    Downstream response messages are held until the bounded request finishes.
    This lets the middleware replace FastAPI's truncated-JSON parse response
    with a deterministic 413 without ever sending two response starts.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = SKILL_VERSION_REQUEST_BODY_LIMIT_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_skill_version_archive_request(scope):
            await self.app(scope, receive, send)
            return

        if _declared_body_exceeds_limit(scope, self.max_body_bytes):
            await _too_large_response()(scope, receive, send)
            return

        received_bytes = 0
        exceeded = False
        downstream_messages: list[Message] = []

        async def limited_receive() -> Message:
            nonlocal exceeded, received_bytes
            if exceeded:
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }

            message = await receive()
            if message["type"] != "http.request":
                return message

            body = message.get("body", b"")
            received_bytes += len(body)
            if received_bytes > self.max_body_bytes:
                exceeded = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            return message

        async def hold_response(message: Message) -> None:
            downstream_messages.append(message)

        try:
            await self.app(scope, limited_receive, hold_response)
        except Exception:
            if not exceeded:
                raise

        if exceeded:
            await _too_large_response()(scope, receive, send)
            return

        for message in downstream_messages:
            await send(message)
