"""Provider-neutral secure file primitives for run-isolated remote sandboxes.

The gateway never asks a remote provider to interpret ``find``/``stat`` output.
Instead, a fixed guest program performs descriptor-anchored operations and
returns a bounded JSON envelope.  User paths and binary chunks are transported
as JSON/base64 data, never interpolated into the guest command string.

Remote providers must use a fresh sandbox for every private run.  That lifecycle
is what prevents a stale guest process from crossing project/run scopes; this
module supplies the same no-follow, inode-bound, atomic file semantics inside
that isolated guest.
"""

from __future__ import annotations

import base64
import json
import posixpath
from collections.abc import Callable, Iterator
from typing import Any

from deerflow.sandbox.sandbox import (
    PRIVATE_FILE_IO_CHUNK_SIZE,
    SandboxAtomicWriter,
    SandboxBinaryReader,
    SandboxFileInfo,
)

PRIVATE_GUEST_REQUEST_ENV = "DEERFLOW_PRIVATE_FILE_REQUEST_B64"
_MAX_TRANSPORT_CHUNK = 48 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_PATH_BYTES = 4096

# Fixed server-owned guest source.  The only input is one JSON/base64 envelope
# supplied separately by the provider adapter.  Keep imports stdlib-only so the
# same program works in AIO, e2b code-interpreter, and minimal BoxLite images
# that contain Python 3.
PRIVATE_GUEST_SCRIPT = r"""
import base64, errno, json, os, stat, sys, uuid

ENV = "DEERFLOW_PRIVATE_FILE_REQUEST_B64"
MAX_REQUEST = 2 * 1024 * 1024

def fail(code, message):
    print(json.dumps({"ok": False, "error": code, "message": str(message)[:300]}, separators=(",", ":")))
    raise SystemExit(0)

def emit(data=None):
    print(json.dumps({"ok": True, "data": data or {}}, separators=(",", ":")))
    raise SystemExit(0)

def same(a, b):
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino

def checked_abs(value):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or len(os.fsencode(value)) > 4096:
        fail("permission", "invalid private path")
    parts = value.split("/")
    if any(p in (".", "..") for p in parts):
        fail("permission", "invalid private path")
    normalized = os.path.normpath(value)
    if normalized != value.rstrip("/") and value != "/":
        fail("permission", "non-canonical private path")
    return normalized

def relative(root, path):
    root = checked_abs(root)
    path = checked_abs(path)
    if path != root and not path.startswith(root.rstrip("/") + "/"):
        fail("permission", "private path escaped root")
    tail = path[len(root):].lstrip("/")
    parts = [] if not tail else tail.split("/")
    if any((not p or p.startswith(".deerflow-private-")) for p in parts):
        fail("permission", "reserved private path")
    return root, parts

def open_absolute_dir(path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.strip("/").split("/") if path != "/" else ():
            newfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = newfd
        return fd
    except BaseException:
        os.close(fd)
        raise

def open_parent(root, parts, create=False):
    fd = open_absolute_dir(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            newfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = newfd
        return fd, (parts[-1] if parts else "")
    except BaseException:
        os.close(fd)
        raise

def regular_identity(st):
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        fail("unsafe", "private file is not a single-link regular file")
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size}

try:
    encoded = os.environ.pop(ENV)
    if len(encoded) > MAX_REQUEST * 2:
        fail("limit", "private request too large")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MAX_REQUEST:
        fail("limit", "private request too large")
    request = json.loads(raw)
    if not isinstance(request, dict) or request.get("version") != 1:
        fail("protocol", "invalid private request")
    action = request.get("action")
    root, parts = relative(request.get("root"), request.get("path"))

    if action == "scan":
        limit = request.get("max_entries")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100000:
            fail("limit", "invalid entry limit")
        root_fd = open_absolute_dir(request["path"])
        entries = []
        def walk(fd, virtual):
            try:
                names = sorted(os.listdir(fd), key=os.fsencode)
                for name in names:
                    if name.startswith(".deerflow-private-"):
                        continue
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode): kind = "symlink"
                    elif stat.S_ISDIR(st.st_mode): kind = "directory"
                    elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1: kind = "regular"
                    else: kind = "other"
                    child = virtual.rstrip("/") + "/" + name
                    entries.append({"path": child, "size": st.st_size, "file_type": kind})
                    if len(entries) > limit:
                        fail("entry_limit", "private scan entry limit exceeded")
                    if kind == "directory":
                        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            if not same(st, os.fstat(child_fd)):
                                fail("changed", "private directory changed during scan")
                            walk(child_fd, child)
                        finally:
                            os.close(child_fd)
            except FileNotFoundError:
                fail("changed", "private tree changed during scan")
        display_path = request.get("display_path")
        if not isinstance(display_path, str) or not display_path.startswith("/mnt/user-data"):
            fail("protocol", "invalid private display path")
        try:
            walk(root_fd, display_path)
        finally:
            os.close(root_fd)
        emit({"entries": entries})

    if not parts:
        fail("permission", "private root cannot be used as a file")
    parent, name = open_parent(root, parts, create=(action == "begin_write"))
    try:
        if action == "open_reader":
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                identity = regular_identity(os.fstat(fd))
                if not same(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    fail("changed", "private file changed while opening")
                emit(identity)
            finally:
                os.close(fd)

        if action == "read":
            offset = request.get("offset")
            size = request.get("size")
            expected = request.get("expected")
            if not isinstance(offset, int) or offset < 0 or not isinstance(size, int) or not 1 <= size <= 1048576 or not isinstance(expected, dict):
                fail("limit", "invalid bounded read")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                st = os.fstat(fd)
                current = regular_identity(st)
                if any(current.get(k) != expected.get(k) for k in ("dev", "ino", "size")):
                    fail("changed", "private file changed after open")
                data = os.pread(fd, size, offset)
                after = os.fstat(fd)
                path_st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not same(st, after) or not same(after, path_st) or after.st_size != expected.get("size"):
                    fail("changed", "private file changed during read")
                emit({"content": base64.b64encode(data).decode("ascii")})
            finally:
                os.close(fd)

        if action == "begin_write":
            token = ".deerflow-private-" + uuid.uuid4().hex
            fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                st = os.fstat(fd)
                os.fsync(fd)
                emit({"token": token, "dev": st.st_dev, "ino": st.st_ino})
            finally:
                os.close(fd)

        if action == "remove":
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode): fail("unsafe", "private directory removal refused")
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass
            emit()

        token = request.get("token")
        expected = request.get("expected")
        if not isinstance(token, str) or not token.startswith(".deerflow-private-") or len(token) != 50 or not isinstance(expected, dict):
            fail("protocol", "invalid private writer handle")

        if action == "append":
            content = base64.b64decode(request.get("content", ""), validate=True)
            if not 0 < len(content) <= 49152:
                fail("limit", "invalid bounded write")
            fd = os.open(token, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                st = os.fstat(fd)
                regular_identity(st)
                if st.st_dev != expected.get("dev") or st.st_ino != expected.get("ino"):
                    fail("changed", "private staging file changed")
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0: fail("io", "short private write")
                    view = view[written:]
                os.fsync(fd)
                path_st = os.stat(token, dir_fd=parent, follow_symlinks=False)
                if not same(st, path_st): fail("changed", "private staging path changed")
                emit({"size": path_st.st_size})
            finally:
                os.close(fd)

        if action == "commit":
            temp_st = os.stat(token, dir_fd=parent, follow_symlinks=False)
            regular_identity(temp_st)
            if temp_st.st_dev != expected.get("dev") or temp_st.st_ino != expected.get("ino"):
                fail("changed", "private staging file changed")
            try:
                target_st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                target_st = None
            if target_st is not None and (not stat.S_ISREG(target_st.st_mode) or target_st.st_nlink != 1):
                fail("unsafe", "private publish target is unsafe")
            os.replace(token, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            published = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not same(temp_st, published): fail("changed", "private publish changed target")
            emit({"size": published.st_size})

        if action == "abort":
            try:
                temp_st = os.stat(token, dir_fd=parent, follow_symlinks=False)
                if temp_st.st_dev != expected.get("dev") or temp_st.st_ino != expected.get("ino"):
                    fail("changed", "private staging file changed")
                os.unlink(token, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass
            emit()

        fail("protocol", "unsupported private operation")
    finally:
        os.close(parent)
except KeyError:
    fail("protocol", "missing private request field")
except FileNotFoundError as exc:
    fail("not_found", exc)
except PermissionError as exc:
    fail("permission", exc)
except (ValueError, TypeError, json.JSONDecodeError) as exc:
    fail("protocol", exc)
except OSError as exc:
    fail("io", exc)
except SystemExit:
    raise
except BaseException as exc:
    fail("internal", type(exc).__name__)
"""


