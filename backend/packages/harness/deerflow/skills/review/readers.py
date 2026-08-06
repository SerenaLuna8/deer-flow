"""Bounded, read-only package readers for deterministic review."""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from deerflow.skills.review.models import (
    DEFAULT_PACKAGE_LIMITS,
    PACKAGE_SNAPSHOT_SCHEMA_VERSION,
    PackageLimits,
    normalize_relative_path,
)

_TEXT_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_ZIP_READ_CHUNK_BYTES = 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_text(data: bytes, path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in _TEXT_EXTENSIONS and b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _subject(
    *,
    source: str,
    display_ref: str,
    name_hint: str | None = None,
    category: str | None = None,
    **identity: Any,
) -> dict[str, Any]:
    return {
        "source": source,
        "category": category,
        "name_hint": name_hint,
        "display_ref": display_ref,
        **identity,
    }


def _empty_snapshot(
    subject: dict[str, Any],
    limits: PackageLimits,
) -> dict[str, Any]:
    return {
        "schema_version": PACKAGE_SNAPSHOT_SCHEMA_VERSION,
        "subject": subject,
        "limits": limits.to_dict(),
        "files": [],
        "truncated": False,
        "reader_errors": [],
    }


def build_bytes_snapshot(
    files: Iterable[tuple[str, bytes]],
    *,
    subject: dict[str, Any],
    limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
) -> dict[str, Any]:
    """Build a bounded snapshot from already-authorized immutable bytes."""
    snapshot = _empty_snapshot(dict(subject), limits)
    selected = sorted(files, key=lambda item: item[0])
    if len(selected) > limits.max_files:
        snapshot["truncated"] = True
        snapshot["reader_errors"].append(
            {
                "code": "too_many_files",
                "path": None,
                "message": "Package file count exceeds the review limit",
            }
        )
        selected = selected[: limits.max_files]

    total_bytes = 0
    for raw_path, raw_content in selected:
        try:
            path = normalize_relative_path(raw_path)
        except ValueError as exc:
            snapshot["reader_errors"].append(
                {
                    "code": "invalid_path",
                    "path": raw_path,
                    "message": str(exc),
                }
            )
            continue
        content = bytes(raw_content)
        total_bytes += len(content)
        if len(content) > limits.max_file_bytes:
            snapshot["truncated"] = True
            snapshot["files"].append(
                {
                    "path": path,
                    "kind": "binary",
                    "size": len(content),
                    "sha256": _sha256(content),
                    "content": None,
                }
            )
            snapshot["reader_errors"].append(
                {
                    "code": "file_too_large",
                    "path": path,
                    "message": "File exceeds the per-file review limit",
                }
            )
            continue
        if total_bytes > limits.max_total_bytes:
            snapshot["truncated"] = True
            snapshot["reader_errors"].append(
                {
                    "code": "total_size_exceeded",
                    "path": path,
                    "message": "Package total size exceeds the review limit",
                }
            )
            break
        text = _decode_text(content, path)
        entry: dict[str, Any] = {
            "path": path,
            "kind": "text" if text is not None else "binary",
            "size": len(content),
            "sha256": _sha256(content),
        }
        if text is not None:
            entry["content"] = text
        snapshot["files"].append(entry)
    return _sort_snapshot(snapshot)


def build_inline_snapshot(
    content: str,
    *,
    name_hint: str | None = None,
    limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
) -> dict[str, Any]:
    data = content.encode("utf-8")
    subject = _subject(
        source="inline",
        display_ref=name_hint or "inline://SKILL.md",
        name_hint=name_hint,
    )
    return build_bytes_snapshot(
        [("SKILL.md", data)],
        subject=subject,
        limits=limits,
    )


class LocalDirectoryReader:
    """Read a local Skill directory without following symlink escapes."""

    def __init__(
        self,
        root: str | Path,
        *,
        subject: dict[str, Any] | None = None,
        limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
    ) -> None:
        self.root = Path(root)
        self.limits = limits
        self.subject = subject or _subject(
            source="local_directory",
            display_ref=self.root.name or str(self.root),
            name_hint=self.root.name or None,
        )

    def read(self) -> dict[str, Any]:
        snapshot = _empty_snapshot(self.subject, self.limits)
        if not self.root.exists():
            snapshot["reader_errors"].append(
                {
                    "code": "root_not_found",
                    "path": None,
                    "message": "Package root does not exist",
                }
            )
            return snapshot
        if not self.root.is_dir():
            snapshot["reader_errors"].append(
                {
                    "code": "root_not_directory",
                    "path": None,
                    "message": "Package root is not a directory",
                }
            )
            return snapshot

        root = self.root.resolve()
        total_bytes = 0
        file_count = 0
        for current_root, dir_names, file_names in os.walk(
            root,
            followlinks=False,
        ):
            current = Path(current_root)
            dir_names[:] = sorted(dir_names)
            for dirname in list(dir_names):
                path = current / dirname
                if path.is_symlink():
                    dir_names.remove(dirname)
                    file_count = self._append_symlink(
                        snapshot,
                        path,
                        root,
                        file_count,
                    )

            for filename in sorted(file_names):
                path = current / filename
                if path.is_symlink():
                    file_count = self._append_symlink(
                        snapshot,
                        path,
                        root,
                        file_count,
                    )
                    continue
                rel_path = self._relative(path, root, snapshot)
                if rel_path is None:
                    continue
                file_count += 1
                if file_count > self.limits.max_files:
                    snapshot["truncated"] = True
                    snapshot["reader_errors"].append(
                        {
                            "code": "too_many_files",
                            "path": None,
                            "message": ("Package file count exceeds the review limit"),
                        }
                    )
                    return _sort_snapshot(snapshot)
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    snapshot["reader_errors"].append(
                        {
                            "code": "stat_failed",
                            "path": rel_path,
                            "message": str(exc),
                        }
                    )
                    continue
                total_bytes += max(size, 0)
                if total_bytes > self.limits.max_total_bytes:
                    snapshot["truncated"] = True
                    snapshot["reader_errors"].append(
                        {
                            "code": "total_size_exceeded",
                            "path": rel_path,
                            "message": ("Package total size exceeds the review limit"),
                        }
                    )
                    return _sort_snapshot(snapshot)
                if size > self.limits.max_file_bytes:
                    snapshot["truncated"] = True
                    snapshot["files"].append(
                        {
                            "path": rel_path,
                            "kind": "binary",
                            "size": size,
                            "sha256": "",
                            "content": None,
                        }
                    )
                    snapshot["reader_errors"].append(
                        {
                            "code": "file_too_large",
                            "path": rel_path,
                            "message": ("File exceeds the per-file review limit"),
                        }
                    )
                    continue
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    snapshot["reader_errors"].append(
                        {
                            "code": "read_failed",
                            "path": rel_path,
                            "message": str(exc),
                        }
                    )
                    continue
                text = _decode_text(data, rel_path)
                entry: dict[str, Any] = {
                    "path": rel_path,
                    "kind": "text" if text is not None else "binary",
                    "size": len(data),
                    "sha256": _sha256(data),
                }
                if text is not None:
                    entry["content"] = text
                snapshot["files"].append(entry)
        return _sort_snapshot(snapshot)

    def _append_symlink(
        self,
        snapshot: dict[str, Any],
        path: Path,
        root: Path,
        file_count: int,
    ) -> int:
        rel_path = self._relative(path, root, snapshot)
        if rel_path is None:
            return file_count
        file_count += 1
        if file_count > self.limits.max_files:
            snapshot["truncated"] = True
            snapshot["reader_errors"].append(
                {
                    "code": "too_many_files",
                    "path": None,
                    "message": "Package file count exceeds the review limit",
                }
            )
            return file_count
        try:
            target = os.readlink(path)
        except OSError:
            target = ""
        snapshot["files"].append(
            {
                "path": rel_path,
                "kind": "symlink",
                "size": 0,
                "sha256": _sha256(target.encode()),
                "target": target,
            }
        )
        return file_count

    @staticmethod
    def _relative(
        path: Path,
        root: Path,
        snapshot: dict[str, Any],
    ) -> str | None:
        try:
            return normalize_relative_path(path.relative_to(root).as_posix())
        except ValueError:
            snapshot["reader_errors"].append(
                {
                    "code": "path_escaped",
                    "path": None,
                    "message": "Package entry escapes the root",
                }
            )
            return None


class ArchivePackageReader:
    """Inspect a .skill ZIP archive without installing it."""

    def __init__(
        self,
        archive_path: str | Path,
        *,
        limits: PackageLimits = DEFAULT_PACKAGE_LIMITS,
    ) -> None:
        self.archive_path = Path(archive_path)
        self.limits = limits

    def read(self) -> dict[str, Any]:
        snapshot = _empty_snapshot(
            _subject(
                source="archive",
                display_ref=self.archive_path.name,
                name_hint=self.archive_path.stem,
            ),
            self.limits,
        )
        try:
            with zipfile.ZipFile(self.archive_path, "r") as archive:
                members = sorted(
                    archive.infolist(),
                    key=lambda info: info.filename,
                )
                if len(members) > self.limits.max_files:
                    snapshot["truncated"] = True
                    snapshot["reader_errors"].append(
                        {
                            "code": "too_many_files",
                            "path": None,
                            "message": ("Archive member count exceeds review limit"),
                        }
                    )
                    members = members[: self.limits.max_files]
                total_bytes = 0
                for info in members:
                    if info.is_dir():
                        continue
                    try:
                        rel_path = normalize_relative_path(info.filename)
                    except ValueError as exc:
                        snapshot["reader_errors"].append(
                            {
                                "code": "invalid_archive_path",
                                "path": info.filename,
                                "message": str(exc),
                            }
                        )
                        continue
                    declared_size = max(info.file_size, 0)
                    remaining = self.limits.max_total_bytes - total_bytes
                    if declared_size > self.limits.max_file_bytes:
                        snapshot["truncated"] = True
                        snapshot["files"].append(
                            {
                                "path": rel_path,
                                "kind": "binary",
                                "size": declared_size,
                                "sha256": "",
                                "content": None,
                            }
                        )
                        snapshot["reader_errors"].append(
                            {
                                "code": "file_too_large",
                                "path": rel_path,
                                "message": ("Archive member exceeds per-file limit"),
                            }
                        )
                        continue
                    if remaining <= 0:
                        snapshot["truncated"] = True
                        snapshot["reader_errors"].append(
                            {
                                "code": "total_size_exceeded",
                                "path": rel_path,
                                "message": ("Archive total size exceeds review limit"),
                            }
                        )
                        break
                    budget = min(self.limits.max_file_bytes, remaining)
                    data, actual_size, exceeded = _read_zip_member_bounded(
                        archive,
                        info,
                        max_bytes=budget,
                    )
                    if exceeded:
                        snapshot["truncated"] = True
                        if actual_size > self.limits.max_file_bytes:
                            snapshot["files"].append(
                                {
                                    "path": rel_path,
                                    "kind": "binary",
                                    "size": actual_size,
                                    "sha256": "",
                                    "content": None,
                                }
                            )
                            snapshot["reader_errors"].append(
                                {
                                    "code": "file_too_large",
                                    "path": rel_path,
                                    "message": ("Archive member exceeds per-file limit"),
                                }
                            )
                            continue
                        snapshot["reader_errors"].append(
                            {
                                "code": "total_size_exceeded",
                                "path": rel_path,
                                "message": ("Archive total size exceeds review limit"),
                            }
                        )
                        break
                    total_bytes += actual_size
                    if _zip_member_is_symlink(info):
                        target = data.decode("utf-8", errors="replace")
                        snapshot["files"].append(
                            {
                                "path": rel_path,
                                "kind": "symlink",
                                "size": 0,
                                "sha256": _sha256(data),
                                "target": target,
                            }
                        )
                        continue
                    text = _decode_text(data, rel_path)
                    entry: dict[str, Any] = {
                        "path": rel_path,
                        "kind": ("text" if text is not None else "binary"),
                        "size": actual_size,
                        "sha256": _sha256(data),
                    }
                    if text is not None:
                        entry["content"] = text
                    snapshot["files"].append(entry)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            snapshot["reader_errors"].append(
                {
                    "code": "archive_read_failed",
                    "path": None,
                    "message": str(exc),
                }
            )
        return _sort_snapshot(snapshot)


def _sort_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot["files"] = sorted(
        snapshot["files"],
        key=lambda item: item["path"],
    )
    snapshot["reader_errors"] = sorted(
        snapshot["reader_errors"],
        key=lambda item: (
            str(item.get("path") or ""),
            str(item.get("code") or ""),
        ),
    )
    return snapshot


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _read_zip_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> tuple[bytes, int, bool]:
    chunks: list[bytes] = []
    actual_size = 0
    with archive.open(info) as member:
        while True:
            read_size = min(
                _ZIP_READ_CHUNK_BYTES,
                max_bytes + 1 - actual_size,
            )
            if read_size <= 0:
                return b"".join(chunks), actual_size, True
            chunk = member.read(read_size)
            if not chunk:
                return b"".join(chunks), actual_size, False
            actual_size += len(chunk)
            if actual_size > max_bytes:
                return b"".join(chunks), actual_size, True
            chunks.append(chunk)
