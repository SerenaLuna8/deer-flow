from __future__ import annotations

import pytest

from deerflow.workspace_changes.api import get_workspace_changes_response
from deerflow.workspace_changes.diff import compare_snapshots
from deerflow.workspace_changes.recorder import (
    trusted_workspace_change_result,
    workspace_change_event_content,
)
from deerflow.workspace_changes.types import (
    FileSnapshot,
    WorkspaceChangeResult,
    WorkspaceChangeSummary,
    WorkspaceFileChange,
    WorkspaceSnapshot,
)


def test_private_finalization_preserves_authoritative_workspace_change_result() -> None:
    result = WorkspaceChangeResult(
        summary=WorkspaceChangeSummary(created=1, additions=3),
        files=[
            WorkspaceFileChange(
                path="/mnt/user-data/outputs/report.md",
                root="outputs",
                status="created",
                binary=False,
                sensitive=False,
                size_before=None,
                size_after=18,
                sha256_before=None,
                sha256_after="a" * 64,
                diff="--- a/mnt/user-data/outputs/report.md\n+++ b/mnt/user-data/outputs/report.md",
                additions=3,
                deletions=0,
            )
        ],
    )

    assert trusted_workspace_change_result(result) is result
    assert workspace_change_event_content(result) == "1 file changed +3 -0"


def test_legacy_private_change_paths_report_unknown_instead_of_fake_zero() -> None:
    result = trusted_workspace_change_result(
        {
            "created": ["outputs/report.bin"],
            "modified": [],
            "deleted": [],
        }
    )

    assert result is not None
    assert result.summary.additions is None
    assert result.summary.deletions is None
    assert result.files[0].additions is None
    assert result.files[0].deletions is None
    assert result.files[0].diff_unavailable_reason == "unavailable"
    assert workspace_change_event_content(result) == ("1 file changed (line counts unavailable)")


@pytest.mark.parametrize(
    ("reason", "binary", "sensitive"),
    [
        ("binary", True, False),
        ("large", False, False),
        ("sensitive", False, True),
    ],
)
def test_unavailable_file_content_has_unknown_not_zero_line_counts(
    reason: str,
    binary: bool,
    sensitive: bool,
) -> None:
    path = "/mnt/user-data/outputs/report.dat"
    result = compare_snapshots(
        WorkspaceSnapshot(),
        WorkspaceSnapshot(
            files={
                path: FileSnapshot(
                    path=path,
                    root="outputs",
                    size=10,
                    mtime_ns=0,
                    sha256="a" * 64,
                    binary=binary,
                    sensitive=sensitive,
                    content_unavailable_reason=reason,  # type: ignore[arg-type]
                )
            }
        ),
    )

    assert result.version == 2
    assert result.summary.additions is None
    assert result.summary.deletions is None
    assert result.files[0].additions is None
    assert result.files[0].deletions is None
    assert result.files[0].diff_unavailable_reason == reason
    if sensitive:
        assert result.files[0].sha256_before is None
        assert result.files[0].sha256_after is None


def test_known_empty_text_change_keeps_truthful_zero_line_counts() -> None:
    path = "/mnt/user-data/outputs/empty.md"
    result = compare_snapshots(
        WorkspaceSnapshot(),
        WorkspaceSnapshot(
            files={
                path: FileSnapshot(
                    path=path,
                    root="outputs",
                    size=0,
                    mtime_ns=0,
                    sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    text="",
                )
            }
        ),
    )

    assert result.summary.additions == 0
    assert result.summary.deletions == 0
    assert result.files[0].additions == 0
    assert result.files[0].deletions == 0


