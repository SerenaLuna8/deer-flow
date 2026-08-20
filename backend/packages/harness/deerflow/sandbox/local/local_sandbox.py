import errno
import logging
import math
import ntpath
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.env_policy import build_sandbox_env, is_blocked_env_name
from deerflow.sandbox.local.list_dir import list_dir
from deerflow.sandbox.path_patterns import build_output_mask_pattern
from deerflow.sandbox.sandbox import (
    PRIVATE_FILE_IO_CHUNK_SIZE,
    Sandbox,
    SandboxFileInfo,
    _validate_extra_env,
    validate_secure_scan_excluded_root_names,
)
from deerflow.sandbox.search import GrepMatch, find_glob_matches, find_grep_matches

logger = logging.getLogger(__name__)

# Default wall-clock timeout (seconds) for a single host bash command. A
# blocking foreground command (for example a server started without
# backgrounding) is terminated after this long so the agent's turn cannot hang
# indefinitely. Overridable per call via ``execute_command(timeout=...)`` and,
# for the bash tool, via ``sandbox.bash_command_timeout`` in config.yaml.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600
_COMMAND_CAPTURE_LIMIT_BYTES = 10 * 1024 * 1024
_PIPE_DRAIN_JOIN_TIMEOUT_SECONDS = 0.2


class LocalProcessSpawnDeadlineExpired(RuntimeError):
    """A guarded Local process was not created before its spawn deadline."""


class LocalProcessSpawnAuthorizationFailed(RuntimeError):
    """A guarded Local process failed its final synchronous authorization."""


def _assert_process_spawn_deadline(
    spawn_deadline_monotonic: float | None,
) -> None:
    if spawn_deadline_monotonic is None:
        return
    if isinstance(spawn_deadline_monotonic, bool) or not isinstance(spawn_deadline_monotonic, (int, float)) or not math.isfinite(spawn_deadline_monotonic):
        raise ValueError("Local process spawn deadline is invalid")
    if time.monotonic() >= float(spawn_deadline_monotonic):
        raise LocalProcessSpawnDeadlineExpired


def _authorize_process_spawn(
    *,
    spawn_deadline_monotonic: float | None,
    spawn_authorization_guard: Callable[[], float] | None,
) -> None:
    guarded_deadline = None
    if spawn_authorization_guard is not None:
        try:
            guarded_deadline = spawn_authorization_guard()
        except (
            LocalProcessSpawnAuthorizationFailed,
            LocalProcessSpawnDeadlineExpired,
        ):
            raise
        except Exception as error:
            raise LocalProcessSpawnAuthorizationFailed from error
        if isinstance(guarded_deadline, bool) or not isinstance(guarded_deadline, (int, float)) or not math.isfinite(guarded_deadline):
            raise LocalProcessSpawnAuthorizationFailed
        guarded_deadline = float(guarded_deadline)
    effective_deadline = spawn_deadline_monotonic
    if guarded_deadline is not None and (effective_deadline is None or guarded_deadline < effective_deadline):
        effective_deadline = guarded_deadline
    _assert_process_spawn_deadline(effective_deadline)


class _BoundedPipeCapture:
    """Drain a subprocess pipe while keeping only bounded output in memory."""

    def __init__(self, *, limit_bytes: int = _COMMAND_CAPTURE_LIMIT_BYTES) -> None:
        self._limit_bytes = limit_bytes
        self._chunks: list[bytes] = []
        self._kept_bytes = 0
        self._total_bytes = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._total_bytes += len(chunk)
            if self._kept_bytes >= self._limit_bytes:
                return
            remaining = self._limit_bytes - self._kept_bytes
            kept = chunk[:remaining]
            self._chunks.append(kept)
            self._kept_bytes += len(kept)

    def read(self) -> str:
        with self._lock:
            data = b"".join(self._chunks)
            truncated = self._total_bytes > self._kept_bytes
            total_bytes = self._total_bytes
            kept_bytes = self._kept_bytes

        output = data.decode("utf-8", errors="replace")
        if truncated:
            notice = f"\n... [output truncated after {kept_bytes} of {total_bytes} bytes; remaining output discarded] ..."
            output += notice
        return output


@dataclass(frozen=True)
class PathMapping:
    """A path mapping from a container path to a local path with optional read-only flag."""

    container_path: str
    local_path: str
    read_only: bool = False


@dataclass(frozen=True)
class LocalCommandExecutionResult:
    """Structured result for one exact prepared host process launch."""

    output: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class _LocalBinaryReader:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def read(self, size: int) -> bytes:
        if not 0 < size <= PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox reads must be bounded to 1 MiB")
        return os.read(self._fd, size)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def _open_private_directory(path: str, *, dir_fd: int | None = None) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "Anchored private file IO is unsupported")
    fd = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=dir_fd,
    )
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "Private path component is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk_private_directory(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_fd = _open_private_directory(part, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = _open_private_directory(part, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_private_absolute_directory(path: str) -> int:
    """Open one absolute directory without following any path component."""

    absolute = PurePosixPath(path)
    if not absolute.is_absolute() or any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise OSError(errno.EINVAL, "Invalid private mapping root", path)
    filesystem_fd = _open_private_directory(os.path.sep)
    try:
        return _walk_private_directory(
            filesystem_fd,
            tuple(absolute.parts[1:]),
        )
    finally:
        os.close(filesystem_fd)


