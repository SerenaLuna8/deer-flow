from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from deerflow.config import get_paths

from .diff import compare_snapshots, get_changed_paths
from .scanner import is_sensitive_workspace_path, scan_workspace_roots
from .types import (
    WORKSPACE_CHANGES_EVENT_TYPE,
    WORKSPACE_CHANGES_METADATA_KEY,
    WorkspaceChangeLimits,
    WorkspaceChangeResult,
    WorkspaceChangeSummary,
    WorkspaceFileChange,
    WorkspaceRoot,
    WorkspaceSnapshot,
)

logger = logging.getLogger(__name__)

_TRUSTED_CHANGE_STATUSES = ("created", "modified", "deleted")
_TRUSTED_LOGICAL_ROOTS = {"workspace", "outputs"}


def workspace_change_event_content(result: WorkspaceChangeResult) -> str:
    """Format one truthful durable event summary from calculated metrics."""

    summary = result.summary
    changed_file_count = summary.created + summary.modified + summary.deleted
    if summary.additions is None or summary.deletions is None:
        return f"{changed_file_count} file{'s' if changed_file_count != 1 else ''} changed (line counts unavailable)"
    return f"{changed_file_count} file{'s' if changed_file_count != 1 else ''} changed +{summary.additions} -{summary.deletions}"


def trusted_workspace_change_result(changes: object) -> WorkspaceChangeResult | None:
    """Adapt finalizer authority to the current public event schema."""

    if isinstance(changes, WorkspaceChangeResult):
        return changes
    if not isinstance(changes, dict):
        return None
    files: list[WorkspaceFileChange] = []
    counts: dict[str, int] = {}
    seen_paths: set[str] = set()
    for status in _TRUSTED_CHANGE_STATUSES:
        paths = changes.get(status)
        if not isinstance(paths, list) or any(type(path) is not str for path in paths):
            return None
        counts[status] = len(paths)
        for logical_path in paths:
            if "\\" in logical_path:
                return None
            path = PurePosixPath(logical_path)
            if path.is_absolute() or path.as_posix() != logical_path or ".." in path.parts or len(path.parts) < 2:
                return None
            root = path.parts[0]
            if root not in _TRUSTED_LOGICAL_ROOTS or logical_path in seen_paths:
                return None
            seen_paths.add(logical_path)
            virtual_path = f"/mnt/user-data/{logical_path}"
            sensitive = is_sensitive_workspace_path(virtual_path)
            files.append(
                WorkspaceFileChange(
                    path=virtual_path,
                    root=root,
                    status=status,
                    binary=False,
                    sensitive=sensitive,
                    size_before=None,
                    size_after=None,
                    sha256_before=None,
                    sha256_after=None,
                    diff="",
                    diff_unavailable_reason=("sensitive" if sensitive else "unavailable"),
                    additions=None,
                    deletions=None,
                )
            )
    if not files:
        return None
    status_rank = {status: index for index, status in enumerate(_TRUSTED_CHANGE_STATUSES)}
    files.sort(key=lambda item: (status_rank[item.status], item.path))
    return WorkspaceChangeResult(
        summary=WorkspaceChangeSummary(
            created=counts["created"],
            modified=counts["modified"],
            deleted=counts["deleted"],
            additions=None,
            deletions=None,
        ),
        files=files,
    )


def build_thread_workspace_roots(thread_id: str, *, user_id: str | None = None) -> list[WorkspaceRoot]:
    paths = get_paths()
    return [
        WorkspaceRoot(
            name="workspace",
            host_path=paths.sandbox_work_dir(thread_id, user_id=user_id),
            virtual_prefix="/mnt/user-data/workspace",
        ),
        WorkspaceRoot(
            name="outputs",
            host_path=paths.sandbox_outputs_dir(thread_id, user_id=user_id),
            virtual_prefix="/mnt/user-data/outputs",
        ),
    ]


async def capture_workspace_snapshot(
    thread_id: str,
    *,
    user_id: str | None = None,
    limits: WorkspaceChangeLimits | None = None,
    include_text: bool = True,
) -> WorkspaceSnapshot:
    roots = build_thread_workspace_roots(thread_id, user_id=user_id)
    text_cache_dir = Path(tempfile.mkdtemp(prefix="deerflow-workspace-changes-")) if include_text else None
    try:
        return await asyncio.to_thread(
            scan_workspace_roots,
            roots,
            limits=limits,
            include_text=include_text,
            text_cache_dir=text_cache_dir,
        )
    except Exception:
        if text_cache_dir is not None:
            shutil.rmtree(text_cache_dir, ignore_errors=True)
        raise


async def record_workspace_changes(
    event_store: Any,
    thread_id: str,
    run_id: str,
    before: WorkspaceSnapshot,
    *,
    user_id: str | None = None,
    limits: WorkspaceChangeLimits | None = None,
) -> dict | None:
    try:
        roots = build_thread_workspace_roots(thread_id, user_id=user_id)
        after_metadata = await asyncio.to_thread(
            scan_workspace_roots,
            roots,
            limits=limits,
            include_text=False,
        )
        changed_paths = get_changed_paths(before, after_metadata)
        after = await asyncio.to_thread(
            scan_workspace_roots,
            roots,
            limits=limits,
            include_text=True,
            text_paths=changed_paths,
        )
        result = compare_snapshots(before, after, limits=limits)
        if not result.has_changes():
            return None

        payload = result.to_dict()
        content = workspace_change_event_content(result)
        return await event_store.put(
            thread_id=thread_id,
            run_id=run_id,
            event_type=WORKSPACE_CHANGES_EVENT_TYPE,
            category="workspace",
            content=content,
            metadata={WORKSPACE_CHANGES_METADATA_KEY: payload},
        )
    finally:
        _cleanup_snapshot_text_cache(before)


def _cleanup_snapshot_text_cache(snapshot: WorkspaceSnapshot) -> None:
    if snapshot.text_cache_dir:
        shutil.rmtree(snapshot.text_cache_dir, ignore_errors=True)
