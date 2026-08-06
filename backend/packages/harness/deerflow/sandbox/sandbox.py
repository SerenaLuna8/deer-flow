import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from langgraph.errors import GraphBubbleUp

from deerflow.sandbox.search import GrepMatch

# POSIX env-var name rule: letter or underscore, then letters/digits/underscores.
# Used to validate ``env`` keys before they reach a sandbox implementation.
# No current implementation splices a key into a shell string — the local
# sandbox passes the dict to ``subprocess.run(env=...)`` (no shell), the AIO
# sandbox forwards it via the ``bash.exec`` structured ``env`` field, and e2b
# forwards it as the SDK's ``envs``. The check is defense-in-depth for the
# contract: a future shell-splicing implementation must not have to re-derive
# its own rule.
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

AUTHORIZATION_REVOKED_REASON = "authorization_revoked"
PRIVATE_FILE_IO_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SandboxFileInfo:
    path: str
    size: int
    file_type: str


class SandboxBinaryReader(Protocol):
    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class SandboxAtomicWriter(Protocol):
    def write(self, content: bytes) -> None: ...

    def commit(self) -> None: ...

    def abort(self) -> None: ...


class AuthorizationRevoked(GraphBubbleUp):
    """Internal control-flow signal with one stable, non-sensitive public reason."""

    def __init__(self) -> None:
        super().__init__(AUTHORIZATION_REVOKED_REASON)


class AuthorizationBoundary(Protocol):
    """App-supplied run authorization checks; the harness stays app-agnostic."""

    async def before_model_call(self) -> None: ...

    async def before_tool_call(self) -> None: ...

    async def before_read_only_tool_call(self) -> None: ...

    async def before_mcp_call(self) -> None: ...

    async def before_mcp_tool_dispatch(self) -> None: ...

    async def before_sandbox_write(self) -> None: ...

    async def before_sandbox_exec(self) -> None: ...

    async def before_checkpoint_read(self) -> None: ...

    async def before_checkpoint_write(self) -> None: ...

    async def before_file_finalization(self) -> None: ...


async def check_authorization_boundary(
    runtime_context: object | None,
    operation: str,
) -> None:
    """Call a trusted boundary when present; legacy runs remain a no-op."""

    if not isinstance(runtime_context, Mapping):
        return
    boundary = runtime_context.get("__authorization_boundary")
    method = getattr(boundary, operation, None)
    if not callable(method) and operation == "before_read_only_tool_call":
        method = getattr(boundary, "before_tool_call", None)
    if callable(method):
        await method()
        return
    checker = runtime_context.get("__authorization_checker")
    if callable(checker):
        result = checker()
        if isinstance(result, Awaitable):
            await result


def _validate_extra_env(extra_env: dict[str, str] | None) -> None:
    """Reject ``env`` keys that are not valid POSIX env-var names.

    The :meth:`Sandbox.execute_command` contract accepts arbitrary ``str``
    keys. Today no implementation splices a key into a shell string — the
    local sandbox passes the dict to ``subprocess.run(env=...)`` (no shell),
    the AIO sandbox forwards it via the ``bash.exec`` structured ``env``
    field (no command-string splice), and e2b forwards it as the SDK's
    ``envs``. Enforcing the POSIX env-name rule in the abstract layer is
    defense-in-depth for the contract: a future implementation that does
    route a key through a shell must not have to re-derive its own
    validation rule, and a caller passing a key derived from config /
    payload / user input fails fast with ``ValueError`` instead of silently
    producing an exploit should a future implementation regress to splicing.

    Raises:
        ValueError: When ``extra_env`` is not None and any key does not
            match ``^[A-Za-z_][A-Za-z0-9_]*$``. ``None`` and empty dicts
            pass through unchanged.
    """
    if not extra_env:
        return
    for key in extra_env:
        if not isinstance(key, str) or not _ENV_NAME_PATTERN.fullmatch(key):
            raise ValueError(f"extra_env key {key!r} is not a valid POSIX environment variable name (must match ^[A-Za-z_][A-Za-z0-9_]*$). This protects shell-using sandbox implementations from command injection via the key.")