def encode_guest_request(request: dict[str, object]) -> str:
    """Encode one validated request for a provider's structured env field."""

    raw = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Private guest request exceeds transport limit")
    return base64.b64encode(raw).decode("ascii")


def decode_guest_response(output: str) -> dict[str, object]:
    """Validate the fixed guest program's bounded response envelope."""

    if not isinstance(output, str):
        raise OSError("Invalid private guest response")
    raw = output.strip().encode()
    if not raw or len(raw) > _MAX_RESPONSE_BYTES:
        raise OSError("Invalid private guest response size")
    try:
        response = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("Invalid private guest JSON response") from exc
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise OSError("Invalid private guest response schema")
    return response


class RemotePrivateFileAuthority:
    """Secure binary I/O facade backed by the fixed guest program."""

    def __init__(
        self,
        *,
        execute: Callable[[dict[str, object]], dict[str, object]],
        resolve_path: Callable[[str], str],
    ) -> None:
        self._execute = execute
        self._resolve_path = resolve_path

    @staticmethod
    def _validate_virtual_path(path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "\\" in path or len(path.encode()) > _MAX_PATH_BYTES:
            raise PermissionError("Invalid private sandbox path")
        parts = path.split("/")
        if any(part in {".", ".."} for part in parts):
            raise PermissionError("Private sandbox path traversal refused")
        normalized = posixpath.normpath(path)
        if normalized != path.rstrip("/") and path != "/":
            raise PermissionError("Non-canonical private sandbox path")
        if normalized != "/mnt/user-data" and not normalized.startswith("/mnt/user-data/"):
            raise PermissionError("Private sandbox path escaped user-data")
        if any(part.startswith(".deerflow-private-") for part in parts):
            raise PermissionError("Reserved private sandbox path")
        return normalized

    def _request(
        self,
        action: str,
        path: str,
        **values: object,
    ) -> dict[str, Any]:
        virtual = self._validate_virtual_path(path)
        virtual_root = "/mnt/user-data"
        resolved_root = self._resolve_path(virtual_root)
        resolved_path = self._resolve_path(virtual)
        if not isinstance(resolved_root, str) or not isinstance(resolved_path, str):
            raise OSError("Invalid private sandbox path mapping")
        request: dict[str, object] = {
            "version": 1,
            "action": action,
            "root": resolved_root,
            "path": resolved_path,
            "display_path": virtual,
            **values,
        }
        response = self._execute(request)
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise OSError("Invalid private guest response schema")
        if response["ok"] is False:
            code = response.get("error")
            message = response.get("message")
            if not isinstance(code, str) or not isinstance(message, str) or len(message) > 300:
                raise OSError("Invalid private guest error response")
            if code == "permission":
                raise PermissionError(message)
            if code == "not_found":
                raise FileNotFoundError(message)
            if code == "entry_limit":
                raise OSError("Private scan entry limit exceeded")
            raise OSError(f"Private sandbox file operation failed: {code}: {message}")
        data = response.get("data", {})
        if not isinstance(data, dict):
            raise OSError("Invalid private guest data response")
        return data

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
    ) -> Iterator[SandboxFileInfo]:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or not 1 <= max_entries <= 100_000:
            raise ValueError("Invalid private scan entry limit")
        data = self._request("scan", root, max_entries=max_entries)
        entries = data.get("entries")
        if not isinstance(entries, list) or len(entries) > max_entries:
            raise OSError("Invalid private scan response")
        result: list[SandboxFileInfo] = []
        for item in entries:
            if not isinstance(item, dict):
                raise OSError("Invalid private scan entry")
            path = item.get("path")
            size = item.get("size")
            file_type = item.get("file_type")
            if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0 or file_type not in {"regular", "directory", "symlink", "other"}:
                raise OSError("Invalid private scan entry schema")
            result.append(
                SandboxFileInfo(
                    path=self._validate_virtual_path(path),
                    size=size,
                    file_type=file_type,
                )
            )
        return iter(result)

    def open_regular_reader(self, path: str) -> SandboxBinaryReader:
        identity = self._request("open_reader", path)
        return _RemoteBinaryReader(self, self._validate_virtual_path(path), identity)

    def open_atomic_writer(self, path: str) -> SandboxAtomicWriter:
        identity = self._request("begin_write", path)
        return _RemoteAtomicWriter(self, self._validate_virtual_path(path), identity)

    def remove_path(self, path: str) -> None:
        self._request("remove", path)


