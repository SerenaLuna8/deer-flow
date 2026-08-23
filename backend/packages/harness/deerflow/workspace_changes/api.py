from __future__ import annotations

from typing import Any

from deerflow.runtime.private_scope import PrivateResourceScope

from .types import WORKSPACE_CHANGES_EVENT_TYPE, WORKSPACE_CHANGES_METADATA_KEY

EMPTY_SUMMARY = {
    "created": 0,
    "modified": 0,
    "deleted": 0,
    "additions": 0,
    "deletions": 0,
    "truncated": False,
}
_LEGACY_PATH_ONLY_FILE_KEYS = frozenset(
    {
        "path",
        "root",
        "status",
        "binary",
        "sensitive",
        "size_before",
        "size_after",
        "sha256_before",
        "sha256_after",
        "diff",
        "diff_truncated",
        "diff_unavailable_reason",
        "additions",
        "deletions",
    }
)


async def get_workspace_changes_response(
    event_store: Any,
    thread_id: str,
    run_id: str,
    *,
    include_files: bool = True,
    include_diff: bool = True,
    scope: PrivateResourceScope | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "event_types": [WORKSPACE_CHANGES_EVENT_TYPE],
        "limit": 10,
    }
    if scope is not None:
        query["scope"] = scope
    events = await event_store.list_events(thread_id, run_id, **query)
    if not events:
        return _empty_response()

    payload = _extract_workspace_changes_payload(events[-1])
    if not isinstance(payload, dict):
        return _empty_response()

    response = _upgrade_legacy_path_only_payload(payload)
    response["available"] = True
    response.setdefault("summary", dict(EMPTY_SUMMARY))
    if include_files:
        response.setdefault("files", [])
        if not include_diff:
            response["files"] = [_without_diff(file) for file in response["files"]]
    else:
        response["files"] = []
    return response


def _upgrade_legacy_path_only_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Widen only the exact historical path-list adapter from v1 to v2.

    That adapter had no file bytes or metadata from which it could calculate
    line counts, but serialized the dataclass defaults as ``+0 -0``. A real v1
    snapshot always carries at least one size/hash for every changed file, so
    the strict all-null signature below does not rewrite a known empty-text
    zero.
    """

    summary = payload.get("summary")
    files = payload.get("files")
    if (
        payload.get("version") != 1
        or not isinstance(summary, dict)
        or set(summary) != set(EMPTY_SUMMARY)
        or not isinstance(files, list)
        or not files
        or type(summary.get("additions")) is not int
        or summary.get("additions") != 0
        or type(summary.get("deletions")) is not int
        or summary.get("deletions") != 0
        or summary.get("truncated") is not False
    ):
        return dict(payload)

    status_counts = {"created": 0, "modified": 0, "deleted": 0}
    upgraded_files: list[dict[str, Any]] = []
    paths: set[str] = set()
    for raw_file in files:
        if not isinstance(raw_file, dict) or set(raw_file) != _LEGACY_PATH_ONLY_FILE_KEYS:
            return dict(payload)
        status = raw_file.get("status")
        root = raw_file.get("root")
        path = raw_file.get("path")
        sensitive = raw_file.get("sensitive")
        if (
            type(status) is not str
            or status not in status_counts
            or type(root) is not str
            or root not in {"workspace", "outputs"}
            or type(path) is not str
            or not path.startswith(f"/mnt/user-data/{root}/")
            or path in paths
            or raw_file.get("binary") is not False
            or type(sensitive) is not bool
            or any(
                raw_file.get(key) is not None
                for key in (
                    "size_before",
                    "size_after",
                    "sha256_before",
                    "sha256_after",
                )
            )
            or raw_file.get("diff") != ""
            or raw_file.get("diff_truncated") is not False
            or raw_file.get("diff_unavailable_reason") != ("sensitive" if sensitive else None)
            or type(raw_file.get("additions")) is not int
            or raw_file.get("additions") != 0
            or type(raw_file.get("deletions")) is not int
            or raw_file.get("deletions") != 0
        ):
            return dict(payload)
        paths.add(path)
        status_counts[status] += 1
        upgraded = dict(raw_file)
        upgraded["additions"] = None
        upgraded["deletions"] = None
        if not sensitive:
            upgraded["diff_unavailable_reason"] = "unavailable"
        upgraded_files.append(upgraded)

    if any(type(summary.get(status)) is not int or summary.get(status) != status_counts[status] for status in status_counts):
        return dict(payload)

    upgraded_payload = dict(payload)
    upgraded_payload["version"] = 2
    upgraded_summary = dict(summary)
    upgraded_summary["additions"] = None
    upgraded_summary["deletions"] = None
    upgraded_payload["summary"] = upgraded_summary
    upgraded_payload["files"] = upgraded_files
    return upgraded_payload


def _empty_response() -> dict[str, Any]:
    return {
        "available": False,
        "version": 2,
        "summary": dict(EMPTY_SUMMARY),
        "files": [],
        "limits": {},
    }


def _extract_workspace_changes_payload(event: dict[str, Any]) -> Any:
    metadata = event.get("metadata") or {}
    if isinstance(metadata, dict) and WORKSPACE_CHANGES_METADATA_KEY in metadata:
        return metadata[WORKSPACE_CHANGES_METADATA_KEY]
    content = event.get("content")
    if isinstance(content, dict):
        return content
    return None


def _without_diff(file: Any) -> Any:
    if not isinstance(file, dict):
        return file
    sanitized = dict(file)
    sanitized["diff"] = ""
    return sanitized
