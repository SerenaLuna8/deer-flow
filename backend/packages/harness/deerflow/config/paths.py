import hashlib
import logging
import os
import re
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

from deerflow.config.runtime_paths import runtime_home

# Virtual path prefix seen by agents inside the sandbox
VIRTUAL_PATH_PREFIX = "/mnt/user-data"

_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNSAFE_USER_ID_CHAR_RE = re.compile(r"[^A-Za-z0-9_\-]")
_SAFE_USER_ID_DIGEST_HEX_LEN = 16

logger = logging.getLogger(__name__)


def _default_local_base_dir() -> Path:
    """Return the caller project's writable ActWeave state directory."""
    return runtime_home()


def _validate_thread_id(thread_id: str) -> str:
    """Validate a thread ID before using it in filesystem paths."""
    if not _SAFE_THREAD_ID_RE.match(thread_id):
        raise ValueError(f"Invalid thread_id {thread_id!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
    return thread_id


def _validate_user_id(user_id: str) -> str:
    """Validate a user ID before using it in filesystem paths."""
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(f"Invalid user_id {user_id!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
    return user_id


def make_safe_user_id(raw: str) -> str:
    """Normalize an external identity into the user-id charset (``[A-Za-z0-9_-]``).

    IM channel ids (Feishu/Slack/Telegram) may contain characters that
    :func:`_validate_user_id` rejects. Already-safe ids pass through unchanged;
    lossy ones get a short digest suffix so two distinct inputs never share a
    storage bucket.
    """
    if not raw:
        raise ValueError("user_id must be a non-empty string.")
    sanitized = _UNSAFE_USER_ID_CHAR_RE.sub("-", raw)
    if sanitized == raw:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SAFE_USER_ID_DIGEST_HEX_LEN]
    return f"{sanitized}-{digest}"


def _legacy_safe_user_id(raw: str, sanitized: str) -> str:
    """Bucket name produced by the previous (SHA-1) digest revision for ``raw``."""
    digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:_SAFE_USER_ID_DIGEST_HEX_LEN]
    return f"{sanitized}-{digest}"


def _join_host_path(base: str, *parts: str) -> str:
    """Join host filesystem path segments while preserving native style.

    Docker Desktop on Windows expects bind mount sources to stay in Windows
    path form (for example ``C:\\repo\\backend\\.fluva-flow``).  Using
    ``Path(base) / ...`` on a POSIX host can accidentally rewrite those paths
    with mixed separators, so this helper preserves the original style.
    """
    if not parts:
        return base

    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\") or "\\" in base:
        result = PureWindowsPath(base)
        for part in parts:
            result /= part
        return str(result)

    result = Path(base)
    for part in parts:
        result /= part
    return str(result)


def join_host_path(base: str, *parts: str) -> str:
    """Join host filesystem path segments while preserving native style."""
    return _join_host_path(base, *parts)


def _validated_host_path(
    value: str,
) -> PurePosixPath | PureWindowsPath:
    """Parse one absolute daemon-view path without touching that filesystem."""

    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError("Invalid host materialization path")
    path = PureWindowsPath(value) if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\") or "\\" in value else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid host materialization path")
    return path


