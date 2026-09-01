import errno
import hashlib
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from deerflow.error_codes import PublicRunError, PublicRunErrorCode
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.local.local_sandbox import (
    LocalSandbox,
    PathMapping,
    reset_private_projection_root,
)
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import (
    Orphaned,
    PrivateSandboxLease,
    ProviderMountAbsentProof,
    ProviderRunMountLease,
    ProviderRunMountOwnerAbsentProof,
    ProviderRunMountOwnerReconciliation,
    ProviderRunMountOwnerUnknown,
    Released,
    RunMountReleaseOutcome,
    RunReadonlyMountSource,
    RunScopedReadOnlyMount,
    SandboxProvider,
    merge_run_mount_release_outcome,
    private_sandbox_relative_root,
    validate_run_readonly_mount_source,
)

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
# ``acquire(thread_id)`` for that thread simply rebuilds the sandbox without
# its accumulated ``_agent_written_paths`` (read_file falls
# back to no reverse resolution, which is the same behaviour as a fresh run).
DEFAULT_MAX_CACHED_THREAD_SANDBOXES = 256
_RUN_SKILLS_CONTAINER_PATH = "/mnt/skills"


@dataclass(frozen=True, slots=True)
class _LocalRunReadonlyMount:
    lease: ProviderRunMountLease
    private_lease: PrivateSandboxLease
    source: RunReadonlyMountSource
    probe_relative_path: str
    probe_content: str


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

    def run_readonly_mounts_ready(self) -> bool:
        """Native Local execution consumes the validated Worker path directly."""

        try:
            self._require_run_readonly_mount_configuration()
        except Exception:
            return False
        return True

    @staticmethod
    def _require_run_readonly_mount_configuration() -> str:
        """Return the fixed mount root or reject a bypass-capable Local mode."""

        from deerflow.config import get_app_config

        config = get_app_config()
        configured_prefix = config.skills.container_path.rstrip("/") or "/"
        if config.sandbox.allow_host_bash is not False:
            raise PublicRunError(
                PublicRunErrorCode.LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED,
            )
        if configured_prefix != _RUN_SKILLS_CONTAINER_PATH:
            raise ValueError(
                "Run-scoped skills require the fixed /mnt/skills root",
            )
        return configured_prefix

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
        self._run_readonly_mount_owner_ids: set[uuid.UUID] = set()
        self._run_readonly_mounts: dict[str, _LocalRunReadonlyMount] = {}
        self._run_readonly_mount_outcomes: dict[
            str,
            RunMountReleaseOutcome,
        ] = {}
        self._max_cached_threads = max_cached_threads
        self._lock = threading.Lock()

    def _setup_path_mappings(self) -> list[PathMapping]:
        """
        Setup static path mappings shared by every sandbox this provider yields.

        Static mappings cover only operator-configured custom mounts. Skill
        files enter a sandbox through run-scoped read-only mounts.

        Returns:
            List of static path mappings
        """
        mappings: list[PathMapping] = []

        try:
            from deerflow.config import get_app_config

            config = get_app_config()
            container_path = config.skills.container_path

            # Map custom mounts from sandbox config
            _RESERVED_CONTAINER_PREFIXES = [
                container_path,
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
                    # Worker process running this provider. Skipping silently
                    # makes this a difficult-to-debug failure (a sandbox Skill
                    # or tool reads an empty directory instead of the configured
                    # mount), so escalate to ERROR with actionable guidance.
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
                            "sandbox.mounts entry %s -> %s ignored: host_path %s does not exist from the perspective of the Worker process. Use an absolute host path visible to that process.",
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
        """Build per-thread data mappings; run mounts own Skill access."""
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

    @staticmethod
    def _validated_run_readonly_mount_source(
        source: RunReadonlyMountSource,
    ) -> tuple[str, str]:
        from deerflow.config.paths import get_paths

        validated = validate_run_readonly_mount_source(
            source,
            trusted_root=get_paths().run_skill_materialization_root(),
        )
        return validated.probe_relative_path, validated.probe_content

    def prepare_run_readonly_mount(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
        run_id: str,
        source: RunReadonlyMountSource,
    ) -> ProviderRunMountLease:
        probe_relative_path, probe_content = self._validated_run_readonly_mount_source(source)
        with self._lock:
            if source.owner_id in self._run_readonly_mount_owner_ids:
                raise SandboxRuntimeError(
                    "Run read-only mount owner is already registered",
                )
            self._run_readonly_mount_owner_ids.add(source.owner_id)

        try:
            private_lease = self.acquire_private(
                thread_id,
                scope=scope,
                user_id=scope.owner_user_id,
                run_id=run_id,
                mounts=(
                    RunScopedReadOnlyMount(
                        run_id=run_id,
                        container_path=_RUN_SKILLS_CONTAINER_PATH,
                        host_path=str(source.worker_root),
                    ),
                ),
            )
        except BaseException:
            with self._lock:
                self._run_readonly_mount_owner_ids.discard(source.owner_id)
            raise
        lease = ProviderRunMountLease(
            owner_id=source.owner_id,
            provider_kind="local",
            sandbox_id=private_lease.sandbox_id,
            mount_lease_id=uuid.uuid4().hex,
        )
        entry = _LocalRunReadonlyMount(
            lease=lease,
            private_lease=private_lease,
            source=source,
            probe_relative_path=probe_relative_path,
            probe_content=probe_content,
        )
        with self._lock:
            self._run_readonly_mounts[lease.mount_lease_id] = entry
        try:
            return self.readback_run_readonly_mount(lease)
        except BaseException:
            self.release_run_readonly_mount(lease)
            raise

    def readback_run_readonly_mount(
        self,
        lease: ProviderRunMountLease,
    ) -> ProviderRunMountLease:
        if type(lease) is not ProviderRunMountLease:
            raise SandboxRuntimeError("Invalid provider run mount lease")
        with self._lock:
            entry = self._run_readonly_mounts.get(lease.mount_lease_id)
        if entry is None or entry.lease != lease:
            raise SandboxRuntimeError("Run read-only mount lease is not active")
        sandbox = self.get(lease.sandbox_id)
        if sandbox is None:
            raise SandboxRuntimeError("Run read-only mount sandbox is absent")
        virtual_probe = f"{_RUN_SKILLS_CONTAINER_PATH}/{entry.probe_relative_path}"
        try:
            if sandbox.read_file(virtual_probe) != entry.probe_content:
                raise SandboxRuntimeError("Run read-only mount readback changed")
            write_probe = f"{_RUN_SKILLS_CONTAINER_PATH}/.actweave-write-probe-{lease.mount_lease_id}"
            try:
                sandbox.write_file(write_probe, "probe")
            except OSError as exc:
                if exc.errno != errno.EROFS:
                    raise SandboxRuntimeError(
                        "Run read-only mount write probe was inconclusive",
                    ) from exc
            else:
                local_probe = entry.source.worker_root / Path(write_probe).name
                try:
                    local_probe.unlink(missing_ok=True)
                finally:
                    raise SandboxRuntimeError("Run read-only mount is writable")
        except SandboxRuntimeError:
            raise
        except (OSError, UnicodeError) as exc:
            raise SandboxRuntimeError(
                "Run read-only mount readback failed",
            ) from exc
        return lease

    def release_run_readonly_mount(
        self,
        lease: ProviderRunMountLease,
    ) -> RunMountReleaseOutcome:
        if type(lease) is not ProviderRunMountLease:
            raise SandboxRuntimeError("Invalid provider run mount lease")
        with self._lock:
            prior = self._run_readonly_mount_outcomes.get(
                lease.mount_lease_id,
            )
            entry = self._run_readonly_mounts.get(lease.mount_lease_id)
        if prior is not None:
            if type(prior) is Released and prior.matches_lease(lease):
                return prior
            if type(prior) is Orphaned and prior.matches_lease(lease):
                pass
            else:
                raise SandboxRuntimeError("Run read-only mount lease mismatch")
        if entry is None or entry.lease != lease:
            raise SandboxRuntimeError("Run read-only mount lease is not active")

        try:
            self.release_private(entry.private_lease)
        except Exception:
            observed: RunMountReleaseOutcome = Orphaned.from_lease(
                lease,
                reason_code="release_readback_unknown",
                last_lifecycle_state="release_pending",
            )
        else:
            observed = (
                Released(proof=ProviderMountAbsentProof.from_lease(lease))
                if self.get(lease.sandbox_id) is None
                else Orphaned.from_lease(
                    lease,
                    reason_code="release_readback_unknown",
                    last_lifecycle_state="release_pending",
                )
            )

        outcome = observed if prior is None else merge_run_mount_release_outcome(prior, observed)
        with self._lock:
            self._run_readonly_mount_outcomes[lease.mount_lease_id] = outcome
            if type(outcome) is Released:
                self._run_readonly_mounts.pop(lease.mount_lease_id, None)
                self._run_readonly_mount_owner_ids.discard(lease.owner_id)
        return outcome

    def ensure_run_readonly_mount_owner_absent(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerReconciliation:
        """Reconcile the process-local mount map for one durable owner."""

        if type(owner_id) is not uuid.UUID:
            raise SandboxRuntimeError("Invalid run read-only mount owner")
        if persisted_lease is not None and (type(persisted_lease) is not ProviderRunMountLease or persisted_lease.owner_id != owner_id or persisted_lease.provider_kind != "local"):
            return ProviderRunMountOwnerUnknown(
                owner_id=owner_id,
                provider_kind="local",
                reason_code="owner_lease_mismatch",
            )
        with self._lock:
            entries = tuple(entry for entry in self._run_readonly_mounts.values() if entry.lease.owner_id == owner_id)
            owner_registered = owner_id in self._run_readonly_mount_owner_ids
        if len(entries) > 1:
            return ProviderRunMountOwnerUnknown(
                owner_id=owner_id,
                provider_kind="local",
                reason_code="owner_mount_ambiguous",
            )
        if entries:
            entry = entries[0]
            if persisted_lease is not None and entry.lease != persisted_lease:
                return ProviderRunMountOwnerUnknown(
                    owner_id=owner_id,
                    provider_kind="local",
                    reason_code="owner_lease_mismatch",
                )
            try:
                outcome = self.release_run_readonly_mount(entry.lease)
            except Exception:
                return ProviderRunMountOwnerUnknown(
                    owner_id=owner_id,
                    provider_kind="local",
                    reason_code="owner_release_unknown",
                )
            if type(outcome) is not Released:
                return ProviderRunMountOwnerUnknown(
                    owner_id=owner_id,
                    provider_kind="local",
                    reason_code="owner_release_unknown",
                )
        elif owner_registered:
            return ProviderRunMountOwnerUnknown(
                owner_id=owner_id,
                provider_kind="local",
                reason_code="owner_acquire_in_progress",
            )
        return ProviderRunMountOwnerAbsentProof(
            owner_id=owner_id,
            provider_kind="local",
        )

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
        configured_prefix = self._require_run_readonly_mount_configuration()
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
            self._run_readonly_mount_owner_ids.clear()
            self._run_readonly_mounts.clear()
            self._run_readonly_mount_outcomes.clear()
            _singleton = None
        for sandbox in run_sandboxes:
            sandbox.close_private_file_authority()

    def shutdown(self) -> None:
        # LocalSandboxProvider has no extra resources beyond the cached
        # ``LocalSandbox`` instances, so shutdown uses the same cleanup path
        # as ``reset``.
        self.reset()
