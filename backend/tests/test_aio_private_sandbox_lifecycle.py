from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.community.aio_sandbox.aio_sandbox_provider import (
    DEFAULT_IMAGE,
    AioSandboxProvider,
)
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.config.paths import Paths
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox import Orphaned, Released, RunReadonlyMountSource
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount

_PRIVATE_ROOTS = (
    "/mnt/user-data/workspace",
    "/mnt/user-data/uploads",
    "/mnt/user-data/outputs",
)


@dataclass
class _FakePrivateSandbox:
    initialize_error: Exception | None = None
    scan_error_root: str | None = None
    initialize_calls: int = 0
    scan_calls: list[tuple[str, int]] = field(default_factory=list)
    close_calls: int = 0
    mount_probe_content: str | None = None
    mount_probe_calls: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    def initialize_private_roots(self) -> None:
        self.initialize_calls += 1
        if self.initialize_error is not None:
            raise self.initialize_error

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
    ) -> Iterator[object]:
        self.scan_calls.append((root, max_entries))
        if root == self.scan_error_root:
            raise FileNotFoundError(root)
        return iter(())

    def close(self) -> None:
        self.close_calls += 1

    def probe_run_readonly_mount(
        self,
        *,
        owner_id: uuid.UUID,
        expected_manifest: str,
    ) -> None:
        self.mount_probe_calls.append((owner_id, expected_manifest))
        if self.mount_probe_content != expected_manifest:
            raise OSError("guest mount probe mismatch")


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    sandbox: _FakePrivateSandbox,
    *,
    bootstrap_error: Exception | None = None,
    destroy_errors: list[Exception] | None = None,
    mount_requests: list[object] | None = None,
    mount_readbacks: list[object] | None = None,
    owner_requests: list[object] | None = None,
    create_entered: threading.Event | None = None,
    allow_create: threading.Event | None = None,
) -> tuple[
    AioSandboxProvider,
    list[SandboxInfo],
    list[SandboxInfo],
    list[SandboxInfo],
]:
    created: list[SandboxInfo] = []
    bootstrapped: list[SandboxInfo] = []
    destroyed: list[SandboxInfo] = []
    recorded_mounts = mount_requests if mount_requests is not None else []
    recorded_readbacks = mount_readbacks if mount_readbacks is not None else []
    recorded_owners = owner_requests if owner_requests is not None else []
    active_owners: dict[str, str] = {}

    def create(
        _thread_id: str,
        sandbox_id: str,
        *,
        extra_mounts: object,
        user_id: str,
        private_owner_id: str | None = None,
    ) -> SandboxInfo:
        del user_id
        if create_entered is not None:
            create_entered.set()
        if allow_create is not None:
            assert allow_create.wait(timeout=5)
        recorded_mounts.append(extra_mounts)
        recorded_owners.append(private_owner_id)
        info = SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url="http://192.168.64.5:8080",
            container_name=f"deer-flow-sandbox-{sandbox_id}",
            container_id=f"deer-flow-sandbox-{sandbox_id}",
        )
        created.append(info)
        if private_owner_id is not None:
            active_owners[info.sandbox_id] = private_owner_id
        return info

    backend = object.__new__(LocalContainerBackend)
    backend._config_mounts = []
    backend.create_private = create
    backend.create = lambda *_args, **_kwargs: pytest.fail(
        "private AIO acquisition must use dedicated private create",
    )

    def initialize_private_roots(info: SandboxInfo) -> None:
        bootstrapped.append(info)
        if bootstrap_error is not None:
            raise bootstrap_error

    backend.initialize_private_roots = initialize_private_roots
    pending_destroy_errors = list(destroy_errors or [])

    def destroy_private(info: SandboxInfo) -> None:
        destroyed.append(info)
        if pending_destroy_errors:
            raise pending_destroy_errors.pop(0)
        active_owners.pop(info.sandbox_id, None)

    backend.destroy_private = destroy_private
    backend.readback_private_owner_state = lambda info, owner_id: (
        "active" if active_owners.get(info.sandbox_id) == owner_id else "absent" if info.sandbox_id not in active_owners else (_ for _ in ()).throw(RuntimeError("private owner mismatch"))
    )

    def readback_private_run_mount_state(
        info: SandboxInfo,
        owner_id: str,
        *,
        daemon_source: str,
        container_path: str,
    ) -> str:
        recorded_readbacks.append(
            (daemon_source, container_path),
        )
        return backend.readback_private_owner_state(info, owner_id)

    backend.readback_private_run_mount_state = readback_private_run_mount_state
    backend.run_readonly_mounts_ready = lambda: True
    backend.destroy = lambda _info: pytest.fail(
        "private AIO cleanup must use strict private destroy",
    )

    provider = object.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    provider._backend = backend
    provider._sandboxes = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._private_runtime_ids = set()
    provider._warm_pool = {}
    provider._shutdown_called = False
    provider._idle_checker_stop = threading.Event()
    provider._idle_checker_thread = None

    def ready(url: str, timeout: int) -> bool:
        assert bootstrapped == created
        return url == "http://192.168.64.5:8080" and timeout == 60

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.wait_for_sandbox_ready",
        ready,
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.AioSandbox",
        lambda **_kwargs: sandbox,
    )
    return provider, created, bootstrapped, destroyed


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )


