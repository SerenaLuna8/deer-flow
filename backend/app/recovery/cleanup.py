"""Cancellation-safe, identity-bound cleanup for recovery secrets."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CLEANUP_ATTEMPTS = 3


class SensitiveCleanupFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnedFile:
    path: Path
    identity: tuple[int, int]


@dataclass(slots=True)
class OwnedWorkspace:
    path: Path
    identity: tuple[int, int]
    files: dict[str, tuple[int, int]] = field(default_factory=dict)

    def register(self, owned: OwnedFile) -> None:
        if owned.path.parent != self.path or owned.path.name in self.files:
            raise SensitiveCleanupFailed
        self.files[owned.path.name] = owned.identity

    def forget(self, owned: OwnedFile) -> None:
        if self.files.get(owned.path.name) != owned.identity:
            raise SensitiveCleanupFailed
        del self.files[owned.path.name]


def _create_owned_workspace(*, prefix: str) -> OwnedWorkspace:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise SensitiveCleanupFailed
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        return OwnedWorkspace(path, (info.st_dev, info.st_ino))
    except SensitiveCleanupFailed:
        raise
    except OSError:
        raise SensitiveCleanupFailed from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_owned_file(
    workspace: Path,
    *,
    prefix: str,
    suffix: str = "",
) -> OwnedFile:
    descriptor = -1
    owned: OwnedFile | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=workspace,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SensitiveCleanupFailed
        owned = OwnedFile(Path(name), (info.st_dev, info.st_ino))
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        return owned
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if owned is not None:
            _remove_owned_file(owned.path, owned.identity)
        raise


def _write_owned_file(owned: OwnedFile, content: bytes) -> None:
    parent = os.open(
        owned.path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        descriptor = os.open(
            owned.path.name,
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != owned.identity:
            raise SensitiveCleanupFailed
        os.ftruncate(descriptor, 0)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise SensitiveCleanupFailed
            view = view[written:]
        os.fsync(descriptor)
    except SensitiveCleanupFailed:
        raise
    except OSError:
        raise SensitiveCleanupFailed from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _remove_owned_file(path: Path, identity: tuple[int, int]) -> None:
    parent = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for attempt in range(_CLEANUP_ATTEMPTS):
            try:
                info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                os.fsync(parent)
                return
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                raise SensitiveCleanupFailed
            try:
                os.unlink(path.name, dir_fd=parent)
                os.fsync(parent)
                try:
                    os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise SensitiveCleanupFailed
            except OSError:
                if attempt + 1 == _CLEANUP_ATTEMPTS:
                    raise SensitiveCleanupFailed from None
        raise SensitiveCleanupFailed
    except SensitiveCleanupFailed:
        raise
    except OSError:
        raise SensitiveCleanupFailed from None
    finally:
        os.close(parent)


def _cleanup_owned_workspace(
    workspace: Path,
    identity: tuple[int, int],
    expected_files: Mapping[str, tuple[int, int]] | None = None,
) -> None:
    if expected_files is None:
        raise SensitiveCleanupFailed
    parent = os.open(
        workspace.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    directory = -1
    try:
        directory = os.open(
            workspace.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            raise SensitiveCleanupFailed
        names = os.listdir(directory)
        if set(names) != set(expected_files):
            raise SensitiveCleanupFailed
        verified: dict[str, tuple[int, int]] = {}
        for name in names:
            child = os.stat(name, dir_fd=directory, follow_symlinks=False)
            child_identity = (child.st_dev, child.st_ino)
            if not stat.S_ISREG(child.st_mode) or child_identity != expected_files[name]:
                raise SensitiveCleanupFailed
            verified[name] = child_identity
        for name, child_identity in verified.items():
            for attempt in range(_CLEANUP_ATTEMPTS):
                try:
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    break
                if (current.st_dev, current.st_ino) != child_identity or not stat.S_ISREG(current.st_mode):
                    raise SensitiveCleanupFailed
                try:
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                    break
                except OSError:
                    if attempt + 1 == _CLEANUP_ATTEMPTS:
                        raise SensitiveCleanupFailed from None
        if os.listdir(directory):
            raise SensitiveCleanupFailed
        os.fsync(directory)
        os.close(directory)
        directory = -1
        for attempt in range(_CLEANUP_ATTEMPTS):
            try:
                os.rmdir(workspace.name, dir_fd=parent)
                os.fsync(parent)
                try:
                    os.stat(workspace.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise SensitiveCleanupFailed
            except FileNotFoundError:
                os.fsync(parent)
                return
            except OSError:
                if attempt + 1 == _CLEANUP_ATTEMPTS:
                    raise SensitiveCleanupFailed from None
        raise SensitiveCleanupFailed
    except SensitiveCleanupFailed:
        raise
    except OSError:
        raise SensitiveCleanupFailed from None
    finally:
        if directory >= 0:
            os.close(directory)
        os.close(parent)


async def _settle_blocking_cleanup(function: Callable[..., Any], *args: Any) -> bool:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


async def _settle_blocking_result[T](
    function: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> tuple[T, bool]:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _settle_async_cleanup(awaitable: Awaitable[Any]) -> bool:
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


__all__ = [
    "OwnedFile",
    "OwnedWorkspace",
    "SensitiveCleanupFailed",
    "_cleanup_owned_workspace",
    "_create_owned_file",
    "_create_owned_workspace",
    "_remove_owned_file",
    "_settle_async_cleanup",
    "_settle_blocking_cleanup",
    "_settle_blocking_result",
    "_write_owned_file",
]