class _RemoteBinaryReader:
    def __init__(
        self,
        authority: RemotePrivateFileAuthority,
        path: str,
        identity: dict[str, Any],
    ) -> None:
        if not all(type(identity.get(key)) is int for key in ("dev", "ino", "size")):
            raise OSError("Invalid private reader identity")
        self._authority = authority
        self._path = path
        self._identity = {key: identity[key] for key in ("dev", "ino", "size")}
        self._offset = 0
        self._closed = False

    def read(self, size: int) -> bytes:
        if self._closed:
            raise OSError("Private reader is closed")
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox reads must be bounded to 1 MiB")
        data = self._authority._request(
            "read",
            self._path,
            offset=self._offset,
            size=size,
            expected=self._identity,
        )
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise OSError("Invalid private read response")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise OSError("Invalid private read content") from exc
        if len(content) > size:
            raise OSError("Private read exceeded requested bound")
        self._offset += len(content)
        return content

    def close(self) -> None:
        self._closed = True


class _RemoteAtomicWriter:
    def __init__(
        self,
        authority: RemotePrivateFileAuthority,
        path: str,
        identity: dict[str, Any],
    ) -> None:
        token = identity.get("token")
        if not isinstance(token, str) or not token.startswith(".deerflow-private-") or len(token) != 50 or not all(type(identity.get(key)) is int for key in ("dev", "ino")):
            raise OSError("Invalid private writer identity")
        self._authority = authority
        self._path = path
        self._token = token
        self._identity = {key: identity[key] for key in ("dev", "ino")}
        self._closed = False

    def write(self, content: bytes) -> None:
        if self._closed:
            raise OSError("Private writer is closed")
        if not isinstance(content, bytes) or not 0 < len(content) <= PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox writes must be bounded to 1 MiB")
        for offset in range(0, len(content), _MAX_TRANSPORT_CHUNK):
            chunk = content[offset : offset + _MAX_TRANSPORT_CHUNK]
            self._authority._request(
                "append",
                self._path,
                token=self._token,
                expected=self._identity,
                content=base64.b64encode(chunk).decode("ascii"),
            )

    def commit(self) -> None:
        if self._closed:
            raise OSError("Private writer is closed")
        try:
            self._authority._request(
                "commit",
                self._path,
                token=self._token,
                expected=self._identity,
            )
        except BaseException:
            self.abort()
            raise
        self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._authority._request(
            "abort",
            self._path,
            token=self._token,
            expected=self._identity,
        )