def _readonly_mount_source(
    tmp_path: Path,
    owner_id: uuid.UUID,
) -> tuple[Paths, RunReadonlyMountSource]:
    paths = Paths(tmp_path / "state")
    owner_root = paths.run_skill_materialization_root() / owner_id.hex
    tree = owner_root / "tree"
    skill = tree / "custom" / "skill-one" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: skill-one\n---\n", encoding="utf-8")
    manifest = tree / ".actweave-run-mount.json"
    manifest.write_text(
        f'{{"owner_id":"{owner_id}","schema_version":1}}\n',
        encoding="utf-8",
    )
    for path in tree.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    tree.chmod(0o555)
    owner_root.chmod(0o700)
    return paths, RunReadonlyMountSource(owner_id=owner_id, worker_root=tree)


def _restore_mount_source(source: RunReadonlyMountSource) -> None:
    for path in source.worker_root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    source.worker_root.chmod(0o700)


def _assert_no_private_state(provider: AioSandboxProvider) -> None:
    assert provider._sandboxes == {}
    assert provider._sandbox_infos == {}
    assert provider._thread_sandboxes == {}
    assert provider._private_runtime_ids == set()
    assert getattr(provider, "_private_leases", {}) == {}
    assert getattr(provider, "_private_releasing", set()) == set()


def test_aio_sandbox_delegates_private_root_initialization() -> None:
    calls: list[str] = []
    sandbox = object.__new__(AioSandbox)
    sandbox._private_files = type(
        "Authority",
        (),
        {"initialize_private_roots": lambda _self: calls.append("initialize")},
    )()

    sandbox.initialize_private_roots()

    assert calls == ["initialize"]


def test_aio_private_acquire_initializes_then_preflights_all_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, bootstrapped, destroyed = _provider(monkeypatch, sandbox)

    lease = provider.acquire_private(
        "thread-1",
        scope=_scope(),
        user_id="owner-1",
        run_id="run-1",
    )

    assert len(created) == 1
    info = created[0]
    assert lease.sandbox_id == info.sandbox_id
    assert bootstrapped == [info]
    assert sandbox.initialize_calls == 1
    assert sandbox.scan_calls == [(root, 1) for root in _PRIVATE_ROOTS]
    assert getattr(provider, "_private_leases") == {info.sandbox_id: lease}
    assert destroyed == []

    provider.release_private(lease)

    assert destroyed == [info]
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_exact_skill_mount_is_read_only_and_strictly_destroyed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox = _FakePrivateSandbox()
    mount_requests: list[object] = []
    provider, created, _bootstrapped, destroyed = _provider(
        monkeypatch,
        sandbox,
        mount_requests=mount_requests,
    )
    skill_root = tmp_path / "exact-skills"
    skill_root.mkdir()

    lease = provider.acquire_private(
        "thread-1",
        scope=_scope(),
        user_id="owner-1",
        run_id="run-1",
        mounts=(
            RunScopedReadOnlyMount(
                run_id="run-1",
                container_path="/mnt/skills",
                host_path=str(skill_root),
            ),
        ),
    )

    assert mount_requests == [[(str(skill_root.resolve()), "/mnt/skills", True)]]
    assert provider._warm_pool == {}

    provider.release_private(lease)

    assert destroyed == created
    assert provider._warm_pool == {}
    _assert_no_private_state(provider)