def _clear_private_directory(directory_fd: int) -> None:
    """Remove directory contents without ever following a filesystem link."""

    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_private_directory(name, dir_fd=directory_fd)
            try:
                if not _same_inode(entry, os.fstat(child_fd)):
                    raise OSError(
                        errno.ESTALE,
                        "Private projection directory changed during reset",
                        name,
                    )
                _clear_private_directory(child_fd)
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_inode(entry, current):
                raise OSError(
                    errno.ESTALE,
                    "Private projection directory changed during reset",
                    name,
                )
            os.rmdir(name, dir_fd=directory_fd)
            continue
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise OSError(
                errno.EPERM,
                "Private projection reset rejects links and special files",
                name,
            )
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or not _same_inode(entry, current):
            raise OSError(
                errno.ESTALE,
                "Private projection file changed during reset",
                name,
            )
        os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def reset_private_projection_root(base_dir: Path, relative_root: str) -> None:
    """Create and empty one private run projection through anchored dirfds.

    Every absolute and relative ancestor is opened with ``O_NOFOLLOW``.  The
    four mutable projection directories are retained but emptied in place, so
    a rollback/error run cannot leak stale host state into the next run.
    """

    relative_parts = PurePosixPath(relative_root).parts
    if not relative_root or PurePosixPath(relative_root).is_absolute() or any(part in {"", ".", ".."} for part in relative_parts):
        raise OSError(errno.EINVAL, "Invalid private projection root")

    absolute_base = Path(base_dir).absolute()
    absolute_parts = tuple(part for part in absolute_base.parts if part != absolute_base.anchor)
    filesystem_fd = _open_private_directory(absolute_base.anchor or os.path.sep)
    try:
        base_fd = _walk_private_directory(filesystem_fd, absolute_parts, create=True)
    finally:
        os.close(filesystem_fd)

    try:
        thread_fd = _walk_private_directory(
            base_fd,
            tuple(relative_parts),
            create=True,
        )
        try:
            user_data_fd = _walk_private_directory(
                thread_fd,
                ("user-data",),
                create=True,
            )
            try:
                for name in ("workspace", "uploads", "outputs"):
                    projection_fd = _walk_private_directory(
                        user_data_fd,
                        (name,),
                        create=True,
                    )
                    try:
                        _clear_private_directory(projection_fd)
                    finally:
                        os.close(projection_fd)
            finally:
                os.close(user_data_fd)

            acp_fd = _walk_private_directory(
                thread_fd,
                ("acp-workspace",),
                create=True,
            )
            try:
                _clear_private_directory(acp_fd)
            finally:
                os.close(acp_fd)
        finally:
            os.close(thread_fd)
    finally:
        os.close(base_fd)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_private_parent(
    open_root: Callable[[], int],
    parent_parts: tuple[str, ...],
    root_fd: int,
    parent_fd: int,
    display_path: str,
) -> None:
    check_root_fd = open_root()
    try:
        if not _same_inode(os.fstat(check_root_fd), os.fstat(root_fd)):
            raise OSError(errno.ESTALE, "Private sandbox root changed", display_path)
        check_parent_fd = _walk_private_directory(check_root_fd, parent_parts)
        try:
            if not _same_inode(os.fstat(check_parent_fd), os.fstat(parent_fd)):
                raise OSError(
                    errno.ESTALE,
                    "Private sandbox path ancestor changed",
                    display_path,
                )
        finally:
            os.close(check_parent_fd)
    finally:
        os.close(check_root_fd)