class Paths:
    """
    Centralized path configuration for ActWeave application data.

    Directory layout (host side):
        {base_dir}/
        ├── USER.md          <-- global user profile (injected into all agents)
        ├── agents/
        │   └── {agent_name}/
        │       ├── config.yaml
        │       └── SOUL.md  <-- agent personality/identity (injected alongside lead prompt)
        └── threads/
            └── {thread_id}/
                └── user-data/         <-- mounted as /mnt/user-data/ inside sandbox
                    ├── workspace/     <-- /mnt/user-data/workspace/
                    ├── uploads/       <-- /mnt/user-data/uploads/
                    └── outputs/       <-- /mnt/user-data/outputs/

    BaseDir resolution (in priority order):
        1. Constructor argument `base_dir`
        2. ACT_WEAVE_HOME environment variable
        3. Caller project fallback: `{project_root}/.fluva-flow`
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def host_base_dir(self) -> Path:
        """Host-visible base directory for Sandbox volume mount sources.

        Set ``ACT_WEAVE_HOST_BASE_DIR`` when a Sandbox daemon or Provisioner
        resolves a host-side path corresponding to the Worker's ``base_dir``.
        It falls back to ``base_dir`` when no alternate view is configured.
        """
        if env := os.getenv("ACT_WEAVE_HOST_BASE_DIR"):
            return Path(env)
        return self.base_dir

    def _host_base_dir_str(self) -> str:
        """Return the host base dir as a raw string for bind mounts."""
        if env := os.getenv("ACT_WEAVE_HOST_BASE_DIR"):
            return env
        return str(self.base_dir)

    @property
    def base_dir(self) -> Path:
        """Root directory for all application data."""
        if self._base_dir is not None:
            return self._base_dir

        if env_home := os.getenv("ACT_WEAVE_HOME"):
            return Path(env_home).resolve()

        return _default_local_base_dir()

    def run_skill_materialization_root(self) -> Path:
        """Dedicated Worker root for exact, per-attempt Skill trees."""

        return self.base_dir / "run-skill-materializations"

    def host_run_skill_materialization_root(self) -> str:
        """Sandbox-runtime view of the dedicated Skill materialization root."""

        root = _join_host_path(
            self._host_base_dir_str(),
            "run-skill-materializations",
        )
        _validated_host_path(root)
        return root

    def host_run_skill_materialization_path(
        self,
        worker_path: Path,
    ) -> str:
        """Translate one exact Worker-contained path into the daemon view."""

        if not isinstance(worker_path, Path) or not worker_path.is_absolute() or ".." in worker_path.parts:
            raise ValueError("Invalid Worker materialization path")
        try:
            trusted_root = self.run_skill_materialization_root().resolve(
                strict=True,
            )
            resolved = worker_path.resolve(strict=True)
            relative = resolved.relative_to(trusted_root)
        except (OSError, ValueError):
            raise ValueError("Worker materialization path is outside the trusted root") from None
        if not relative.parts or worker_path != resolved:
            raise ValueError("Worker materialization path is outside the trusted root")
        return _join_host_path(
            self.host_run_skill_materialization_root(),
            *relative.parts,
        )

    def worker_run_skill_materialization_path(
        self,
        host_path: str,
    ) -> Path:
        """Map a daemon-view path back to its exact Worker-visible source."""

        candidate = _validated_host_path(host_path)
        trusted_host_root = _validated_host_path(
            self.host_run_skill_materialization_root(),
        )
        if type(candidate) is not type(trusted_host_root):
            raise ValueError("Host materialization path is outside the trusted root")
        try:
            relative = candidate.relative_to(trusted_host_root)
        except ValueError:
            raise ValueError("Host materialization path is outside the trusted root") from None
        if not relative.parts:
            raise ValueError("Host materialization path is outside the trusted root")
        worker_path = self.run_skill_materialization_root().joinpath(
            *relative.parts,
        )
        try:
            trusted_worker_root = self.run_skill_materialization_root().resolve(
                strict=True,
            )
            resolved = worker_path.resolve(strict=True)
            resolved.relative_to(trusted_worker_root)
        except (OSError, ValueError):
            raise ValueError("Host materialization path has no trusted Worker source") from None
        return resolved

    def run_skill_host_mapping_ready(self) -> bool:
        """Return whether the configured Worker/daemon path mapping is usable."""

        sandbox_host = os.getenv("ACT_WEAVE_SANDBOX_HOST", "localhost").strip().lower()
        if sandbox_host == "host.docker.internal" and not os.getenv("ACT_WEAVE_HOST_BASE_DIR"):
            return False
        try:
            _validated_host_path(self.host_run_skill_materialization_root())
        except ValueError:
            return False
        return True

    def run_skill_uses_distinct_host_view(self) -> bool:
        """Return whether the daemon resolves a different absolute root."""

        worker_root = _validated_host_path(
            str(self.run_skill_materialization_root()),
        )
        host_root = _validated_host_path(
            self.host_run_skill_materialization_root(),
        )
        return type(worker_root) is not type(host_root) or worker_root != host_root

    @property
    def user_md_file(self) -> Path:
        """Path to the global user profile file: `{base_dir}/USER.md`."""
        return self.base_dir / "USER.md"

    @property
    def agents_dir(self) -> Path:
        """Legacy root for shared (pre user-isolation) custom agents: `{base_dir}/agents/`.

        New code should use :meth:`user_agents_dir` instead. This property remains
        only as a read-side compatibility fallback.
        """
        return self.base_dir / "agents"

    def agent_dir(self, name: str) -> Path:
        """Legacy per-agent directory (no user isolation): `{base_dir}/agents/{name}/`."""
        return self.agents_dir / name.lower()

    def user_dir(self, user_id: str) -> Path:
        """Directory for a specific user: `{base_dir}/users/{user_id}/`."""
        return self.base_dir / "users" / _validate_user_id(user_id)

    def prepare_user_dir_for_raw_id(self, raw_user_id: str) -> str:
        """Return the safe user ID and migrate this ID's legacy unsafe-id bucket.

        A previous branch revision used SHA-1 for unsafe external user IDs.
        New IDs use SHA-256; the legacy bucket name is recomputed from the same
        raw ID, so only this user's own old bucket can ever be moved — a
        different raw ID sharing the sanitized prefix produces a different
        legacy digest and is never touched.
        """
        safe_user_id = make_safe_user_id(raw_user_id)
        sanitized = _UNSAFE_USER_ID_CHAR_RE.sub("-", raw_user_id)
        if safe_user_id == raw_user_id:
            return safe_user_id

        users_dir = self.base_dir / "users"
        target_dir = users_dir / safe_user_id
        legacy_dir = users_dir / _legacy_safe_user_id(raw_user_id, sanitized)
        try:
            if target_dir.exists() or not legacy_dir.is_dir():
                return safe_user_id
            legacy_dir.rename(target_dir)
            logger.info("Migrated legacy unsafe-id user directory to the current digest format")
        except OSError:
            logger.exception("Failed to migrate legacy unsafe-id user directory")
        return safe_user_id

    def user_agents_dir(self, user_id: str) -> Path:
        """Per-user root for that user's custom agents: `{base_dir}/users/{user_id}/agents/`."""
        return self.user_dir(user_id) / "agents"

    def user_agent_dir(self, user_id: str, agent_name: str) -> Path:
        """Per-user per-agent directory: `{base_dir}/users/{user_id}/agents/{name}/`."""
        return self.user_agents_dir(user_id) / agent_name.lower()

    def user_skills_dir(self, user_id: str) -> Path:
        """Per-user root for that user's custom skills: `{base_dir}/users/{user_id}/skills/`."""
        return self.user_dir(user_id) / "skills"

    def user_custom_skills_dir(self, user_id: str) -> Path:
        """Per-user custom skills directory: `{base_dir}/users/{user_id}/skills/custom/`.

        This is the user-scoped replacement for the global ``{base_dir}/skills/custom/``
        directory. Custom skills are written here; public skills remain under the
        global ``{base_dir}/skills/public/`` (read-only).
        """
        return self.user_skills_dir(user_id) / "custom"

    def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for a thread's data.

        When *user_id* is provided:
            `{base_dir}/users/{user_id}/threads/{thread_id}/`
        Otherwise (legacy layout):
            `{base_dir}/threads/{thread_id}/`

        This directory contains a `user-data/` subdirectory that is mounted
        as `/mnt/user-data/` inside the sandbox.

        Raises:
            ValueError: If `thread_id` or `user_id` contains unsafe characters (path
                        separators or `..`) that could cause directory traversal.
        """
        if user_id is not None:
            return self.user_dir(user_id) / "threads" / _validate_thread_id(thread_id)
        return self.base_dir / "threads" / _validate_thread_id(thread_id)

    def sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for the agent's workspace directory.
        Host: `{base_dir}/threads/{thread_id}/user-data/workspace/`
        Sandbox: `/mnt/user-data/workspace/`
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "workspace"

    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for user-uploaded files.
        Host: `{base_dir}/threads/{thread_id}/user-data/uploads/`
        Sandbox: `/mnt/user-data/uploads/`
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "uploads"

    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for agent-generated artifacts.
        Host: `{base_dir}/threads/{thread_id}/user-data/outputs/`
        Sandbox: `/mnt/user-data/outputs/`
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data" / "outputs"

    def acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for the ACP workspace of a specific thread.
        Host: `{base_dir}/threads/{thread_id}/acp-workspace/`
        Sandbox: `/mnt/acp-workspace/`

        Each thread gets its own isolated ACP workspace so that concurrent
        sessions cannot read each other's ACP agent outputs.
        """
        return self.thread_dir(thread_id, user_id=user_id) / "acp-workspace"

    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path:
        """
        Host path for the user-data root.
        Host: `{base_dir}/threads/{thread_id}/user-data/`
        Sandbox: `/mnt/user-data/`
        """
        return self.thread_dir(thread_id, user_id=user_id) / "user-data"

    def host_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for a thread directory, preserving Windows path syntax."""
        if user_id is not None:
            return _join_host_path(self._host_base_dir_str(), "users", _validate_user_id(user_id), "threads", _validate_thread_id(thread_id))
        return _join_host_path(self._host_base_dir_str(), "threads", _validate_thread_id(thread_id))

    def host_sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for a thread's user-data root."""
        return _join_host_path(self.host_thread_dir(thread_id, user_id=user_id), "user-data")

    def host_sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the workspace mount source."""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "workspace")

    def host_sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the uploads mount source."""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "uploads")

    def host_sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the outputs mount source."""
        return _join_host_path(self.host_sandbox_user_data_dir(thread_id, user_id=user_id), "outputs")

    def host_acp_workspace_dir(self, thread_id: str, *, user_id: str | None = None) -> str:
        """Host path for the ACP workspace mount source."""
        return _join_host_path(self.host_thread_dir(thread_id, user_id=user_id), "acp-workspace")

    def ensure_thread_dirs(self, thread_id: str, *, user_id: str | None = None) -> None:
        """Create all standard sandbox directories for a thread.

        Directories are created with mode 0o777 so that sandbox containers
        (which may run as a different UID than the host backend process) can
        write to the volume-mounted paths without "Permission denied" errors.
        The explicit chmod() call is necessary because Path.mkdir(mode=...) is
        subject to the process umask and may not yield the intended permissions.

        Includes the ACP workspace directory so it can be volume-mounted into
        the sandbox container at ``/mnt/acp-workspace`` even before the first
        ACP agent invocation.
        """
        for d in [
            self.sandbox_work_dir(thread_id, user_id=user_id),
            self.sandbox_uploads_dir(thread_id, user_id=user_id),
            self.sandbox_outputs_dir(thread_id, user_id=user_id),
            self.acp_workspace_dir(thread_id, user_id=user_id),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o777)

    def delete_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> None:
        """Delete all persisted data for a thread.

        The operation is idempotent: missing thread directories are ignored.
        """
        thread_dir = self.thread_dir(thread_id, user_id=user_id)
        if thread_dir.exists():
            shutil.rmtree(thread_dir)

    def resolve_virtual_path(self, thread_id: str, virtual_path: str, *, user_id: str | None = None) -> Path:
        """Resolve a sandbox virtual path to the actual host filesystem path.

        Args:
            thread_id: The thread ID.
            virtual_path: Virtual path as seen inside the sandbox, e.g.
                          ``/mnt/user-data/outputs/report.pdf``.
                          Leading slashes are stripped before matching.
            user_id: Optional user ID for user-scoped path resolution.

        Returns:
            The resolved absolute host filesystem path.

        Raises:
            ValueError: If the path does not start with the expected virtual
                        prefix or a path-traversal attempt is detected.
        """
        stripped = virtual_path.lstrip("/")
        prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

        # Require an exact segment-boundary match to avoid prefix confusion
        # (e.g. reject paths like "mnt/user-dataX/...").
        if stripped != prefix and not stripped.startswith(prefix + "/"):
            raise ValueError(f"Path must start with /{prefix}")

        relative = stripped[len(prefix) :].lstrip("/")
        base = self.sandbox_user_data_dir(thread_id, user_id=user_id).resolve()
        actual = (base / relative).resolve()

        try:
            actual.relative_to(base)
        except ValueError:
            raise ValueError("Access denied: path traversal detected")

        return actual


# ── Singleton ────────────────────────────────────────────────────────────

_paths: Paths | None = None


def get_paths() -> Paths:
    """Return the global Paths singleton (lazy-initialized)."""
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def resolve_path(path: str) -> Path:
    """Resolve *path* to an absolute ``Path``.

    Relative paths are resolved relative to the application base directory.
    Absolute paths are returned as-is (after normalisation).
    """
    p = Path(path)
    if not p.is_absolute():
        p = get_paths().base_dir / path
    return p.resolve()