def test_aio_typed_run_mount_derives_owner_label_and_returns_absent_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_id = uuid.UUID("50000000-0000-0000-0000-000000000001")
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    expected_manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    sandbox = _FakePrivateSandbox(mount_probe_content=expected_manifest)
    mount_requests: list[object] = []
    owner_requests: list[object] = []
    provider, _created, _bootstrapped, _destroyed = _provider(
        monkeypatch,
        sandbox,
        mount_requests=mount_requests,
        owner_requests=owner_requests,
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-1",
            scope=_scope(),
            run_id="run-typed-1",
            source=source,
        )

        assert lease.owner_id == owner_id
        assert lease.provider_kind == "aio-local-container"
        assert owner_requests == [owner_id.hex]
        assert mount_requests == [
            [(str(source.worker_root.resolve()), "/mnt/skills", True)],
        ]
        assert sandbox.mount_probe_calls == [(owner_id, expected_manifest)]
        assert provider.readback_run_readonly_mount(lease) == lease

        released = provider.release_run_readonly_mount(lease)

        assert type(released) is Released
        assert released.matches_lease(lease)
        assert provider.release_run_readonly_mount(lease) is released
    finally:
        _restore_mount_source(source)


def test_aio_mount_uses_host_view_but_validates_worker_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_id = uuid.UUID("50000000-0000-0000-0000-000000000011")
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    host_base = tmp_path / "host-state"
    expected_host_tree = str(
        host_base / "run-skill-materializations" / owner_id.hex / "tree",
    )
    expected_manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    mount_requests: list[object] = []
    mount_readbacks: list[object] = []
    provider, _created, _bootstrapped, _destroyed = _provider(
        monkeypatch,
        _FakePrivateSandbox(mount_probe_content=expected_manifest),
        mount_requests=mount_requests,
        mount_readbacks=mount_readbacks,
    )
    monkeypatch.setenv("ACT_WEAVE_HOST_BASE_DIR", str(host_base))
    monkeypatch.setenv("ACT_WEAVE_SANDBOX_HOST", "host.docker.internal")
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-host-view",
            scope=_scope(),
            run_id="run-host-view",
            source=source,
        )

        assert mount_requests == [
            [(expected_host_tree, "/mnt/skills", True)],
        ]
        assert mount_readbacks == [
            (expected_host_tree, "/mnt/skills"),
        ]
        assert type(provider.release_run_readonly_mount(lease)) is Released
    finally:
        _restore_mount_source(source)


def test_aio_run_mount_readiness_fails_closed_for_distinct_docker_host_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "worker-state")
    backend = object.__new__(LocalContainerBackend)
    backend._runtime = "docker"
    backend.run_readonly_mounts_ready = lambda: True
    provider = object.__new__(AioSandboxProvider)
    provider._backend = backend
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_app_config",
        lambda: SimpleNamespace(skills=SimpleNamespace(container_path="/mnt/skills")),
    )
    monkeypatch.setenv("ACT_WEAVE_SANDBOX_HOST", "host.docker.internal")
    monkeypatch.setenv(
        "ACT_WEAVE_HOST_BASE_DIR",
        str(tmp_path / "host-state"),
    )
    assert provider.run_readonly_mounts_ready() is False

    provider._backend = object()
    assert provider.run_readonly_mounts_ready() is False


def test_aio_run_mount_readiness_allows_shared_docker_host_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "native-state")
    backend = object.__new__(LocalContainerBackend)
    backend._runtime = "docker"
    backend.run_readonly_mounts_ready = lambda: True
    provider = object.__new__(AioSandboxProvider)
    provider._backend = backend
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_app_config",
        lambda: SimpleNamespace(skills=SimpleNamespace(container_path="/mnt/skills")),
    )
    monkeypatch.setenv("ACT_WEAVE_SANDBOX_HOST", "localhost")
    monkeypatch.delenv("ACT_WEAVE_HOST_BASE_DIR", raising=False)

    assert provider.run_readonly_mounts_ready() is True


def test_aio_typed_run_mount_retries_orphaned_release_until_exact_absence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_id = uuid.UUID("50000000-0000-0000-0000-000000000002")
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    expected_manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    provider, created, _bootstrapped, destroyed = _provider(
        monkeypatch,
        _FakePrivateSandbox(mount_probe_content=expected_manifest),
        destroy_errors=[RuntimeError("runtime destroy was inconclusive")],
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-1",
            scope=_scope(),
            run_id="run-typed-retry",
            source=source,
        )

        first = provider.release_run_readonly_mount(lease)
        second = provider.release_run_readonly_mount(lease)

        assert type(first) is Orphaned
        assert first.matches_lease(lease)
        assert type(second) is Released
        assert second.matches_lease(lease)
        assert destroyed == [created[0], created[0]]
        assert provider.release_run_readonly_mount(lease) is second
    finally:
        _restore_mount_source(source)