def test_sensitive_same_size_change_uses_internal_hash_but_redacts_it() -> None:
    path = "/mnt/user-data/workspace/.env"
    before = FileSnapshot(
        path=path,
        root="workspace",
        size=8,
        mtime_ns=0,
        sha256="a" * 64,
        sensitive=True,
        content_unavailable_reason="sensitive",
    )
    after = FileSnapshot(
        path=path,
        root="workspace",
        size=8,
        mtime_ns=0,
        sha256="b" * 64,
        sensitive=True,
        content_unavailable_reason="sensitive",
    )

    result = compare_snapshots(
        WorkspaceSnapshot(files={path: before}),
        WorkspaceSnapshot(files={path: after}),
    )

    assert result.summary.modified == 1
    assert result.files[0].sha256_before is None
    assert result.files[0].sha256_after is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "summary": {
                "created": 1,
                "modified": 0,
                "deleted": 0,
                "additions": 0,
                "deletions": 0,
                "truncated": False,
            },
            "files": [],
            "limits": {},
        },
        {
            "version": 2,
            "summary": {
                "created": 1,
                "modified": 0,
                "deleted": 0,
                "additions": None,
                "deletions": None,
                "truncated": False,
            },
            "files": [],
            "limits": {},
        },
    ],
)
async def test_workspace_change_api_preserves_v1_numeric_and_v2_nullable_counts(
    payload: dict[str, object],
) -> None:
    class EventStore:
        async def list_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return [{"metadata": {"workspace_changes": payload}}]

    response = await get_workspace_changes_response(
        EventStore(),
        "thread-1",
        "run-1",
    )

    assert response["available"] is True
    assert response["version"] == payload["version"]
    assert response["summary"] == payload["summary"]


@pytest.mark.asyncio
async def test_workspace_change_api_upgrades_historical_path_only_fake_zeros() -> None:
    payload = {
        "version": 1,
        "summary": {
            "created": 1,
            "modified": 0,
            "deleted": 0,
            "additions": 0,
            "deletions": 0,
            "truncated": False,
        },
        "files": [
            {
                "path": "/mnt/user-data/outputs/report.md",
                "root": "outputs",
                "status": "created",
                "binary": False,
                "sensitive": False,
                "size_before": None,
                "size_after": None,
                "sha256_before": None,
                "sha256_after": None,
                "diff": "",
                "diff_truncated": False,
                "diff_unavailable_reason": None,
                "additions": 0,
                "deletions": 0,
            }
        ],
        "limits": {
            "max_files": 200,
            "max_scanned_files": 2000,
            "max_file_bytes_for_diff": 262144,
            "max_total_diff_bytes": 1048576,
        },
    }

    class EventStore:
        async def list_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return [{"metadata": {"workspace_changes": payload}}]

    response = await get_workspace_changes_response(
        EventStore(),
        "thread-1",
        "run-1",
    )

    assert response["version"] == 2
    assert response["summary"]["additions"] is None
    assert response["summary"]["deletions"] is None
    assert response["files"][0]["additions"] is None
    assert response["files"][0]["deletions"] is None
    assert response["files"][0]["diff_unavailable_reason"] == "unavailable"


@pytest.mark.asyncio
async def test_workspace_change_api_does_not_upgrade_real_v1_empty_text_zero() -> None:
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    payload = {
        "version": 1,
        "summary": {
            "created": 1,
            "modified": 0,
            "deleted": 0,
            "additions": 0,
            "deletions": 0,
            "truncated": False,
        },
        "files": [
            {
                "path": "/mnt/user-data/outputs/empty.md",
                "root": "outputs",
                "status": "created",
                "binary": False,
                "sensitive": False,
                "size_before": None,
                "size_after": 0,
                "sha256_before": None,
                "sha256_after": empty_sha256,
                "diff": "",
                "diff_truncated": False,
                "diff_unavailable_reason": None,
                "additions": 0,
                "deletions": 0,
            }
        ],
        "limits": {},
    }

    class EventStore:
        async def list_events(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return [{"metadata": {"workspace_changes": payload}}]

    response = await get_workspace_changes_response(
        EventStore(),
        "thread-1",
        "run-1",
    )

    assert response["version"] == 1
    assert response["summary"]["additions"] == 0
    assert response["summary"]["deletions"] == 0
    assert response["files"][0]["additions"] == 0
    assert response["files"][0]["deletions"] == 0