class _LocalAtomicWriter:
    def __init__(
        self,
        *,
        open_root: Callable[[], int],
        parent_parts: tuple[str, ...],
        root_fd: int,
        parent_fd: int,
        temp_name: str,
        target_name: str,
        display_path: str,
        fd: int,
    ) -> None:
        self._open_root = open_root
        self._parent_parts = parent_parts
        self._root_fd = root_fd
        self._parent_fd = parent_fd
        self._temp_name = temp_name
        self._target_name = target_name
        self._display_path = display_path
        self._fd = fd
        self._finished = False
        self._published_stat: os.stat_result | None = None
        self._rollback_parent_fd = -1

    def write(self, content: bytes) -> None:
        if self._finished or not isinstance(content, bytes) or not 0 < len(content) <= PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox writes must be bounded to 1 MiB")
        view = memoryview(content)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]

    def commit(self) -> None:
        if self._finished:
            raise OSError(errno.EBADF, "Atomic writer is closed")
        try:
            temp_fd_stat = os.fstat(self._fd)
            if not stat.S_ISREG(temp_fd_stat.st_mode) or temp_fd_stat.st_nlink != 1:
                raise OSError(
                    errno.EPERM,
                    "Atomic staging target must be one regular link",
                    self._display_path,
                )
            os.fsync(self._fd)
            try:
                temp_entry_stat = os.stat(
                    self._temp_name,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                raise OSError(
                    errno.ESTALE,
                    "Atomic staging entry changed",
                    self._display_path,
                ) from None
            if not _same_inode(temp_entry_stat, temp_fd_stat):
                raise OSError(
                    errno.ESTALE,
                    "Atomic staging entry changed",
                    self._display_path,
                )
            try:
                target_stat = os.stat(
                    self._target_name,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                target_stat = None
            if target_stat is not None and (not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1):
                raise OSError(
                    errno.ELOOP,
                    "Atomic target must be one regular link",
                    self._display_path,
                )
            _verify_private_parent(
                self._open_root,
                self._parent_parts,
                self._root_fd,
                self._parent_fd,
                self._display_path,
            )
            self._rollback_parent_fd = os.dup(self._parent_fd)
            self._published_stat = temp_fd_stat
            os.replace(
                self._temp_name,
                self._target_name,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
            )
            os.fsync(self._parent_fd)
            fd = self._fd
            self._fd = -1
            os.close(fd)
            parent_fd = self._parent_fd
            self._parent_fd = -1
            os.close(parent_fd)
            root_fd = self._root_fd
            self._root_fd = -1
            os.close(root_fd)
            self._published_stat = None
            self._finished = True
            rollback_parent_fd = self._rollback_parent_fd
            self._rollback_parent_fd = -1
            try:
                os.close(rollback_parent_fd)
            except OSError:
                pass
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self._finished:
            return
        if self._fd >= 0:
            fd = self._fd
            self._fd = -1
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            cleanup_parent_fd = self._parent_fd if self._parent_fd >= 0 else self._rollback_parent_fd
            if cleanup_parent_fd >= 0:
                if self._published_stat is not None:
                    try:
                        target_stat = os.stat(
                            self._target_name,
                            dir_fd=cleanup_parent_fd,
                            follow_symlinks=False,
                        )
                        if stat.S_ISREG(target_stat.st_mode) and target_stat.st_nlink == 1 and _same_inode(target_stat, self._published_stat):
                            os.unlink(
                                self._target_name,
                                dir_fd=cleanup_parent_fd,
                            )
                            try:
                                os.fsync(cleanup_parent_fd)
                            except OSError:
                                pass
                    except FileNotFoundError:
                        pass
                    finally:
                        self._published_stat = None
                try:
                    os.unlink(self._temp_name, dir_fd=cleanup_parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            if self._parent_fd >= 0:
                parent_fd = self._parent_fd
                self._parent_fd = -1
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
            if self._root_fd >= 0:
                root_fd = self._root_fd
                self._root_fd = -1
                try:
                    os.close(root_fd)
                except OSError:
                    pass
            if self._rollback_parent_fd >= 0:
                rollback_parent_fd = self._rollback_parent_fd
                self._rollback_parent_fd = -1
                try:
                    os.close(rollback_parent_fd)
                except OSError:
                    pass
            self._finished = True


class ResolvedPath(NamedTuple):
    path: str
    mapping: PathMapping | None


class LocalSandbox(Sandbox):
    @staticmethod
    def _shell_name(shell: str) -> str:
        """Return the executable name for a shell path or command."""
        return shell.replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _is_powershell(shell: str) -> bool:
        """Return whether the selected shell is a PowerShell executable."""
        return LocalSandbox._shell_name(shell) in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}

    @staticmethod
    def _is_cmd_shell(shell: str) -> bool:
        """Return whether the selected shell is cmd.exe."""
        return LocalSandbox._shell_name(shell) in {"cmd", "cmd.exe"}

    @staticmethod
    def _is_msys_shell(shell: str) -> bool:
        """Return whether the selected shell is a Git Bash/MSYS shell."""
        normalized = shell.replace("\\", "/").lower()
        shell_name = LocalSandbox._shell_name(shell)
        return shell_name in {"sh.exe", "bash.exe"} and any(part in normalized for part in ("/git/", "/mingw", "/msys"))

    @staticmethod
    def _find_first_available_shell(candidates: tuple[str, ...]) -> str | None:
        """Return the first executable shell path or command found from candidates."""
        for shell in candidates:
            if os.path.isabs(shell):
                if os.path.isfile(shell) and os.access(shell, os.X_OK):
                    return shell
                continue

            shell_from_path = shutil.which(shell)
            if shell_from_path is not None:
                return shell_from_path

        return None

    @staticmethod
    def _format_timeout_duration(timeout: float) -> str:
        seconds = float(timeout)
        if seconds.is_integer():
            amount = str(int(seconds))
        else:
            amount = f"{seconds:g}"
        unit = "second" if seconds == 1 else "seconds"
        return f"{amount} {unit}"

    @staticmethod
    def _format_timeout_notice(timeout: float) -> str:
        return (
            f"Command timed out after {LocalSandbox._format_timeout_duration(timeout)} and was terminated. "
            "To run a long-lived process such as a web server, start it in the background "
            "and redirect its output, e.g. `your-command > /mnt/user-data/workspace/server.log 2>&1 &`."
        )

    @staticmethod
    def _coerce_process_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _drain_pipe(fd: int, capture: _BoundedPipeCapture) -> None:
        try:
            while chunk := os.read(fd, 8192):
                capture.append(chunk)
        except OSError:
            logger.debug("Subprocess output pipe closed while draining", exc_info=True)
        finally:
            try:
                os.close(fd)
            except OSError:
                # The fd may already be closed during pipe teardown; cleanup is best-effort.
                pass

    @staticmethod
    def _start_pipe_drain(fd: int, name: str) -> tuple[_BoundedPipeCapture, threading.Thread]:
        capture = _BoundedPipeCapture()
        thread = threading.Thread(target=LocalSandbox._drain_pipe, args=(fd, capture), name=name, daemon=True)
        thread.start()
        return capture, thread

    @staticmethod
    def _process_group_exists(pgid: int | None) -> bool:
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def __init__(self, id: str, path_mappings: list[PathMapping] | None = None):
        """
        Initialize local sandbox with optional path mappings.

        Args:
            id: Sandbox identifier
            path_mappings: List of path mappings with optional read-only flag.
                          Skills directory is read-only by default.
        """
        super().__init__(id)
        self.path_mappings = path_mappings or []
        self._private_mapping_fds: dict[PathMapping, int] | None = None
        self._private_mapping_closed = False
        # Track files written through write_file so read_file only
        # reverse-resolves paths in agent-authored content.
        self._agent_written_paths: set[str] = set()

    def anchor_private_mappings(self) -> None:
        """Pin every mapping root to its acquire-time inode for one private lease."""

        if self._private_mapping_fds is not None:
            raise OSError(errno.EBUSY, "Private mapping roots are already anchored")
        if self._private_mapping_closed:
            raise OSError(errno.EBADF, "Private mapping roots are closed")
        opened: dict[PathMapping, int] = {}
        try:
            for mapping in self.path_mappings:
                opened[mapping] = _open_private_absolute_directory(mapping.local_path)
        except BaseException:
            for fd in opened.values():
                os.close(fd)
            raise
        self._private_mapping_fds = opened

    def close_private_file_authority(self) -> None:
        """Release opaque handles and every root fd owned by a private lease."""

        super().close_private_file_authority()
        anchors = self._private_mapping_fds
        self._private_mapping_fds = None
        self._private_mapping_closed = True
        if anchors is not None:
            for fd in anchors.values():
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _open_private_mapping_root(self, mapping: PathMapping) -> int:
        anchors = self._private_mapping_fds
        if anchors is None:
            if self._private_mapping_closed:
                raise OSError(errno.EBADF, "Private mapping root is closed", mapping.container_path)
            return _open_private_absolute_directory(mapping.local_path)
        anchor_fd = anchors.get(mapping)
        if anchor_fd is None:
            raise OSError(errno.EBADF, "Private mapping root is closed", mapping.container_path)
        check_fd = _open_private_absolute_directory(mapping.local_path)
        try:
            if not _same_inode(os.fstat(anchor_fd), os.fstat(check_fd)):
                raise OSError(
                    errno.ESTALE,
                    "Private mapping root changed after acquire",
                    mapping.container_path,
                )
        finally:
            os.close(check_fd)
        return os.dup(anchor_fd)

    # ``path_mappings`` is set once in ``__init__`` and never mutated, so the
    # sorted views and compiled path-rewrite patterns below are stable for the
    # sandbox's lifetime. Caching them avoids re-sorting and re-compiling these
    # regexes on every bash/read_file/write_file call (the agent's hot path).

    @cached_property
    def _command_pattern(self) -> re.Pattern[str] | None:
        """Compiled matcher for container paths in shell commands (shell-aware boundaries)."""
        mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)
        if not mappings:
            return None
        # The lookahead (?=/|$|...) ensures we only match at a path-segment boundary,
        # preventing /mnt/skills from matching inside /mnt/skills-extra.
        patterns = [re.escape(m.container_path) + r"(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?" for m in mappings]
        return re.compile("|".join(f"({p})" for p in patterns))

    @cached_property
    def _content_pattern(self) -> re.Pattern[str] | None:
        """Compiled matcher for container paths in plain file content (text boundaries)."""
        mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)
        if not mappings:
            return None
        patterns = [re.escape(m.container_path) + r"(?=/|$|[^\w./-])(?:/[^\s\"';&|<>()]*)?" for m in mappings]
        return re.compile("|".join(f"({p})" for p in patterns))

    @cached_property
    def _reverse_output_patterns(self) -> list[re.Pattern[str]]:
        """Compiled matchers for local paths in command output (longest local path first)."""
        return [build_output_mask_pattern(self._resolved_local_paths[m]) for m in self._mappings_by_local_specificity]

    @cached_property
    def _resolved_local_paths(self) -> dict[PathMapping, str]:
        """Filesystem-resolved local root per mapping. ``Path.resolve()`` hits the
        disk, and the mounted directories don't move, so resolve once and reuse."""
        return {m: str(Path(m.local_path).resolve()) for m in self.path_mappings}

    @cached_property
    def _mappings_by_container_specificity(self) -> list[PathMapping]:
        """Mappings ordered most-specific-container-first (for forward resolution)."""
        return sorted(self.path_mappings, key=lambda m: len(m.container_path.rstrip("/") or "/"), reverse=True)

    @cached_property
    def _mappings_by_local_specificity(self) -> list[PathMapping]:
        """Mappings ordered longest-local-path-first (for reverse resolution)."""
        return sorted(self.path_mappings, key=lambda m: len(m.local_path), reverse=True)

    def _is_read_only_path(self, resolved_path: str) -> bool:
        """Check if a resolved path is under a read-only mount.

        When multiple mappings match (nested mounts), prefer the most specific
        mapping (i.e. the one whose local_path is the longest prefix of the
        resolved path), similar to how ``_resolve_path`` handles container paths.
        """
        resolved = str(Path(resolved_path).resolve())

        best_mapping: PathMapping | None = None
        best_prefix_len = -1

        for mapping in self.path_mappings:
            local_resolved = self._resolved_local_paths[mapping]
            if resolved == local_resolved or resolved.startswith(local_resolved + os.sep):
                prefix_len = len(local_resolved)
                if prefix_len > best_prefix_len:
                    best_prefix_len = prefix_len
                    best_mapping = mapping

        if best_mapping is None:
            return False

        return best_mapping.read_only

    def _find_path_mapping(self, path: str) -> tuple[PathMapping, str] | None:
        path_str = str(path)

        for mapping in self._mappings_by_container_specificity:
            container_path = mapping.container_path.rstrip("/") or "/"
            if container_path == "/":
                if path_str.startswith("/"):
                    return mapping, path_str.lstrip("/")
                continue

            if path_str == container_path or path_str.startswith(container_path + "/"):
                relative = path_str[len(container_path) :].lstrip("/")
                return mapping, relative

        return None

    def _secure_private_path(self, path: str, *, create_parents: bool = False) -> Path:
        match = self._find_path_mapping(path)
        if match is None:
            raise OSError(errno.EACCES, "Path is outside the sandbox", path)
        mapping, relative = match
        if mapping.read_only:
            raise OSError(errno.EROFS, "Read-only file system", path)
        parts = Path(relative.replace("\\", "/")).parts
        if not relative or any(part in {"", ".", ".."} for part in parts):
            raise OSError(errno.EACCES, "Unsafe sandbox path", path)
        root = Path(mapping.local_path)
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in parts[:-1]:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if not create_parents:
                    raise OSError(errno.ENOENT, "Parent directory does not exist", path) from None
                current.mkdir()
                mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise OSError(errno.ELOOP, "Unsafe sandbox path ancestor", path)
        target = current / parts[-1]
        root_resolved = root.resolve()
        parent_resolved = target.parent.resolve()
        if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
            raise OSError(errno.EACCES, "Path escapes sandbox mapping", path)
        return target

    def _private_path_parts(
        self,
        path: str,
        *,
        write: bool,
        allow_root: bool = False,
    ) -> tuple[PathMapping, tuple[str, ...]]:
        match = self._find_path_mapping(path)
        if match is None:
            raise OSError(errno.EACCES, "Path is outside the sandbox", path)
        mapping, relative = match
        if write and mapping.read_only:
            raise OSError(errno.EROFS, "Read-only file system", path)
        if "\\" in relative:
            raise OSError(errno.EACCES, "Unsafe sandbox path", path)
        parts = PurePosixPath(relative).parts if relative else ()
        if (not allow_root and not parts) or any(part in {"", ".", ".."} for part in parts):
            raise OSError(errno.EACCES, "Unsafe sandbox path", path)
        return mapping, tuple(parts)

    def _open_private_parent(
        self,
        path: str,
        *,
        write: bool,
        create_parents: bool = False,
    ) -> tuple[PathMapping, tuple[str, ...], int, int, str]:
        mapping, parts = self._private_path_parts(path, write=write)
        root_fd = self._open_private_mapping_root(mapping)
        try:
            parent_parts = parts[:-1]
            parent_fd = _walk_private_directory(
                root_fd,
                parent_parts,
                create=create_parents,
            )
        except BaseException:
            os.close(root_fd)
            raise
        return mapping, parent_parts, root_fd, parent_fd, parts[-1]

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
        excluded_root_names: tuple[str, ...] = (),
    ) -> Iterator[SandboxFileInfo]:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("Secure scan entry limit must be positive")
        excluded_names = validate_secure_scan_excluded_root_names(
            excluded_root_names,
        )
        mapping, parts = self._private_path_parts(
            root,
            write=False,
            allow_root=True,
        )

        def iterate() -> Iterator[SandboxFileInfo]:
            mapping_root_fd = self._open_private_mapping_root(mapping)
            base_fd = -1
            stack: list[tuple[int, str, Iterator[os.DirEntry[str]]]] = []
            seen = 0
            try:
                try:
                    base_fd = _walk_private_directory(mapping_root_fd, parts)
                except FileNotFoundError:
                    return
                stack.append((base_fd, root, os.scandir(base_fd)))
                while stack:
                    directory_fd, virtual_directory, names = stack[-1]
                    try:
                        directory_entry = next(names)
                    except StopIteration:
                        finished_fd, _, finished_names = stack.pop()
                        close_names = getattr(finished_names, "close", None)
                        if callable(close_names):
                            close_names()
                        if finished_fd != base_fd:
                            os.close(finished_fd)
                        continue
                    name = directory_entry.name
                    virtual_path = f"{virtual_directory.rstrip('/')}/{name}"
                    info = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(info.st_mode):
                        kind = "symlink"
                    elif stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        kind = "regular"
                    else:
                        kind = "other"
                    if directory_fd == base_fd and kind == "directory" and name in excluded_names:
                        continue
                    seen += 1
                    if seen > max_entries:
                        raise OSError(
                            errno.EFBIG,
                            "Secure scan entry limit exceeded",
                            root,
                        )
                    yield SandboxFileInfo(
                        path=virtual_path,
                        size=info.st_size,
                        file_type=kind,
                    )
                    if kind == "directory":
                        child_fd = _open_private_directory(name, dir_fd=directory_fd)
                        if not _same_inode(info, os.fstat(child_fd)):
                            os.close(child_fd)
                            raise OSError(
                                errno.ESTALE,
                                "Secure scan directory changed",
                                virtual_path,
                            )
                        try:
                            child_names = os.scandir(child_fd)
                        except BaseException:
                            os.close(child_fd)
                            raise
                        stack.append(
                            (
                                child_fd,
                                virtual_path,
                                child_names,
                            )
                        )
                check_root_fd = self._open_private_mapping_root(mapping)
                try:
                    if not _same_inode(os.fstat(mapping_root_fd), os.fstat(check_root_fd)):
                        raise OSError(errno.ESTALE, "Secure scan root changed", root)
                    check_base_fd = _walk_private_directory(check_root_fd, parts)
                    try:
                        if not _same_inode(os.fstat(base_fd), os.fstat(check_base_fd)):
                            raise OSError(errno.ESTALE, "Secure scan root changed", root)
                    finally:
                        os.close(check_base_fd)
                finally:
                    os.close(check_root_fd)
            finally:
                while stack:
                    directory_fd, _, names = stack.pop()
                    close_names = getattr(names, "close", None)
                    if callable(close_names):
                        close_names()
                    if directory_fd != base_fd:
                        os.close(directory_fd)
                if base_fd >= 0:
                    os.close(base_fd)
                os.close(mapping_root_fd)

        return iterate()

    def open_regular_reader(self, path: str) -> _LocalBinaryReader:
        mapping, parent_parts, root_fd, parent_fd, target_name = self._open_private_parent(path, write=False)
        fd = -1
        try:
            fd = os.open(
                target_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise OSError(errno.EINVAL, "Private authority reads require regular files", path)
            entry_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not _same_inode(file_stat, entry_stat):
                raise OSError(errno.ESTALE, "Private authority file changed", path)
            _verify_private_parent(
                lambda: self._open_private_mapping_root(mapping),
                parent_parts,
                root_fd,
                parent_fd,
                path,
            )
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise
        finally:
            os.close(parent_fd)
            os.close(root_fd)
        return _LocalBinaryReader(fd)

    def open_atomic_writer(self, path: str) -> _LocalAtomicWriter:
        mapping, parent_parts, root_fd, parent_fd, target_name = self._open_private_parent(
            path,
            write=True,
            create_parents=True,
        )
        try:
            try:
                target_stat = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                target_stat = None
            if target_stat is not None and (not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1):
                raise OSError(errno.ELOOP, "Atomic target must be one regular link", path)
            while True:
                temp_name = f".deerflow-private-{uuid.uuid4().hex}"
                try:
                    fd = os.open(
                        temp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    break
                except FileExistsError:
                    continue
            return _LocalAtomicWriter(
                open_root=lambda: self._open_private_mapping_root(mapping),
                parent_parts=parent_parts,
                root_fd=root_fd,
                parent_fd=parent_fd,
                temp_name=temp_name,
                target_name=target_name,
                display_path=path,
                fd=fd,
            )
        except BaseException:
            os.close(parent_fd)
            os.close(root_fd)
            raise

    def remove_path(self, path: str) -> None:
        mapping, parent_parts, root_fd, parent_fd, target_name = self._open_private_parent(path, write=True)
        try:
            try:
                info = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(errno.EPERM, "Refusing to remove a non-regular path", path)
            _verify_private_parent(
                lambda: self._open_private_mapping_root(mapping),
                parent_parts,
                root_fd,
                parent_fd,
                path,
            )
            os.unlink(target_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
            os.close(root_fd)

    def _resolve_path_with_mapping(self, path: str) -> ResolvedPath:
        """
        Resolve container path to actual local path using mappings.

        Args:
            path: Path that might be a container path

        Returns:
            Resolved local path and the matched mapping, if any
        """
        path_str = str(path)

        mapping_match = self._find_path_mapping(path_str)
        if mapping_match is None:
            return ResolvedPath(path_str, None)

        mapping, relative = mapping_match
        local_root = Path(self._resolved_local_paths[mapping])
        resolved_path = (local_root / relative).resolve() if relative else local_root

        try:
            resolved_path.relative_to(local_root)
        except ValueError as exc:
            raise PermissionError(errno.EACCES, "Access denied: path escapes mounted directory", path_str) from exc

        return ResolvedPath(str(resolved_path), mapping)

    def _resolve_path(self, path: str) -> str:
        return self._resolve_path_with_mapping(path).path

    def _is_resolved_path_read_only(self, resolved: ResolvedPath) -> bool:
        return bool(resolved.mapping and resolved.mapping.read_only) or self._is_read_only_path(resolved.path)

    def _reverse_resolve_path(self, path: str) -> str:
        """
        Reverse resolve local path back to container path using mappings.

        Args:
            path: Local path that might need to be mapped to container path

        Returns:
            Container path if mapping exists, otherwise original path
        """
        normalized_path = path.replace("\\", "/")
        path_str = str(Path(normalized_path).resolve())

        # Try each mapping (longest local path first for more specific matches)
        for mapping in self._mappings_by_local_specificity:
            local_path_resolved = self._resolved_local_paths[mapping]
            if path_str == local_path_resolved or path_str.startswith(local_path_resolved + os.sep):
                # Replace the local path prefix with container path
                relative = path_str[len(local_path_resolved) :].lstrip(os.sep).replace(os.sep, "/")
                resolved = f"{mapping.container_path}/{relative}" if relative else mapping.container_path
                return resolved

        # No mapping found, return original path
        return path_str

    def _reverse_resolve_paths_in_output(self, output: str) -> str:
        """
        Reverse resolve local paths back to container paths in output string.

        Args:
            output: Output string that may contain local paths

        Returns:
            Output with local paths resolved to container paths
        """
        # Patterns are compiled once per sandbox (longest local path first for
        # correct prefix matching) and reused across calls.
        result = output
        for pattern in self._reverse_output_patterns:

            def replace_match(match: re.Match) -> str:
                matched_path = match.group(0)
                return self._reverse_resolve_path(matched_path)

            result = pattern.sub(replace_match, result)

        return result

    def _resolve_paths_in_command(self, command: str) -> str:
        """
        Resolve container paths to local paths in a command string.

        Args:
            command: Command string that may contain container paths

        Returns:
            Command with container paths resolved to local paths
        """
        pattern = self._command_pattern
        if pattern is None:
            return command

        def replace_match(match: re.Match) -> str:
            matched_path = match.group(0)
            # Normalize to forward slashes so bash doesn't interpret Windows
            # backslash sequences (\\U, \\a, \\d, \\s, \\n, \\t) as escapes.
            return self._resolve_path(matched_path).replace("\\", "/")

        return pattern.sub(replace_match, command)

    def _resolve_paths_in_content(self, content: str) -> str:
        """Resolve container paths to local paths in arbitrary file content.

        Unlike ``_resolve_paths_in_command`` which uses shell-aware boundary
        characters, this method treats the content as plain text and resolves
        every occurrence of a container path prefix.  Resolved paths are
        normalized to forward slashes to avoid backslash-escape issues on
        Windows hosts (e.g. ``C:\\Users\\..`` breaking Python string literals).

        Args:
            content: File content that may contain container paths.

        Returns:
            Content with container paths resolved to local paths (forward slashes).
        """
        pattern = self._content_pattern
        if pattern is None:
            return content

        def replace_match(match: re.Match) -> str:
            matched_path = match.group(0)
            resolved = self._resolve_path(matched_path)
            # Normalize to forward slashes so that Windows backslash paths
            # don't create invalid escape sequences in source files.
            return resolved.replace("\\", "/")

        return pattern.sub(replace_match, content)

    @staticmethod
    def _get_shell() -> str:
        """Detect available shell executable with fallback."""
        shell = LocalSandbox._find_first_available_shell(("/bin/zsh", "/bin/bash", "/bin/sh", "sh"))
        if shell is not None:
            return shell

        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            shell = LocalSandbox._find_first_available_shell(
                (
                    "pwsh",
                    "pwsh.exe",
                    "powershell",
                    "powershell.exe",
                    ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                    "cmd.exe",
                )
            )
            if shell is not None:
                return shell

            raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, `sh` on PATH, then PowerShell and cmd.exe fallbacks for Windows.")

        raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, and `sh` on PATH.")

    def execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        resolved_command = self.resolve_command_for_execution(command)
        return self.execute_prepared_command(
            resolved_command,
            shell=self.get_execution_shell(),
            env=env,
            timeout=timeout,
        )

    def resolve_command_for_execution(self, command: str) -> str:
        """Freeze virtual path mappings before a host approval is requested."""

        return self._resolve_paths_in_command(command)

    def get_execution_shell(self) -> str:
        """Return the exact shell executable included in an approval digest."""

        return self._get_shell()

    def execute_prepared_command(
        self,
        command: str,
        *,
        shell: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Execute a command whose path mapping and shell were already frozen."""

        return self.execute_prepared_command_result(
            command,
            shell=shell,
            env=env,
            timeout=timeout,
        ).output

    def execute_prepared_command_result(
        self,
        command: str,
        *,
        shell: str,
        env: dict[str, str] | None = None,
        prepared_base_env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        spawn_deadline_monotonic: float | None = None,
        spawn_authorization_guard: Callable[[], float] | None = None,
    ) -> LocalCommandExecutionResult:
        """Execute a frozen host plan and retain authoritative exit metadata."""

        # Validate ``env`` keys against the POSIX env-var rule. Defense in
        # depth: ``subprocess.run(env=...)`` does not go through a shell so a
        # metachar in a key here would not actually inject — but the public
        # ``Sandbox.execute_command`` contract is shared with the AIO sandbox,
        # which DOES splice keys into ``export <k>=<v>``. Enforcing the same
        # rule on both implementations keeps the contract consistent and forces
        # any new caller to use safe key names.
        _validate_extra_env(env)
        if timeout is None:
            timeout = DEFAULT_COMMAND_TIMEOUT_SECONDS

        # Inherit os.environ minus platform secrets, then layer any injected
        # request-scoped secrets on top (#3861). An explicit env is always passed
        # so platform credentials never leak into skill subprocesses.
        if prepared_base_env is None:
            sandbox_env = build_sandbox_env(env)
        else:
            if any(not isinstance(key, str) or not isinstance(value, str) or is_blocked_env_name(key) for key, value in prepared_base_env.items()):
                raise ValueError("prepared sandbox environment is invalid")
            # The approval continuation captured and fingerprinted this exact
            # sanitized mapping immediately before this call. Copying it does
            # not re-read ``os.environ`` and therefore closes the check/spawn
            # time-of-check/time-of-use gap.
            sandbox_env = dict(prepared_base_env)
            if env:
                sandbox_env.update(env)
        timed_out = False
        if os.name == "nt":
            if self._is_powershell(shell):
                args = [shell, "-NoProfile", "-Command", command]
            elif self._is_cmd_shell(shell):
                args = [shell, "/c", command]
            else:
                args = [shell, "-c", command]
                if self._is_msys_shell(shell):
                    sandbox_env = {
                        **sandbox_env,
                        "MSYS_NO_PATHCONV": "1",
                        "MSYS2_ARG_CONV_EXCL": "*",
                    }

            try:
                _authorize_process_spawn(
                    spawn_deadline_monotonic=spawn_deadline_monotonic,
                    spawn_authorization_guard=spawn_authorization_guard,
                )
                result = subprocess.run(
                    args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=sandbox_env,
                )
                stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                stdout = self._coerce_process_output(exc.stdout if exc.stdout is not None else exc.output)
                stderr = self._coerce_process_output(exc.stderr)
                returncode = 124
        else:
            args = [shell, "-c", command]
            stdout, stderr, returncode, timed_out = self._run_posix_command(
                args,
                timeout,
                sandbox_env,
                spawn_deadline_monotonic=spawn_deadline_monotonic,
                spawn_authorization_guard=spawn_authorization_guard,
            )

        output = stdout
        if stderr:
            output += f"\nStd Error:\n{stderr}" if output else stderr
        if timed_out:
            notice = self._format_timeout_notice(timeout)
            output += f"\n{notice}" if output else notice
        elif returncode != 0:
            output += f"\nExit Code: {returncode}"

        final_output = output if output else "(no output)"
        # Reverse resolve local paths back to container paths in output
        return LocalCommandExecutionResult(
            output=self._reverse_resolve_paths_in_output(final_output),
            stdout=self._reverse_resolve_paths_in_output(stdout),
            stderr=self._reverse_resolve_paths_in_output(stderr),
            exit_code=returncode,
            timed_out=timed_out,
        )

    @staticmethod
    def _run_posix_command(
        args: list[str],
        timeout: float,
        env: dict[str, str] | None = None,
        *,
        spawn_deadline_monotonic: float | None = None,
        spawn_authorization_guard: Callable[[], float] | None = None,
    ) -> tuple[str, str, int, bool]:
        """Run a command on POSIX with bounded pipe capture.

        ``subprocess.communicate()`` cannot be used here: a backgrounded
        long-lived process (``server &``) inherits stdout/stderr and keeps the
        pipes open, so ``communicate()`` would block until timeout even though
        the foreground shell already returned. Instead, daemon drain threads
        keep the pipes flowing while retaining only bounded output in memory.
        This lets the call return as soon as the foreground shell exits without
        handing backgrounded processes anonymous temp files that can grow
        invisibly. ``stdin`` is taken from ``/dev/null`` so commands that read
        stdin get immediate EOF, and ``start_new_session`` puts the command in
        its own process group so a genuinely blocking foreground command can be
        killed in full (children included) when it times out.

        ``env`` is forwarded to :class:`subprocess.Popen`; ``None`` means
        inherit the current process environment (the common case).

        Returns ``(stdout, stderr, returncode, timed_out)``.
        """
        timed_out = False
        stdout_read_fd, stdout_write_fd = os.pipe()
        stderr_read_fd, stderr_write_fd = os.pipe()
        try:
            _authorize_process_spawn(
                spawn_deadline_monotonic=spawn_deadline_monotonic,
                spawn_authorization_guard=spawn_authorization_guard,
            )
            process = subprocess.Popen(
                args,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_write_fd,
                stderr=stderr_write_fd,
                start_new_session=True,
                env=env,
            )
        except Exception:
            for fd in (stdout_read_fd, stdout_write_fd, stderr_read_fd, stderr_write_fd):
                try:
                    os.close(fd)
                except OSError:
                    # Preserve the original Popen failure; fd cleanup is best-effort.
                    pass
            raise
        finally:
            for fd in (stdout_write_fd, stderr_write_fd):
                try:
                    os.close(fd)
                except OSError:
                    # The write fd may already be closed by the exception cleanup above.
                    pass

        stdout_capture, stdout_thread = LocalSandbox._start_pipe_drain(stdout_read_fd, "deerflow-bash-stdout-drain")
        stderr_capture, stderr_thread = LocalSandbox._start_pipe_drain(stderr_read_fd, "deerflow-bash-stderr-drain")
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = None

        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                LocalSandbox._terminate_process_group(process)
            returncode = process.returncode if process.returncode is not None else 0
        finally:
            join_timeout = 10 if timed_out or not LocalSandbox._process_group_exists(process_group_id) else _PIPE_DRAIN_JOIN_TIMEOUT_SECONDS
            for thread in (stdout_thread, stderr_thread):
                thread.join(timeout=join_timeout)
                if thread.is_alive():
                    logger.debug("Subprocess output drain thread still active after command returned")

        stdout = stdout_capture.read()
        stderr = stderr_capture.read()
        return stdout, stderr, returncode, timed_out

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        """Kill the command's whole process group, then reap it.

        Falls back to killing just the direct child if the group is already
        gone (e.g. the command exited between the timeout and this call).
        """
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # The process group is already gone (the command exited in the race
            # between the timeout and this call); fall back to killing just the
            # direct child.
            try:
                process.kill()
            except OSError:
                # Direct child already reaped too — nothing left to kill.
                logger.debug("Process %s already exited before fallback kill", process.pid)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Process group for pid %s did not exit after SIGKILL", process.pid)

    def list_dir(self, path: str, max_depth=2) -> list[str]:
        resolved_path = self._resolve_path(path)
        entries = list_dir(resolved_path, max_depth)
        # Reverse resolve local paths back to container paths and preserve
        # list_dir's trailing "/" marker for directories.
        result: list[str] = []
        for entry in entries:
            is_dir = entry.endswith(("/", "\\"))
            reversed_entry = self._reverse_resolve_path(entry.rstrip("/\\")) if is_dir else self._reverse_resolve_path(entry)
            result.append(f"{reversed_entry}/" if is_dir and not reversed_entry.endswith("/") else reversed_entry)

        # Virtual sub-directory overlay: when a container path like /mnt/skills
        # has child mappings (public, custom, legacy) whose local_path targets
        # are outside the resolved host directory (symlinks or bind-mount style),
        # the ``list_dir`` utility skips them for security. We patch those
        # missing virtual children back in so the agent can discover them via
        # ``ls /mnt/skills``.
        container_path = path.rstrip("/")
        existing_dirs = {e.rstrip("/") for e in result if e.endswith("/")}
        for mapping in self.path_mappings:
            # A mapping is a virtual child if:
            # 1. Its container_path is a direct child of the requested path
            # 2. It is NOT already present in the result (was skipped by list_dir)
            if mapping.container_path.startswith(container_path + "/"):
                child_rel = mapping.container_path[len(container_path) + 1 :]
                # Only direct children (no further slashes), e.g. "public", "custom"
                if "/" not in child_rel and child_rel not in existing_dirs:
                    # Verify the host path exists so we don't add phantom entries
                    try:
                        if Path(mapping.local_path).resolve().is_dir():
                            result.append(f"{mapping.container_path}/")
                    except OSError:
                        pass

        return sorted(result)

    def read_file(self, path: str) -> str:
        resolved_path = self._resolve_path(path)
        try:
            with open(resolved_path, encoding="utf-8") as f:
                content = f.read()
            # Only reverse-resolve paths in files that were previously written
            # by write_file (agent-authored content). User-uploaded files,
            # external tool output, and other non-agent content should not be
            # silently rewritten — see discussion on PR #1935.
            if resolved_path in self._agent_written_paths:
                content = self._reverse_resolve_paths_in_output(content)
            return content
        except OSError as e:
            # Re-raise with the original path for clearer error messages, hiding internal resolved paths
            raise type(e)(e.errno, e.strerror, path) from None

    def download_file(self, path: str) -> bytes:
        normalised = path.replace("\\", "/")
        stripped_path = normalised.lstrip("/")
        allowed_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            logger.error("Refused download outside allowed directory: path=%s, allowed_prefix=%s", path, VIRTUAL_PATH_PREFIX)
            raise PermissionError(errno.EACCES, f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}'", path)

        resolved_path = self._resolve_path(path)
        max_download_size = 100 * 1024 * 1024
        try:
            file_size = os.path.getsize(resolved_path)
            if file_size > max_download_size:
                raise OSError(errno.EFBIG, f"File exceeds maximum download size of {max_download_size} bytes", path)
            # TOCTOU note: the file could grow between getsize() and read(); accepted
            # tradeoff since this is a controlled sandbox environment.
            with open(resolved_path, "rb") as f:
                return f.read()
        except OSError as e:
            # Re-raise with the original path for clearer error messages, hiding internal resolved paths
            raise type(e)(e.errno, e.strerror, path) from None

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            # Resolve container paths in content to local paths
            # using the content-specific resolver (forward-slash safe)
            resolved_content = self._resolve_paths_in_content(content)
            mode = "a" if append else "w"
            with open(resolved_path, mode, encoding="utf-8") as f:
                f.write(resolved_content)
            # Track this path so read_file knows to reverse-resolve on read.
            # Only agent-written files get reverse-resolved; user uploads and
            # external tool output are left untouched.
            self._agent_written_paths.add(resolved_path)
        except OSError as e:
            # Re-raise with the original path for clearer error messages, hiding internal resolved paths
            raise type(e)(e.errno, e.strerror, path) from None

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_glob_matches(resolved_path, pattern, include_dirs=include_dirs, max_results=max_results)
        return [self._reverse_resolve_path(match) for match in matches], truncated

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_grep_matches(
            resolved_path,
            pattern,
            glob_pattern=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
        return [
            GrepMatch(
                path=self._reverse_resolve_path(match.path),
                line_number=match.line_number,
                line=match.line,
            )
            for match in matches
        ], truncated

    def update_file(self, path: str, content: bytes) -> None:
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(resolved_path, "wb") as f:
                f.write(content)
        except OSError as e:
            # Re-raise with the original path for clearer error messages, hiding internal resolved paths
            raise type(e)(e.errno, e.strerror, path) from None
