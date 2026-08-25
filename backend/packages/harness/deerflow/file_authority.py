from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from deerflow.sandbox.sandbox_provider import RunMountReleaseOutcome


@dataclass(frozen=True, slots=True)
class AuthorityManifestEntry:
    file_id: uuid.UUID
    logical_path: str
    kind: str
    media_type: str
    size: int
    sha256: str
    version: int


@dataclass(frozen=True, slots=True)
class AuthorityManifest:
    entries: tuple[AuthorityManifestEntry, ...]
    run_id: str | None = None

    def by_logical_path(self) -> dict[str, AuthorityManifestEntry]:
        return {entry.logical_path: entry for entry in self.entries}


class RunFileAuthority(Protocol):
    """Server-owned project file boundary exposed to the harness."""

    @property
    def sandbox_id(self) -> str | None: ...

    async def restore(self) -> AuthorityManifest: ...

    def thread_data_paths(self) -> dict[str, str]: ...

    def visible_uploads(self) -> tuple[dict[str, object], ...]: ...

    def record_current_upload_ids(self, file_ids: tuple[str, ...]) -> None: ...

    def current_upload_ids(self) -> tuple[str, ...]: ...

    def current_uploads(self) -> tuple[AuthorityManifestEntry, ...]: ...

    def authorizes_run_read_only_mount_path(
        self,
        *,
        run_id: str,
        path: str,
    ) -> bool: ...

    async def write_output(
        self,
        relative_path: str,
        content: bytes,
    ) -> str: ...

    async def write_internal(
        self,
        relative_path: str,
        content: bytes,
    ) -> str: ...

    async def write_delegated_output(
        self,
        output_root: str,
        relative_path: str,
        content: bytes,
    ) -> str: ...

    async def write_delegated_internal(
        self,
        output_root: str,
        relative_path: str,
        content: bytes,
    ) -> str: ...

    def delegated_output_scope(
        self,
        task_id: str,
    ) -> AbstractAsyncContextManager[Any]: ...

    async def record_presented_paths(
        self,
        presented_paths: tuple[str, ...],
        *,
        tool_call_id: str,
    ) -> None: ...

    async def output_delivery_status(self) -> str: ...

    async def finalize(self) -> object: ...

    async def mark_failed(self) -> None: ...

    async def release(self) -> RunMountReleaseOutcome | None: ...


def require_private_file_authority(
    runtime_context: object,
    *,
    method: str | None = None,
) -> RunFileAuthority | None:
    """Return the internal authority in project mode; keep legacy as ``None``.

    The project-mode sentinel is installed by the worker. Client dictionaries
    are never accepted as authority implementations.
    """

    if not isinstance(runtime_context, Mapping) or "private_scope" not in runtime_context:
        return None
    authority: Any = runtime_context.get("__file_authority")
    if authority is None or isinstance(authority, Mapping):
        raise RuntimeError("Private file authority is unavailable")
    if method is not None and not callable(getattr(authority, method, None)):
        raise RuntimeError("Private file authority is unavailable")
    return authority
