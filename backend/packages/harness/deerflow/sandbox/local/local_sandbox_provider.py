import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.local.local_sandbox import (
    LocalSandbox,
    PathMapping,
    reset_private_projection_root,
)
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import (
    PrivateSandboxLease,
    RunScopedReadOnlyMount,
    SandboxProvider,
    private_sandbox_relative_root,
)
from deerflow.skills.storage import user_should_see_legacy_skills

logger = logging.getLogger(__name__)

# Module-level alias kept for backward compatibility with older callers/tests
# that reach into ``local_sandbox_provider._singleton`` directly. New code reads
# the provider instance attributes (``_generic_sandbox`` / ``_thread_sandboxes``)
# instead.
_singleton: LocalSandbox | None = None

# Virtual prefixes that must be reserved by the per-thread mappings created in
# ``acquire`` — custom mounts from ``config.yaml`` may not overlap with these.
_USER_DATA_VIRTUAL_PREFIX = "/mnt/user-data"
_ACP_WORKSPACE_VIRTUAL_PREFIX = "/mnt/acp-workspace"

# Default upper bound on per-thread LocalSandbox instances retained in memory.
# Each cached instance is cheap (a small Python object with a list of
# PathMapping and a set of agent-written paths used for reverse resolve), but
# in a long-running gateway the number of distinct thread_ids is unbounded.
# When the cap is exceeded the least-recently-used entry is dropped; the next
# ``acquire(thread_id)`` for that thread simply rebuilds the sandbox at the
# cost of losing its accumulated ``_agent_written_paths`` (read_file falls
# back to no reverse resolution, which is the same behaviour as a fresh run).
DEFAULT_MAX_CACHED_THREAD_SANDBOXES = 256