def test_aio_typed_run_mount_clears_tracking_after_runtime_is_already_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_id = uuid.UUID("50000000-0000-0000-0000-000000000004")
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    expected_manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    provider, created, _bootstrapped, destroyed = _provider(
        monkeypatch,
        _FakePrivateSandbox(mount_probe_content=expected_manifest),
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-1",
            scope=_scope(),
            run_id="run-already-absent",
            source=source,
        )
        provider._backend.destroy_private(created[0])

        outcome = provider.release_run_readonly_mount(lease)

        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
        assert destroyed == [created[0], created[0]]
        _assert_no_private_state(provider)
    finally:
        _restore_mount_source(source)


def test_aio_typed_run_mount_reserves_owner_before_container_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_id = uuid.UUID("50000000-0000-0000-0000-000000000003")
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    expected_manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    create_entered = threading.Event()
    allow_create = threading.Event()
    owner_requests: list[object] = []
    provider, _created, _bootstrapped, _destroyed = _provider(
        monkeypatch,
        _FakePrivateSandbox(mount_probe_content=expected_manifest),
        owner_requests=owner_requests,
        create_entered=create_entered,
        allow_create=allow_create,
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )
    leases: list[object] = []
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            leases.append(
                provider.prepare_run_readonly_mount(
                    "thread-1",
                    scope=_scope(),
                    run_id="run-pending-owner",
                    source=source,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        assert create_entered.wait(timeout=5)
        with pytest.raises(SandboxRuntimeError, match="already registered"):
            provider.prepare_run_readonly_mount(
                "thread-2",
                scope=_scope(),
                run_id="run-duplicate-owner",
                source=source,
            )
    finally:
        allow_create.set()
        worker.join(timeout=5)

    try:
        assert not worker.is_alive()
        assert errors == []
        assert len(leases) == 1
        assert owner_requests == [owner_id.hex]
        assert type(provider.release_run_readonly_mount(leases[0])) is Released
    finally:
        _restore_mount_source(source)


def _require_real_apple_container_runtime(runtime: str) -> None:
    if runtime != "container":
        if os.environ.get("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION") == "1":
            pytest.fail("Apple Container runtime is required for the P-02 provider probe")
        pytest.skip("Apple Container is not the selected local runtime")


def test_required_p02_probe_fails_when_apple_container_runtime_is_not_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION", "1")

    with pytest.raises(pytest.fail.Exception):
        try:
            _require_real_apple_container_runtime("docker")
        except pytest.skip.Exception as exc:
            raise AssertionError("required P-02 runtime mismatch was skipped") from exc


@pytest.mark.provider_integration
@pytest.mark.p02_native_aio
def test_real_apple_container_typed_run_mount_probe_and_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.environ.get("ACT_WEAVE_RUN_REAL_APPLE_CONTAINER_TEST") != "1":
        if os.environ.get("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION") == "1":
            pytest.fail(
                "ACT_WEAVE_RUN_REAL_APPLE_CONTAINER_TEST=1 is required",
            )
        pytest.skip("requires the local Apple Container daemon and pinned AIO image")
    owner_id = uuid.uuid4()
    paths, source = _readonly_mount_source(tmp_path, owner_id)
    backend = LocalContainerBackend(
        image=DEFAULT_IMAGE,
        base_port=31415,
        container_prefix=f"actweave-p02-{uuid.uuid4().hex[:8]}",
        config_mounts=[],
        environment={},
    )
    _require_real_apple_container_runtime(backend.runtime)
    provider = object.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    provider._backend = backend
    provider._sandboxes = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._private_runtime_ids = set()
    provider._warm_pool = {}
    provider._shutdown_called = False
    provider._idle_checker_stop = threading.Event()
    provider._idle_checker_thread = None
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.get_paths",
        lambda: paths,
    )

    lease = None
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-real-p02",
            scope=_scope(),
            run_id=f"run-real-p02-{uuid.uuid4().hex}",
            source=source,
        )

        assert provider.readback_run_readonly_mount(lease) == lease
        outcome = provider.release_run_readonly_mount(lease)
        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
    finally:
        if lease is not None:
            for _attempt in range(2):
                try:
                    outcome = provider.release_run_readonly_mount(lease)
                except Exception:
                    continue
                if type(outcome) is Released:
                    break
        for info in tuple(provider._sandbox_infos.values()):
            try:
                backend.destroy_private(info)
            except Exception:
                pass
        _restore_mount_source(source)