class Sandbox(ABC):
    """Abstract base class for sandbox environments"""

    _id: str

    def __init__(self, id: str):
        self._id = id
        self._private_atomic_writers: dict[str, SandboxAtomicWriter] = {}
        self._private_regular_readers: dict[str, SandboxBinaryReader] = {}

    @property
    def id(self) -> str:
        return self._id

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
    ) -> Iterator[SandboxFileInfo]:
        """List regular and rejected objects without following links.

        Providers that do not implement this private-work boundary fail closed.
        """

        from deerflow.sandbox.exceptions import SandboxRuntimeError

        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox")

    def open_regular_reader(self, path: str) -> SandboxBinaryReader:
        from deerflow.sandbox.exceptions import SandboxRuntimeError

        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox")

    def open_atomic_writer(self, path: str) -> SandboxAtomicWriter:
        from deerflow.sandbox.exceptions import SandboxRuntimeError

        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox")

    def remove_path(self, path: str) -> None:
        from deerflow.sandbox.exceptions import SandboxRuntimeError

        raise SandboxRuntimeError("Private file authority is unsupported by this sandbox")

    # Stateful compatibility facade used by the app authority. Handles are
    # opaque and never contain physical paths.
    def begin_atomic_file(self, path: str) -> str:
        import uuid

        writer = self.open_atomic_writer(path)
        handle = uuid.uuid4().hex
        self._private_atomic_writers[handle] = writer
        return handle

    def append_atomic_file(self, handle: str, content: bytes) -> None:
        if not isinstance(content, bytes) or not 0 < len(content) <= PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox writes must be bounded to 1 MiB")
        self._private_atomic_writers[handle].write(content)

    def publish_atomic_file(self, handle: str) -> None:
        writer = self._private_atomic_writers[handle]
        writer.commit()
        self._private_atomic_writers.pop(handle, None)

    def abort_atomic_file(self, handle: str) -> None:
        writer = self._private_atomic_writers.pop(handle, None)
        if writer is not None:
            writer.abort()

    def remove_file(self, path: str) -> None:
        self.remove_path(path)

    def open_regular_file(self, path: str) -> str:
        import uuid

        reader = self.open_regular_reader(path)
        handle = uuid.uuid4().hex
        self._private_regular_readers[handle] = reader
        return handle

    def read_regular_file(self, handle: str, max_bytes: int) -> bytes:
        if max_bytes != PRIVATE_FILE_IO_CHUNK_SIZE:
            raise ValueError("Private sandbox reads use the fixed 1 MiB bound")
        return self._private_regular_readers[handle].read(max_bytes)

    def close_regular_file(self, handle: str) -> None:
        reader = self._private_regular_readers.pop(handle, None)
        if reader is not None:
            reader.close()

    def close_private_file_authority(self) -> None:
        """Close every opaque secure-I/O handle owned by this lease."""

        writers = tuple(self._private_atomic_writers.values())
        readers = tuple(self._private_regular_readers.values())
        self._private_atomic_writers.clear()
        self._private_regular_readers.clear()
        for writer in writers:
            try:
                writer.abort()
            except Exception:
                pass
        for reader in readers:
            try:
                reader.close()
            except Exception:
                pass

    @abstractmethod
    def execute_command(
        self,
        command: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Execute bash command in sandbox.

        Args:
            command: The command to execute.
            env: Optional per-call environment variables to inject into the
                command's process. Used to pass request-scoped secrets (e.g. a
                short-lived end-user token for skill scripts, issue #3861, or a
                GitHub App installation token for ``git push`` / ``gh``) without
                placing them in the prompt, tool arguments, or the command
                string. When ``None`` the sandbox uses its default environment.
                Keys must be valid POSIX environment-variable names
                (``^[A-Za-z_][A-Za-z0-9_]*$``); implementations validate
                via :func:`_validate_extra_env` before use. Values are
                arbitrary strings — shell-using implementations
                ``shlex.quote`` them on splice.
            timeout: Optional per-call wall-clock timeout in seconds. Local
                sandboxes use this to bound host bash commands so long-lived
                foreground processes cannot hang a turn indefinitely. Remote/AIO
                implementations may ignore it when their backend does not expose
                an equivalent command-timeout control separate from its own API
                timeouts.

        Returns:
            The standard or error output of the command.

        Raises:
            ValueError: when an ``env`` key is not a valid env-var name.
        """
        pass

    @abstractmethod
    def read_file(self, path: str) -> str:
        """Read the content of a file.

        Args:
            path: The absolute path of the file to read.

        Returns:
            The content of the file.
        """
        pass

    @abstractmethod
    def download_file(self, path: str) -> bytes:
        """Download the binary content of a file.

        Args:
            path: The absolute path of the file to download.

        Returns:
            Raw file bytes.

        Raises:
            PermissionError: If path traversal is detected or the path is outside
                the allowed virtual prefix.
            OSError: If the file cannot be read or does not exist.  Both local
                and remote implementations must raise ``OSError`` so callers
                have a single exception type to handle.
        """
        pass

    @abstractmethod
    def list_dir(self, path: str, max_depth=2) -> list[str]:
        """List the contents of a directory.

        Args:
            path: The absolute path of the directory to list.
            max_depth: The maximum depth to traverse. Default is 2.

        Returns:
            The contents of the directory.
        """
        pass

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """Write content to a file.

        Args:
            path: The absolute path of the file to write to.
            content: The text content to write to the file.
            append: Whether to append the content to the file. If False, the file will be created or overwritten.
        """
        pass

    @abstractmethod
    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """Find paths that match a glob pattern under a root directory."""
        pass

    @abstractmethod
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
        """Search for matches inside a text file or files under a directory."""
        pass

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None:
        """Update a file with binary content.

        Args:
            path: The absolute path of the file to update.
            content: The binary content to write to the file.
        """
        pass