class LocalSandboxProvider(SandboxProvider):
    """Local-filesystem sandbox provider with per-thread path scoping.

    Earlier revisions of this provider returned a single process-wide
    ``LocalSandbox`` keyed by the literal id ``"local"``. That singleton could
    not honour the documented ``/mnt/user-data/...`` contract at the public
    ``Sandbox`` API boundary because the corresponding host directory is
    per-thread (``{base_dir}/users/{user_id}/threads/{thread_id}/user-data/``).

    The provider now produces a fresh ``LocalSandbox`` per ``thread_id`` whose
    ``path_mappings`` include thread-scoped entries for
    ``/mnt/user-data/{workspace,uploads,outputs}`` and ``/mnt/acp-workspace``,
    mirroring how :class:`AioSandboxProvider` bind-mounts those paths into its
    docker container. The legacy ``acquire()`` / ``acquire(None)`` call still
    returns a generic singleton with id ``"local"`` for callers (and tests)
    that do not have a thread context.

    Thread-safety: ``acquire``, ``get`` and ``reset`` may be invoked from
    multiple threads (Gateway tool dispatch, subagent worker pools, the
    background memory updater, …) so all cache state changes are serialised
    through a provider-wide :class:`threading.Lock`. This matches the pattern
    used by :class:`AioSandboxProvider`.

    Memory bound: ``_thread_sandboxes`` is an LRU cache capped at
    ``max_cached_threads`` (default :data:`DEFAULT_MAX_CACHED_THREAD_SANDBOXES`).
    When the cap is exceeded the least-recently-used entry is evicted on the
    next ``acquire``; the evicted thread's next ``acquire`` rebuilds a fresh
    sandbox (losing only its ``_agent_written_paths`` reverse-resolve hint,
    which gracefully degrades read_file output).
    """

    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = False

    def __init__(self, max_cached_threads: int = DEFAULT_MAX_CACHED_THREAD_SANDBOXES):
        """Initialize the local sandbox provider with static path mappings.

        Args:
            max_cached_threads: Upper bound on per-thread sandboxes retained in
                the LRU cache. When exceeded, the least-recently-used entry is
                evicted on the next ``acquire``.
        """
        self._path_mappings = self._setup_path_mappings()
        self._generic_sandbox: LocalSandbox | None = None
        self._thread_sandboxes: OrderedDict[tuple[str, str], LocalSandbox] = OrderedDict()
        self._run_sandboxes: OrderedDict[tuple[str, str, str], LocalSandbox] = OrderedDict()
        self._run_sandbox_ids: dict[str, tuple[str, str, str]] = {}
        self._active_private_runs: dict[tuple[str, str], str] = {}
        self._max_cached_threads = max_cached_threads
        self._lock = threading.Lock()

    def _setup_path_mappings(self) -> list[PathMapping]:
        """
        Setup static path mappings shared by every sandbox this provider yields.

        Static mappings cover the **public** skills directory and any custom
        mounts from ``config.yaml`` — both are process-wide and identical for
        every thread.  Per-thread ``/mnt/user-data/...``, ``/mnt/acp-workspace``
        and ``/mnt/skills/custom`` mappings are appended inside
        :meth:`_build_thread_path_mappings` because they depend on
        ``thread_id`` and the effective ``user_id``.

        Returns:
            List of static path mappings
        """
        mappings: list[PathMapping] = []

        # Map skills: split mount for public + legacy + custom
        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            # Public skills: global, read-only — static, shared by all threads
            public_skills_path = skills_path / "public"
            if public_skills_path.exists():
                mappings.append(
                    PathMapping(
                        container_path=f"{container_path}/public",
                        local_path=str(public_skills_path),
                        read_only=True,
                    )
                )

            # NOTE: Legacy skills mount is NOT included here because it must
            # only be exposed to users who have no per-user custom skills yet
            # (mirroring ``UserScopedSkillStorage._iter_skill_files`` which only
            # surfaces SkillCategory.LEGACY to such users). Including it for
            # every user would let users with per-user custom skills still
            # ``read_file("/mnt/skills/legacy/<name>/SKILL.md")`` and read
            # content the listing layer told them doesn't exist. See review
            # feedback on PR #3889 — the legacy mount is now built in
            # ``_build_thread_path_mappings`` after we know the user_id.

            # NOTE: Custom skills mount is NOT included here because it is
            # per-user and must be built dynamically per-thread inside
            # ``_build_thread_path_mappings``.  The static mount that previously
            # bound ``get_effective_user_id()`` at init time was incorrect:
            # every subsequent user's sandbox would resolve
            # ``/mnt/skills/custom`` to the init-time user's directory.

            # Map custom mounts from sandbox config
            _RESERVED_CONTAINER_PREFIXES = [
                f"{container_path}/public",
                f"{container_path}/custom",
                f"{container_path}/legacy",
                _ACP_WORKSPACE_VIRTUAL_PREFIX,
                _USER_DATA_VIRTUAL_PREFIX,
            ]
            sandbox_config = config.sandbox
            if sandbox_config and sandbox_config.mounts:
                for mount in sandbox_config.mounts:
                    host_path = Path(mount.host_path)
                    container_path = mount.container_path.rstrip("/") or "/"

                    if not host_path.is_absolute():
                        logger.warning(
                            "Mount host_path must be absolute, skipping: %s -> %s",
                            mount.host_path,
                            mount.container_path,
                        )
                        continue

                    if not container_path.startswith("/"):
                        logger.warning(
                            "Mount container_path must be absolute, skipping: %s -> %s",
                            mount.host_path,
                            mount.container_path,
                        )
                        continue

                    # Reject mounts that conflict with reserved container paths
                    if any(container_path == p or container_path.startswith(p + "/") for p in _RESERVED_CONTAINER_PREFIXES):
                        logger.warning(
                            "Mount container_path conflicts with reserved prefix, skipping: %s",
                            mount.container_path,
                        )
                        continue
                    # Ensure the host path exists before adding mapping.
                    #
                    # ``host_path`` is resolved against the filesystem of the
                    # process running this provider — for ``make dev`` that is
                    # the host machine, but for ``make up`` it is the
                    # ``deer-flow-gateway`` container, so any host path that
                    # isn't bind-mounted into the gateway image will be missing
                    # here. Skipping silently makes this a high-cost-to-debug
                    # silent failure (sandbox skill / tool reads an empty dir
                    # instead of the configured mount), so escalate to ERROR
                    # and include actionable guidance. See #3244.
                    if host_path.exists():
                        mappings.append(
                            PathMapping(
                                container_path=container_path,
                                local_path=str(host_path.resolve()),
                                read_only=mount.read_only,
                            )
                        )
                    else:
                        logger.error(
                            "sandbox.mounts entry %s -> %s ignored: host_path %s does not exist from the "
                            "perspective of the gateway process. In Docker deployments (make up / docker-compose), "
                            "this path must also be bind-mounted into the gateway container — add a matching "
                            "volume entry under services.gateway.volumes in docker/docker-compose.yaml (and use "
                            "the in-container path here), or run in local mode (make dev) where the gateway sees "
                            "the host filesystem directly.",
                            mount.host_path,
                            mount.container_path,
                            mount.host_path,
                        )
        except Exception as e:
            # Log but don't fail if config loading fails
            logger.warning("Could not setup path mappings: %s", e, exc_info=True)

        return mappings

    @staticmethod
    def _effective_acquire_user_id(user_id: str | None) -> str:
        from deerflow.runtime.user_context import get_effective_user_id

        return user_id or get_effective_user_id()

    @staticmethod
    def _thread_key(thread_id: str, user_id: str) -> tuple[str, str]:
        return (user_id, thread_id)

    @staticmethod
    def _sandbox_id_for_thread(thread_id: str, user_id: str) -> str:
        return f"local:{user_id}:{thread_id}"

    @staticmethod
    def _key_from_sandbox_id(sandbox_id: str) -> tuple[str, str] | None:
        if not sandbox_id.startswith("local:"):
            return None
        value = sandbox_id[len("local:") :]
        user_id, separator, thread_id = value.partition(":")
        if not separator or not user_id or not thread_id:
            return None
        return (user_id, thread_id)

    @staticmethod
    def _build_thread_path_mappings(thread_id: str, *, user_id: str | None = None) -> list[PathMapping]:
        """Build per-thread path mappings for /mnt/user-data, /mnt/acp-workspace,
        and /mnt/skills/custom.

        Uses the explicitly resolved user id when provided, falling back to
        :func:`get_effective_user_id` for legacy callers.  Custom skills are
        mounted per-user (read-only) because agent writes custom skills via
        ``skill_manage_tool`` on the host filesystem, not inside the sandbox.
        """
        from deerflow.config import get_app_config
        from deerflow.config.paths import get_paths

        paths = get_paths()
        effective_user_id = LocalSandboxProvider._effective_acquire_user_id(user_id)
        paths.ensure_thread_dirs(thread_id, user_id=effective_user_id)

        mappings = [
            # Aggregate parent mapping so ``ls /mnt/user-data`` and other
            # parent-level operations behave the same as inside AIO (where the
            # parent directory is real and contains the three subdirs). Longer
            # subpath mappings below still win for ``/mnt/user-data/workspace/...``
            # because ``_find_path_mapping`` sorts by container_path length.
            PathMapping(
                container_path=_USER_DATA_VIRTUAL_PREFIX,
                local_path=str(paths.sandbox_user_data_dir(thread_id, user_id=effective_user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/workspace",
                local_path=str(paths.sandbox_work_dir(thread_id, user_id=effective_user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/uploads",
                local_path=str(paths.sandbox_uploads_dir(thread_id, user_id=effective_user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/outputs",
                local_path=str(paths.sandbox_outputs_dir(thread_id, user_id=effective_user_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=_ACP_WORKSPACE_VIRTUAL_PREFIX,
                local_path=str(paths.acp_workspace_dir(thread_id, user_id=effective_user_id)),
                read_only=False,
            ),
        ]

        # Per-user custom skills mount (read-only).  This must be per-thread
        # because ``/mnt/skills/custom`` resolves to different host directories
        # for different users.
        try:
            config = get_app_config()
            skills_container_path = config.skills.container_path
            user_custom_path = paths.user_custom_skills_dir(effective_user_id)
            user_custom_path.mkdir(parents=True, exist_ok=True)

            mappings.append(
                PathMapping(
                    container_path=f"{skills_container_path}/custom",
                    local_path=str(user_custom_path),
                    read_only=True,
                )
            )
        except Exception as exc:
            logger.warning("Could not setup per-thread custom skills mount: %s", exc, exc_info=True)

        # Legacy (pre-migration global-custom) skills: only mount for users
        # who have no per-user custom skills yet, mirroring the
        # ``UserScopedSkillStorage._iter_skill_files`` visibility rule. Users
        # with their own per-user custom skills cannot see LEGACY in the
        # listing/prompt and must not be able to read it via the sandbox
        # either — otherwise the listing layer and the sandbox layer disagree
        # about visibility, and the sandbox layer is the more permissive one.
        try:
            config = get_app_config()
            skills_container_path = config.skills.container_path
            user_custom_path = paths.user_custom_skills_dir(effective_user_id)
            legacy_skills_path = config.skills.get_skills_path() / "custom"
            if user_should_see_legacy_skills(effective_user_id, host_path=str(config.skills.get_skills_path())) and legacy_skills_path.exists():
                mappings.append(
                    PathMapping(
                        container_path=f"{skills_container_path}/legacy",
                        local_path=str(legacy_skills_path),
                        read_only=True,
                    )
                )
        except Exception as exc:
            logger.warning("Could not setup per-thread legacy skills mount: %s", exc, exc_info=True)

        return mappings

    @staticmethod
    def _build_private_path_mappings(
        thread_id: str,
        *,
        scope: PrivateResourceScope,
    ) -> list[PathMapping]:
        from deerflow.config import get_app_config
        from deerflow.config.paths import get_paths

        paths = get_paths()
        relative_root = private_sandbox_relative_root(scope, thread_id)
        thread_root = paths.base_dir / relative_root
        user_data = thread_root / "user-data"
        mappings = [
            PathMapping(_USER_DATA_VIRTUAL_PREFIX, str(user_data), False),
            PathMapping(f"{_USER_DATA_VIRTUAL_PREFIX}/workspace", str(user_data / "workspace"), False),
            PathMapping(f"{_USER_DATA_VIRTUAL_PREFIX}/uploads", str(user_data / "uploads"), False),
            PathMapping(f"{_USER_DATA_VIRTUAL_PREFIX}/outputs", str(user_data / "outputs"), False),
            PathMapping(_ACP_WORKSPACE_VIRTUAL_PREFIX, str(thread_root / "acp-workspace"), False),
        ]
        try:
            config = get_app_config()
            custom = paths.user_custom_skills_dir(scope.owner_user_id)
            custom.mkdir(parents=True, exist_ok=True)
            mappings.append(PathMapping(f"{config.skills.container_path}/custom", str(custom), True))
        except Exception as exc:
            logger.warning("Could not setup private custom skills mount: %s", exc)
        return mappings

    def acquire_private(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        user_id: str,
        run_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...] = (),
    ) -> PrivateSandboxLease:
        if user_id != scope.owner_user_id or not run_id:
            raise SandboxRuntimeError("Invalid private sandbox authority")
        relative_root = private_sandbox_relative_root(scope, thread_id)
        scope_key = hashlib.sha256(relative_root.encode()).hexdigest()[:24]
        key = (scope_key, thread_id, run_id)
        active_key = (scope_key, thread_id)
        with self._lock:
            cached = self._run_sandboxes.get(key)
            if cached is not None:
                self._run_sandboxes.move_to_end(key)
                return PrivateSandboxLease(cached.id, run_id, relative_root)
            active_run_id = self._active_private_runs.get(active_key)
            if active_run_id is not None:
                raise SandboxRuntimeError("Private sandbox already has an active run for this thread")
            self._active_private_runs[active_key] = run_id

        try:
            from deerflow.config.paths import get_paths

            reset_private_projection_root(get_paths().base_dir, relative_root)
            mappings = list(self._path_mappings) + self._build_private_path_mappings(
                thread_id,
                scope=scope,
            )
            if mounts:
                self.validate_run_scoped_mounts(
                    thread_id,
                    user_id=user_id,
                    mounts=mounts,
                )
                mount = mounts[0]
                skills_prefix = mount.container_path.rstrip("/")
                mappings = [mapping for mapping in mappings if not (mapping.container_path == skills_prefix or mapping.container_path.startswith(f"{skills_prefix}/"))]
                mappings.append(
                    PathMapping(
                        skills_prefix,
                        mount.host_path,
                        True,
                    )
                )
            sandbox_id = f"local-run:{scope_key}:{thread_id}:{run_id}"
            candidate = LocalSandbox(sandbox_id, path_mappings=mappings)
            candidate.anchor_private_mappings()
            with self._lock:
                self._run_sandboxes[key] = candidate
                self._run_sandbox_ids[candidate.id] = key
            return PrivateSandboxLease(candidate.id, run_id, relative_root)
        except BaseException:
            with self._lock:
                if self._active_private_runs.get(active_key) == run_id:
                    self._active_private_runs.pop(active_key, None)
            raise

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        """Return a sandbox id scoped to *thread_id* (or the generic singleton).

        - ``thread_id=None`` keeps the legacy singleton with id ``"local"`` for
          callers that have no thread context (e.g. legacy tests, scripts).
        - ``thread_id="abc"`` yields a per-thread ``LocalSandbox`` with id
          ``"local:abc"`` whose ``path_mappings`` resolve ``/mnt/user-data/...``
          to that thread's host directories.

        Thread-safe under concurrent invocation: the cache check + insert is
        guarded by ``self._lock`` so two callers racing on the same
        ``thread_id`` always observe the same LocalSandbox instance.
        """
        global _singleton

        if thread_id is None:
            with self._lock:
                if self._generic_sandbox is None:
                    self._generic_sandbox = LocalSandbox("local", path_mappings=list(self._path_mappings))
                    _singleton = self._generic_sandbox
                return self._generic_sandbox.id

        effective_user_id = self._effective_acquire_user_id(user_id)
        key = self._thread_key(thread_id, effective_user_id)

        # Fast path under lock.
        with self._lock:
            cached = self._thread_sandboxes.get(key)
            if cached is not None:
                # Mark as most-recently used so frequently-touched threads
                # survive eviction.
                self._thread_sandboxes.move_to_end(key)
                return cached.id

        # ``_build_thread_path_mappings`` touches the filesystem
        # (``ensure_thread_dirs``); release the lock during I/O.
        new_mappings = list(self._path_mappings) + self._build_thread_path_mappings(thread_id, user_id=effective_user_id)

        with self._lock:
            # Re-check after the lock-free I/O: another caller may have
            # populated the cache while we were computing mappings.
            cached = self._thread_sandboxes.get(key)
            if cached is None:
                cached = LocalSandbox(self._sandbox_id_for_thread(thread_id, effective_user_id), path_mappings=new_mappings)
                self._thread_sandboxes[key] = cached
                self._evict_until_within_cap_locked()
            else:
                self._thread_sandboxes.move_to_end(key)
            return cached.id

    def acquire_with_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> str:
        """Acquire a run-specific sandbox whose skills view is exact-only."""

        self.validate_run_scoped_mounts(
            thread_id,
            user_id=user_id,
            mounts=mounts,
        )
        mount = mounts[0]
        skills_prefix = mount.container_path.rstrip("/")
        host_root = Path(mount.host_path).resolve()
        effective_user_id = self._effective_acquire_user_id(user_id)
        key = (effective_user_id, thread_id, mount.run_id)
        with self._lock:
            cached = self._run_sandboxes.get(key)
            if cached is not None:
                self._run_sandboxes.move_to_end(key)
                return cached.id

        base_mappings = list(self._path_mappings) + self._build_thread_path_mappings(
            thread_id,
            user_id=effective_user_id,
        )
        exact_mappings = [mapping for mapping in base_mappings if not (mapping.container_path == skills_prefix or mapping.container_path.startswith(f"{skills_prefix}/"))]
        exact_mappings.append(
            PathMapping(
                container_path=skills_prefix,
                local_path=str(host_root),
                read_only=True,
            )
        )
        sandbox_id = f"local-run:{effective_user_id}:{thread_id}:{mount.run_id}"
        candidate = LocalSandbox(sandbox_id, path_mappings=exact_mappings)
        with self._lock:
            cached = self._run_sandboxes.get(key)
            if cached is None:
                cached = candidate
                self._run_sandboxes[key] = cached
                self._run_sandbox_ids[cached.id] = key
            else:
                self._run_sandboxes.move_to_end(key)
            return cached.id

    def validate_run_scoped_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> None:
        del thread_id, user_id
        if len(mounts) != 1:
            raise ValueError("Local private runtime requires exactly one skills mount")
        mount = mounts[0]
        skills_prefix = mount.container_path.rstrip("/")
        try:
            from deerflow.config import get_app_config

            app_config = get_app_config()
            configured_prefix = app_config.skills.container_path.rstrip("/")
            allow_host_bash = bool(getattr(getattr(app_config, "sandbox", None), "allow_host_bash", False))
        except Exception:
            configured_prefix = skills_prefix
            allow_host_bash = False
        if allow_host_bash:
            raise ValueError("Local private runtime cannot enforce read-only mounts when host bash is enabled")
        if skills_prefix != configured_prefix:
            raise ValueError("Run-scoped skills must replace the configured skills root")
        host_root = Path(mount.host_path).resolve()
        if not host_root.is_dir():
            raise ValueError("Run-scoped skills host tree is unavailable")

    def release_run_scoped_mounts(
        self,
        thread_id: str,
        *,
        user_id: str,
        mounts: tuple[RunScopedReadOnlyMount, ...],
    ) -> None:
        effective_user_id = self._effective_acquire_user_id(user_id)
        run_ids = {mount.run_id for mount in mounts}
        released: list[LocalSandbox] = []
        with self._lock:
            for run_id in run_ids:
                key = (effective_user_id, thread_id, run_id)
                sandbox = self._run_sandboxes.pop(key, None)
                if sandbox is not None:
                    self._run_sandbox_ids.pop(sandbox.id, None)
                    released.append(sandbox)
        for sandbox in released:
            sandbox.close_private_file_authority()

    def _evict_until_within_cap_locked(self) -> None:
        """LRU-evict cached thread sandboxes once the cap is exceeded.

        Caller MUST hold ``self._lock``.
        """
        while len(self._thread_sandboxes) > self._max_cached_threads:
            evicted_key, _ = self._thread_sandboxes.popitem(last=False)
            logger.info(
                "Evicting LocalSandbox cache entry for user/thread %s/%s (cap=%d)",
                evicted_key[0],
                evicted_key[1],
                self._max_cached_threads,
            )

    def get(self, sandbox_id: str) -> Sandbox | None:
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local-run:"):
            with self._lock:
                key = self._run_sandbox_ids.get(sandbox_id)
                if key is None:
                    return None
                cached = self._run_sandboxes.get(key)
                if cached is not None:
                    self._run_sandboxes.move_to_end(key)
                return cached
        if sandbox_id == "local":
            with self._lock:
                generic = self._generic_sandbox
            if generic is None:
                self.acquire()
                with self._lock:
                    return self._generic_sandbox
            return generic
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local:"):
            key = self._key_from_sandbox_id(sandbox_id)
            if key is None:
                return None
            with self._lock:
                cached = self._thread_sandboxes.get(key)
                if cached is not None:
                    # Touching a thread via ``get`` (used by tools.py to look
                    # up the sandbox once per tool call) promotes it in LRU
                    # order so an active thread isn't evicted under load.
                    self._thread_sandboxes.move_to_end(key)
                return cached
        return None

    def release(self, sandbox_id: str) -> None:
        # LocalSandbox has no resources to release; keep the cached instance so
        # that ``_agent_written_paths`` (used to reverse-resolve agent-authored
        # file contents on read) survives between turns. LRU eviction in
        # ``acquire`` and explicit ``reset()`` / ``shutdown()`` are the only
        # paths that drop cached entries.
        #
        # Note: This method is intentionally not called by SandboxMiddleware
        # to allow sandbox reuse across multiple turns in a thread.
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local-run:"):
            sandbox = None
            with self._lock:
                key = self._run_sandbox_ids.pop(sandbox_id, None)
                if key is not None:
                    sandbox = self._run_sandboxes.pop(key, None)
                    active_key = (key[0], key[1])
                    if self._active_private_runs.get(active_key) == key[2]:
                        self._active_private_runs.pop(active_key, None)
            if sandbox is not None:
                sandbox.close_private_file_authority()

    def reset(self) -> None:
        """Drop all cached LocalSandbox instances.

        ``reset_sandbox_provider()`` calls this to ensure config / mount
        changes take effect on the next ``acquire()``. We also reset the
        module-level ``_singleton`` alias so older callers/tests that reach
        # into it see a fresh state.
        """
        global _singleton
        run_sandboxes: tuple[LocalSandbox, ...]
        with self._lock:
            self._generic_sandbox = None
            self._thread_sandboxes.clear()
            run_sandboxes = tuple(dict.fromkeys(self._run_sandboxes.values()))
            self._run_sandboxes.clear()
            self._run_sandbox_ids.clear()
            self._active_private_runs.clear()
            _singleton = None
        for sandbox in run_sandboxes:
            sandbox.close_private_file_authority()

    def shutdown(self) -> None:
        # LocalSandboxProvider has no extra resources beyond the cached
        # ``LocalSandbox`` instances, so shutdown uses the same cleanup path
        # as ``reset``.
        self.reset()
