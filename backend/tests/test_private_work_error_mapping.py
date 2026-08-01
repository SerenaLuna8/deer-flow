from __future__ import annotations

import pytest

from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkDefaultAgentUnavailable,
    PrivateWorkError,
    PrivateWorkForbidden,
    PrivateWorkInvalid,
    PrivateWorkMcpQuotaExceeded,
    PrivateWorkNotFound,
    PrivateWorkRunQuotaExceeded,
    PrivateWorkStorageQuotaExceeded,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)


@pytest.mark.parametrize(
    ("error_type", "status", "code", "message"),
    [
        (PrivateWorkNotFound, 404, "PRIVATE_WORK_NOT_FOUND", "Private work was not found."),
        (PrivateWorkForbidden, 403, "PRIVATE_WORK_FORBIDDEN", "Private work action is forbidden."),
        (PrivateWorkConflict, 409, "PRIVATE_WORK_CONFLICT", "Private work conflict."),
        (PrivateWorkAssetStale, 409, "PRIVATE_WORK_ASSET_STALE", "Private work asset is stale."),
        (
            PrivateWorkDefaultAgentUnavailable,
            409,
            "DEFAULT_AGENT_UNAVAILABLE",
            "The project default Agent is unavailable.",
        ),
        (PrivateWorkTooLarge, 413, "PRIVATE_WORK_TOO_LARGE", "Private work payload is too large."),
        (PrivateWorkStorageQuotaExceeded, 429, "PROJECT_STORAGE_QUOTA_EXCEEDED", "Project storage quota was exceeded."),
        (PrivateWorkRunQuotaExceeded, 429, "PROJECT_RUN_QUOTA_EXCEEDED", "Project concurrent Run quota was exceeded."),
        (PrivateWorkMcpQuotaExceeded, 429, "PROJECT_MCP_QUOTA_EXCEEDED", "Project MCP call quota was exceeded."),
        (PrivateWorkInvalid, 422, "PRIVATE_WORK_INVALID", "Private work request is invalid."),
        (PrivateWorkUnavailable, 503, "PRIVATE_WORK_UNAVAILABLE", "Private work is unavailable."),
    ],
)
def test_private_work_error_mapping_is_stable_and_redacted(error_type: type[Exception], status: int, code: str, message: str) -> None:
    error = error_type("req-123")
    error.__cause__ = RuntimeError("postgresql://secret@host/db /private/file provider-secret")

    mapped = private_work_http_exception(error)

    assert mapped.status_code == status
    assert mapped.detail == {"code": code, "message": message, "request_id": "req-123"}
    if status in {429, 503}:
        assert mapped.headers == {"Retry-After": "1"}
    else:
        assert mapped.headers is None
    rendered = repr(mapped.detail)
    assert "postgresql" not in rendered
    assert "private/file" not in rendered
    assert "provider-secret" not in rendered


def test_private_work_error_mapper_rejects_forged_error_subclasses() -> None:
    class ForgedPrivateWorkError(PrivateWorkError):
        code = "PRIVATE_WORK_NOT_FOUND"
        public_message = "leak provider-secret"

    with pytest.raises(TypeError, match="unsupported private work error"):
        private_work_http_exception(ForgedPrivateWorkError("req-forged"))
