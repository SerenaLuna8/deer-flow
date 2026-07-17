"""Cancellation-safe, identity-bound cleanup for recovery secrets."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

_CLEANUP_ATTEMPTS = 3


class SensitiveCleanupFailed(RuntimeError):
    pass


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
        os.close(parent)


def _cleanup_owned_workspace(
    workspace: Path,
    identity: tuple[int, int],
    expected_files: Mapping[str, tuple[int, int]] | None = None,
) -> None:
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
        if expected_files is not None and set(names) != set(expected_files):
            raise SensitiveCleanupFailed
        for name in names:
            child = os.stat(name, dir_fd=directory, follow_symlinks=False)
            child_identity = (child.st_dev, child.st_ino)
            expected_identity = None if expected_files is None else expected_files[name]
            if not stat.S_ISREG(child.st_mode) or (expected_identity is not None and child_identity != expected_identity):
                raise SensitiveCleanupFailed
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
                return
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
    "SensitiveCleanupFailed",
    "_cleanup_owned_workspace",
    "_remove_owned_file",
    "_settle_async_cleanup",
    "_settle_blocking_cleanup",
]
