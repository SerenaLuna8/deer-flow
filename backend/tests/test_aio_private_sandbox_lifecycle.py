from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.private_scope import PrivateResourceScope
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


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    sandbox: _FakePrivateSandbox,
    *,
    bootstrap_error: Exception | None = None,
    destroy_errors: list[Exception] | None = None,
) -> tuple[
    AioSandboxProvider,
    list[SandboxInfo],
    list[SandboxInfo],
    list[SandboxInfo],
]:
    created: list[SandboxInfo] = []
    bootstrapped: list[SandboxInfo] = []
    destroyed: list[SandboxInfo] = []

    def create(
        _thread_id: str,
        sandbox_id: str,
        *,
        extra_mounts: object,
        user_id: str,
    ) -> SandboxInfo:
        del extra_mounts, user_id
        info = SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url="http://192.168.64.5:8080",
            container_name=f"deer-flow-sandbox-{sandbox_id}",
            container_id=f"deer-flow-sandbox-{sandbox_id}",
        )
        created.append(info)
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

    backend.destroy_private = destroy_private
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