def test_aio_private_initialization_failure_destroys_tracking_and_lease_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox(
        initialize_error=RuntimeError("initialization failed"),
    )
    provider, created, bootstrapped, destroyed = _provider(monkeypatch, sandbox)

    with pytest.raises(RuntimeError, match="initialization failed"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    assert len(created) == 1
    assert bootstrapped == created
    assert destroyed == created
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_preflight_failure_destroys_tracking_and_lease_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox(
        scan_error_root="/mnt/user-data/outputs",
    )
    provider, created, bootstrapped, destroyed = _provider(monkeypatch, sandbox)

    with pytest.raises(
        FileNotFoundError,
        match="/mnt/user-data/outputs",
    ):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    assert sandbox.initialize_calls == 1
    assert sandbox.scan_calls == [(root, 1) for root in _PRIVATE_ROOTS]
    assert len(created) == 1
    assert bootstrapped == created
    assert destroyed == created
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_backend_bootstrap_failure_strictly_destroys_without_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, bootstrapped, destroyed = _provider(
        monkeypatch,
        sandbox,
        bootstrap_error=RuntimeError("bootstrap failed"),
    )

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    assert len(created) == 1
    assert bootstrapped == created
    assert destroyed == created
    assert sandbox.initialize_calls == 0
    assert sandbox.scan_calls == []
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_readiness_failure_destroys_bootstrapped_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, bootstrapped, destroyed = _provider(monkeypatch, sandbox)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.wait_for_sandbox_ready",
        lambda _url, *, timeout: timeout != 60,
    )

    with pytest.raises(SandboxRuntimeError, match="failed readiness"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    assert len(created) == 1
    assert bootstrapped == created
    assert destroyed == created
    assert sandbox.initialize_calls == 0
    assert sandbox.scan_calls == []
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_failed_cleanup_keeps_provisional_tracking_for_shutdown_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, bootstrapped, destroyed = _provider(
        monkeypatch,
        sandbox,
        bootstrap_error=RuntimeError("bootstrap failed"),
        destroy_errors=[RuntimeError("first destroy failed")],
    )

    with pytest.raises(RuntimeError, match="first destroy failed"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    assert len(created) == 1
    info = created[0]
    assert bootstrapped == [info]
    assert destroyed == [info]
    assert provider._sandbox_infos == {info.sandbox_id: info}
    assert provider._private_runtime_ids == {info.sandbox_id}
    assert getattr(provider, "_private_leases", {}) == {}
    assert sandbox.close_calls == 0

    provider.shutdown()

    assert destroyed == [info, info]
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_shutdown_retries_strict_cleanup_until_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, _bootstrapped, destroyed = _provider(
        monkeypatch,
        sandbox,
        bootstrap_error=RuntimeError("bootstrap failed"),
        destroy_errors=[
            RuntimeError("acquire cleanup failed"),
            RuntimeError("first shutdown cleanup failed"),
        ],
    )

    with pytest.raises(RuntimeError, match="acquire cleanup failed"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
        )

    info = created[0]
    provider.shutdown()
    assert destroyed == [info, info]
    assert provider._sandbox_infos == {info.sandbox_id: info}
    assert provider._private_runtime_ids == {info.sandbox_id}

    provider.shutdown()

    assert destroyed == [info, info, info]
    assert sandbox.close_calls == 1
    _assert_no_private_state(provider)


def test_aio_private_guest_uses_isolated_absolute_python() -> None:
    calls: list[dict[str, object]] = []

    class Bash:
        def exec(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                data=SimpleNamespace(
                    stdout='{"ok":true,"data":{"initialized":true}}',
                    stderr="",
                    status="completed",
                    exit_code=0,
                )
            )

    sandbox = object.__new__(AioSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(bash=Bash())

    result = sandbox._execute_private_guest(
        {
            "version": 1,
            "action": "init_private_roots",
            "root": "/mnt/user-data",
            "path": "/mnt/user-data",
            "display_path": "/mnt/user-data",
        }
    )

    assert result == {"ok": True, "data": {"initialized": True}}
    assert len(calls) == 1
    command = calls[0]["command"]
    assert isinstance(command, str)
    assert command.startswith("/usr/bin/python3 -I -S -c ")
    assert calls[0]["max_output_length"] == 0


def test_aio_run_mount_probe_requires_unprivileged_read_only_guest_receipt() -> None:
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000001")
    manifest = f'{{"owner_id":"{owner_id}","schema_version":1}}\n'
    calls: list[dict[str, object]] = []

    class Bash:
        def exec(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                data=SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "data": {
                                "euid": 1000,
                                "owner_id": str(owner_id),
                                "readable": True,
                                "writable": False,
                            },
                        },
                        separators=(",", ":"),
                    ),
                    stderr="",
                    status="completed",
                    exit_code=0,
                ),
            )

    sandbox = object.__new__(AioSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(bash=Bash())

    sandbox.probe_run_readonly_mount(
        owner_id=owner_id,
        expected_manifest=manifest,
    )

    assert len(calls) == 1
    environment = calls[0]["env"]
    assert isinstance(environment, dict)
    request = json.loads(
        base64.b64decode(
            environment["ACT_WEAVE_PRIVATE_FILE_REQUEST_B64"],
            validate=True,
        ),
    )
    assert request == {
        "version": 1,
        "action": "probe_run_readonly_mount",
        "root": "/mnt/skills",
        "path": "/mnt/skills/.actweave-run-mount.json",
        "display_path": "/mnt/skills/.actweave-run-mount.json",
        "expected_owner_id": str(owner_id),
        "expected_manifest": manifest,
    }


def test_aio_private_guest_disables_truncation_for_clipboard_sized_image_read() -> None:
    image_bytes = b"x" * 445_553
    response = json.dumps(
        {
            "ok": True,
            "data": {"content": base64.b64encode(image_bytes).decode("ascii")},
        },
        separators=(",", ":"),
    )
    calls: list[dict[str, object]] = []

    class Bash:
        def exec(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            output = response if kwargs.get("max_output_length") == 0 else response[:50_000]
            return SimpleNamespace(
                data=SimpleNamespace(
                    stdout=output,
                    stderr="",
                    status="completed",
                    exit_code=0,
                )
            )

    sandbox = object.__new__(AioSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(bash=Bash())

    result = sandbox._execute_private_guest(
        {
            "version": 1,
            "action": "read",
            "root": "/mnt/user-data",
            "path": "/mnt/user-data/uploads/image.png",
            "display_path": "/mnt/user-data/uploads/image.png",
            "offset": 0,
            "size": len(image_bytes),
            "expected": {"dev": 1, "ino": 2, "size": len(image_bytes)},
        }
    )

    encoded = result["data"]["content"]
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded, validate=True) == image_bytes
    assert calls[0]["max_output_length"] == 0


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("running", None),
        ("timed_out", None),
        ("killed", None),
        ("completed", 7),
    ],
)
def test_aio_private_guest_rejects_unsuccessful_command_state(
    status: str,
    exit_code: int | None,
) -> None:
    class Bash:
        def exec(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                data=SimpleNamespace(
                    stdout='{"ok":true,"data":{"initialized":true}}',
                    stderr="provider detail that must stay private",
                    status=status,
                    exit_code=exit_code,
                )
            )

    sandbox = object.__new__(AioSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(bash=Bash())

    with pytest.raises(OSError, match="AIO private file helper did not complete successfully") as exc_info:
        sandbox._execute_private_guest(
            {
                "version": 1,
                "action": "init_private_roots",
                "root": "/mnt/user-data",
                "path": "/mnt/user-data",
                "display_path": "/mnt/user-data",
            }
        )

    assert "provider detail" not in str(exc_info.value)


@pytest.mark.parametrize(
    "container_path",
    [
        "/usr/bin",
        "/etc",
        "/opt/gem",
        "/home/gem",
    ],
)
def test_aio_private_run_mount_cannot_replace_trusted_guest_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    container_path: str,
) -> None:
    sandbox = _FakePrivateSandbox()
    provider, created, _bootstrapped, _destroyed = _provider(monkeypatch, sandbox)
    mount = RunScopedReadOnlyMount(
        run_id="run-1",
        host_path=str(tmp_path),
        container_path=container_path,
    )

    with pytest.raises(SandboxRuntimeError, match="overlaps private runtime"):
        provider.acquire_private(
            "thread-1",
            scope=_scope(),
            user_id="owner-1",
            run_id="run-1",
            mounts=(mount,),
        )

    assert created == []
    _assert_no_private_state(provider)
