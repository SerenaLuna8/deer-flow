from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from deerflow.community.remote_file_authority import (
    PRIVATE_GUEST_REQUEST_ENV,
    PRIVATE_GUEST_SCRIPT,
    RemotePrivateFileAuthority,
)
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.sandbox import PRIVATE_FILE_IO_CHUNK_SIZE
from deerflow.sandbox.sandbox_provider import (
    PrivateSandboxLease,
    RunScopedReadOnlyMount,
)


def _local_guest(request: dict[str, object]) -> dict[str, object]:
    env = dict(os.environ)
    env[PRIVATE_GUEST_REQUEST_ENV] = base64.b64encode(json.dumps(request, separators=(",", ":")).encode()).decode("ascii")
    completed = subprocess.run(
        [sys.executable, "-c", PRIVATE_GUEST_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture
def remote_authority(tmp_path: Path) -> RemotePrivateFileAuthority:
    virtual_root = tmp_path / "user-data"
    for name in ("workspace", "uploads", "outputs"):
        (virtual_root / name).mkdir(parents=True)

    def resolve(path: str) -> str:
        assert path == "/mnt/user-data" or path.startswith("/mnt/user-data/")
        suffix = path.removeprefix("/mnt/user-data").lstrip("/")
        return str(virtual_root / suffix)

    return RemotePrivateFileAuthority(execute=_local_guest, resolve_path=resolve)


def test_remote_contract_binary_reader_is_bounded_and_inode_bound(
    remote_authority: RemotePrivateFileAuthority,
    tmp_path: Path,
) -> None:
    payload = b"a" * PRIVATE_FILE_IO_CHUNK_SIZE + b"tail"
    target = tmp_path / "user-data" / "workspace" / "input.bin"
    target.write_bytes(payload)

    reader = remote_authority.open_regular_reader("/mnt/user-data/workspace/input.bin")
    assert reader.read(PRIVATE_FILE_IO_CHUNK_SIZE) == payload[:PRIVATE_FILE_IO_CHUNK_SIZE]
    with pytest.raises(ValueError, match="1 MiB"):
        reader.read(PRIVATE_FILE_IO_CHUNK_SIZE + 1)

    replacement = target.with_suffix(".replacement")
    replacement.write_bytes(b"x" * len(payload))
    replacement.replace(target)
    with pytest.raises(OSError, match="changed"):
        reader.read(PRIVATE_FILE_IO_CHUNK_SIZE)
    reader.close()


def test_remote_contract_atomic_writer_publishes_or_aborts_without_partial(
    remote_authority: RemotePrivateFileAuthority,
    tmp_path: Path,
) -> None:
    target = tmp_path / "user-data" / "workspace" / "output.bin"
    writer = remote_authority.open_atomic_writer("/mnt/user-data/workspace/output.bin")
    writer.write(b"first")
    assert not target.exists()
    with pytest.raises(ValueError, match="1 MiB"):
        writer.write(b"x" * (PRIVATE_FILE_IO_CHUNK_SIZE + 1))
    writer.write(b"second")
    writer.commit()
    assert target.read_bytes() == b"firstsecond"

    aborted = tmp_path / "user-data" / "workspace" / "aborted.bin"
    writer = remote_authority.open_atomic_writer("/mnt/user-data/workspace/aborted.bin")
    writer.write(b"partial")
    writer.abort()
    assert not aborted.exists()
    assert not tuple(aborted.parent.glob(".deerflow-private-*"))


def test_remote_contract_scan_is_nofollow_and_limit_bounded(
    remote_authority: RemotePrivateFileAuthority,
    tmp_path: Path,
) -> None:
    root = tmp_path / "user-data" / "workspace"
    (root / "a.txt").write_text("a")
    (root / "dir").mkdir()
    (root / "dir" / "b.txt").write_text("b")
    (root / "link").symlink_to(root / "a.txt")

    entries = tuple(
        remote_authority.list_secure_files(
            "/mnt/user-data/workspace",
            max_entries=10,
        )
    )
    by_path = {entry.path: entry.file_type for entry in entries}
    assert by_path["/mnt/user-data/workspace/a.txt"] == "regular"
    assert by_path["/mnt/user-data/workspace/dir"] == "directory"
    assert by_path["/mnt/user-data/workspace/link"] == "symlink"

    with pytest.raises(OSError, match="entry limit"):
        tuple(
            remote_authority.list_secure_files(
                "/mnt/user-data/workspace",
                max_entries=1,
            )
        )


def test_remote_contract_rejects_escape_and_link_ancestor(
    remote_authority: RemotePrivateFileAuthority,
    tmp_path: Path,
) -> None:
    root = tmp_path / "user-data" / "workspace"
    (root / "outside").symlink_to(tmp_path)
    with pytest.raises((PermissionError, OSError)):
        remote_authority.open_atomic_writer("/mnt/user-data/workspace/outside/escape.bin")
    with pytest.raises(PermissionError):
        remote_authority.open_regular_reader("/mnt/user-data/../secret")


@pytest.mark.parametrize(
    "provider_path",
    [
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        "deerflow.community.e2b_sandbox:E2BSandboxProvider",
        "deerflow.community.boxlite:BoxliteProvider",
    ],
)
def test_remote_provider_private_lease_is_fresh_scoped_and_destroyed_not_warmed(
    provider_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.reflection import resolve_class
    from deerflow.sandbox.sandbox_provider import SandboxProvider

    provider_class = resolve_class(provider_path, SandboxProvider)
    provider = provider_class.__new__(provider_class)
    sandboxes: dict[str, object] = {}
    destroyed: list[str] = []
    identities: list[tuple[str, str, str, str]] = []

    def probe_files(root: str, *, max_entries: int):
        assert root == "/mnt/user-data/workspace"
        assert max_entries == 1
        return iter(())

    def create(*, scope, thread_id, run_id, mounts):
        identity = (scope.project_id, scope.owner_user_id, thread_id, run_id)
        identities.append(identity)
        sandbox_id = f"private-{len(identities)}"
        sandboxes[sandbox_id] = SimpleNamespace(list_secure_files=probe_files)
        return sandbox_id

    monkeypatch.setattr(provider, "_acquire_private_fresh", create)
    monkeypatch.setattr(provider, "get", sandboxes.get)
    monkeypatch.setattr(
        provider,
        "_destroy_private_sandbox",
        lambda sandbox_id: (destroyed.append(sandbox_id), sandboxes.pop(sandbox_id)),
    )
    legacy_calls: list[str] = []
    monkeypatch.setattr(
        provider,
        "acquire",
        lambda *_args, **_kwargs: legacy_calls.append("legacy"),
    )

    owner = str(uuid.uuid4())
    first_scope = PrivateResourceScope(str(uuid.uuid4()), owner, 1)
    second_scope = PrivateResourceScope(str(uuid.uuid4()), owner, 1)
    first = provider.acquire_private("thread-1", scope=first_scope, user_id=owner, run_id="run-1")
    second = provider.acquire_private("thread-1", scope=second_scope, user_id=owner, run_id="run-2")

    assert first.sandbox_id != second.sandbox_id
    assert first.relative_root == (f"projects/{first_scope.project_id}/users/{owner}/threads/thread-1")
    assert identities == [
        (first_scope.project_id, owner, "thread-1", "run-1"),
        (second_scope.project_id, owner, "thread-1", "run-2"),
    ]
    assert legacy_calls == []

    provider.release_private(first)
    assert destroyed == [first.sandbox_id]
    with pytest.raises(SandboxRuntimeError, match="lease"):
        provider.release_private(first)


def test_private_lease_rejects_forged_release_and_owner_mismatch(monkeypatch) -> None:
    from deerflow.community.aio_sandbox import AioSandboxProvider

    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    monkeypatch.setattr(
        provider,
        "_acquire_private_fresh",
        lambda **_kwargs: "private-one",
    )
    monkeypatch.setattr(
        provider,
        "get",
        lambda _sandbox_id: type(
            "Probe",
            (),
            {"list_secure_files": lambda self, root, max_entries: iter(())},
        )(),
    )
    monkeypatch.setattr(provider, "_destroy_private_sandbox", lambda _sid: None)

    owner = str(uuid.uuid4())
    scope = PrivateResourceScope(str(uuid.uuid4()), owner, 1)
    with pytest.raises(SandboxRuntimeError, match="owner"):
        provider.acquire_private("thread", scope=scope, user_id=str(uuid.uuid4()), run_id="run")

    lease = provider.acquire_private("thread", scope=scope, user_id=owner, run_id="run")
    forged = PrivateSandboxLease(
        sandbox_id=lease.sandbox_id,
        run_id="other-run",
        relative_root=lease.relative_root,
    )
    with pytest.raises(SandboxRuntimeError, match="lease"):
        provider.release_private(forged)


def test_private_release_failure_keeps_exact_lease_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox import AioSandboxProvider

    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    probe = SimpleNamespace(list_secure_files=lambda _root, *, max_entries: iter(()))
    monkeypatch.setattr(provider, "_acquire_private_fresh", lambda **_kwargs: "private-one")
    monkeypatch.setattr(provider, "get", lambda _sandbox_id: probe)
    attempts = 0

    def destroy(_sandbox_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient destroy failure")

    monkeypatch.setattr(provider, "_destroy_private_sandbox", destroy)
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    lease = provider.acquire_private("thread-a", scope=scope, user_id="owner-a", run_id="run-a")
    with pytest.raises(OSError, match="transient"):
        provider.release_private(lease)
    provider.release_private(lease)
    assert attempts == 2


def test_aio_private_guest_uses_fixed_command_and_structured_input() -> None:
    from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox

    calls: list[dict[str, object]] = []

    class Bash:
        def exec(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(data=SimpleNamespace(stdout='{"ok":true,"data":{}}', stderr=""))

    sandbox = AioSandbox.__new__(AioSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(bash=Bash())
    request = {
        "version": 1,
        "action": "remove",
        "root": "/mnt/user-data",
        "path": "/mnt/user-data/workspace/needle.bin",
    }
    assert sandbox._execute_private_guest(request) == {"ok": True, "data": {}}
    assert "needle.bin" not in calls[0]["command"]
    encoded = calls[0]["env"][PRIVATE_GUEST_REQUEST_ENV]
    assert json.loads(base64.b64decode(encoded))["path"].endswith("needle.bin")


def test_e2b_private_guest_uses_fixed_command_and_structured_input() -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox import E2BSandbox

    calls: list[tuple[str, dict[str, object]]] = []

    class Commands:
        def run(self, command: str, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(stdout='{"ok":true,"data":{}}', stderr="", exit_code=0)

    sandbox = E2BSandbox.__new__(E2BSandbox)
    sandbox._lock = threading.Lock()
    sandbox._client = SimpleNamespace(commands=Commands())
    sandbox._dead = False
    sandbox._execution_user = "deerflow_agent"
    request = {
        "version": 1,
        "action": "remove",
        "root": "/home/user",
        "path": "/home/user/workspace/needle.bin",
    }
    assert sandbox._execute_private_guest(request) == {"ok": True, "data": {}}
    assert "needle.bin" not in calls[0][0]
    encoded = calls[0][1]["envs"][PRIVATE_GUEST_REQUEST_ENV]
    assert json.loads(base64.b64decode(encoded))["path"].endswith("needle.bin")
    assert calls[0][1]["user"] == "deerflow_agent"


def test_boxlite_private_guest_uses_argv_and_structured_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.boxlite.box import BoxliteBox

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    sandbox = BoxliteBox.__new__(BoxliteBox)

    def execute(*argv: str, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout='{"ok":true,"data":{}}', stderr="", exit_code=0)

    monkeypatch.setattr(sandbox, "_exec", execute)
    request = {
        "version": 1,
        "action": "remove",
        "root": "/mnt/user-data",
        "path": "/mnt/user-data/workspace/needle.bin",
    }
    assert sandbox._execute_private_guest(request) == {"ok": True, "data": {}}
    assert calls[0][0][:2] == ("python3", "-c")
    assert "needle.bin" not in calls[0][0][2]
    encoded = calls[0][1]["env"][PRIVATE_GUEST_REQUEST_ENV]
    assert json.loads(base64.b64decode(encoded))["path"].endswith("needle.bin")


def test_aio_private_fresh_hook_never_uses_legacy_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from deerflow.community.aio_sandbox import aio_sandbox_provider as module
    from deerflow.community.aio_sandbox.aio_sandbox_provider import (
        AioSandboxProvider,
    )
    from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend

    creates: list[dict[str, object]] = []

    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    provider._backend = LocalContainerBackend.__new__(LocalContainerBackend)
    provider._backend._config_mounts = []
    provider._private_runtime_ids = set()
    provider._sandboxes = {}
    provider._sandbox_infos = {}

    def create(thread_id, sandbox_id, **kwargs):
        creates.append({"thread_id": thread_id, "sandbox_id": sandbox_id, **kwargs})
        return SimpleNamespace(sandbox_id=sandbox_id, sandbox_url="http://private")

    monkeypatch.setattr(provider._backend, "create", create)
    monkeypatch.setattr(module, "wait_for_sandbox_ready", lambda *_a, **_k: True)

    def register(_thread, sandbox_id, info, *, private=False):
        assert private is True
        provider._sandboxes[sandbox_id] = object()
        provider._sandbox_infos[sandbox_id] = info
        provider._private_runtime_ids.add(sandbox_id)

    monkeypatch.setattr(provider, "_register_created_sandbox", register)
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    mount = RunScopedReadOnlyMount(
        run_id="run-a",
        container_path="/mnt/skills",
        host_path=str(tmp_path),
    )
    first = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-a",
        mounts=(mount,),
    )
    second = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-b",
        mounts=(),
    )
    assert first != second
    assert all(call["thread_id"].startswith("private-") for call in creates)
    assert creates[0]["extra_mounts"] == [(str(tmp_path), "/mnt/skills", True)]


@pytest.mark.parametrize("case", ["rw_parent", "rw_alias", "socket"])
def test_aio_private_local_rejects_unsafe_config_mounts_before_create(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox.aio_sandbox_provider import (
        AioSandboxProvider,
    )
    from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend

    run_root = tmp_path / "run-skills"
    run_root.mkdir()
    host_path = run_root
    container_path = "/mnt/reference"
    read_only = True
    if case == "rw_parent":
        host_path = tmp_path
        read_only = False
    elif case == "rw_alias":
        host_path = run_root
        read_only = False
    else:
        host_path = tmp_path
        container_path = "/var/run/docker.sock"

    backend = LocalContainerBackend.__new__(LocalContainerBackend)
    backend._config_mounts = [
        SimpleNamespace(
            host_path=str(host_path),
            container_path=container_path,
            read_only=read_only,
        )
    ]
    created: list[str] = []
    monkeypatch.setattr(
        backend,
        "create",
        lambda *_args, **_kwargs: created.append("created"),
    )
    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._backend = backend
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    mount = RunScopedReadOnlyMount(
        run_id="run-a",
        container_path="/mnt/skills",
        host_path=str(run_root),
    )
    with pytest.raises(SandboxRuntimeError):
        provider._acquire_private_fresh(
            scope=scope,
            thread_id="thread-a",
            run_id="run-a",
            mounts=(mount,),
        )
    assert created == []


def test_aio_idle_reaper_excludes_private_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox.aio_sandbox_provider import (
        AioSandboxProvider,
    )

    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    provider._last_activity = {"private-a": 0.0, "legacy-a": 0.0}
    provider._private_runtime_ids = {"private-a"}
    destroyed: list[str] = []
    monkeypatch.setattr(provider, "destroy", destroyed.append)
    monkeypatch.setattr(provider, "_reap_expired_warm", lambda _timeout: None)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.aio_sandbox_provider.time.time",
        lambda: 100.0,
    )

    provider._cleanup_idle_sandboxes(10.0)
    assert destroyed == ["legacy-a"]


@pytest.mark.parametrize("with_mount", [False, True])
def test_aio_remote_private_fails_before_provisioner_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_mount: bool,
) -> None:
    from deerflow.community.aio_sandbox.aio_sandbox_provider import (
        AioSandboxProvider,
    )
    from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend

    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._backend = RemoteSandboxBackend.__new__(RemoteSandboxBackend)
    created: list[str] = []
    monkeypatch.setattr(
        provider._backend,
        "create",
        lambda *_args, **_kwargs: created.append("created"),
    )
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    mount = RunScopedReadOnlyMount(
        run_id="run-a",
        container_path="/mnt/skills",
        host_path=str(tmp_path),
    )
    with pytest.raises(SandboxRuntimeError, match="lacks required"):
        provider._acquire_private_fresh(
            scope=scope,
            thread_id="thread-a",
            run_id="run-a",
            mounts=(mount,) if with_mount else (),
        )
    assert created == []


def test_aio_private_destroy_failure_retains_tracking_and_lease_for_retry() -> None:
    from deerflow.community.aio_sandbox.aio_sandbox_provider import (
        AioSandboxProvider,
    )

    attempts: list[object] = []

    class Backend:
        def destroy_private(self, info) -> None:
            attempts.append(info)
            if len(attempts) == 1:
                raise OSError("transient destroy failure")

    closed: list[str] = []
    provider = AioSandboxProvider.__new__(AioSandboxProvider)
    provider._lock = threading.Lock()
    provider._backend = Backend()
    info = SimpleNamespace(sandbox_id="private-a", sandbox_url="http://private")
    provider._sandboxes = {"private-a": SimpleNamespace(close=lambda: closed.append("private-a"))}
    provider._sandbox_infos = {"private-a": info}
    provider._thread_sandboxes = {}
    provider._last_activity = {"private-a": 1.0}
    provider._warm_pool = {}
    lease = PrivateSandboxLease(
        sandbox_id="private-a",
        run_id="run-a",
        relative_root="projects/project-a/users/owner-a/threads/thread-a",
    )
    provider._private_leases = {lease.sandbox_id: lease}
    provider._private_lease_lock = threading.RLock()

    with pytest.raises(OSError, match="transient"):
        provider.release_private(lease)
    assert provider._sandbox_infos == {"private-a": info}
    assert provider._private_leases == {"private-a": lease}
    assert closed == []

    provider.release_private(lease)
    assert attempts == [info, info]
    assert provider._sandbox_infos == {}
    assert provider._private_leases == {}
    assert closed == ["private-a"]


def test_remote_backend_private_destroy_accepts_delete_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend
    from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

    backend = RemoteSandboxBackend("http://provisioner")
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.delete",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=404,
            ok=False,
            text="not found",
        ),
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.get",
        lambda *_args, **_kwargs: pytest.fail("404 DELETE must not poll"),
    )

    backend.destroy_private(SandboxInfo("private-a", "http://private"))


def test_remote_backend_private_destroy_polls_until_get_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend
    from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

    backend = RemoteSandboxBackend("http://provisioner")
    statuses = iter((200, 404))
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.delete",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=204, ok=True, text=""),
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.get",
        lambda *_args, **_kwargs: (
            lambda status: SimpleNamespace(
                status_code=status,
                ok=status == 200,
                text="",
            )
        )(next(statuses)),
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.time.sleep",
        lambda _seconds: None,
    )

    backend.destroy_private(SandboxInfo("private-a", "http://private"))


@pytest.mark.parametrize("get_status, succeeds", [(404, True), (200, False)])
def test_remote_backend_private_destroy_timeout_confirms_with_get_first(
    get_status: int,
    succeeds: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend
    from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

    backend = RemoteSandboxBackend("http://provisioner")
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.delete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )
    get_calls: list[str] = []

    def get(url: str, **_kwargs):
        get_calls.append(url)
        return SimpleNamespace(
            status_code=get_status,
            ok=get_status == 200,
            text="",
        )

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.remote_backend.requests.get",
        get,
    )
    info = SandboxInfo("private-a", "http://private")
    if succeeds:
        backend.destroy_private(info)
    else:
        with pytest.raises(RuntimeError, match="request failed"):
            backend.destroy_private(info)
    assert len(get_calls) == 1


def test_e2b_private_fresh_hook_persists_complete_remote_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox_provider import (
        META_KEY_PROJECT,
        META_KEY_RUN,
        META_KEY_THREAD,
        META_KEY_USER,
        E2BSandboxProvider,
    )

    create_kwargs: list[dict[str, object]] = []
    client = SimpleNamespace(sandbox_id="e2b-private-1")

    class ClientClass:
        @classmethod
        def create(cls, **kwargs):
            create_kwargs.append(kwargs)
            return client

    provider = E2BSandboxProvider.__new__(E2BSandboxProvider)
    provider._lock = threading.Lock()
    provider._sandboxes = {}
    provider._config = {
        "template": "template",
        "idle_timeout": 10,
        "environment": {},
        "home_dir": "/home/user",
    }
    monkeypatch.setattr(provider, "_get_sandbox_cls", lambda: ClientClass)
    monkeypatch.setattr(provider, "_common_kwargs", lambda: {})
    monkeypatch.setattr(
        provider,
        "_provision_private_runtime",
        lambda _client, _mounts: (),
    )
    monkeypatch.setattr(
        provider,
        "_probe_private_runtime",
        lambda _client, _roots: None,
    )
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    sandbox_id = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-a",
        mounts=(),
    )
    assert sandbox_id == "e2b-private-1"
    metadata = create_kwargs[0]["metadata"]
    assert metadata[META_KEY_PROJECT] == "project-a"
    assert metadata[META_KEY_USER] == "owner-a"
    assert metadata[META_KEY_THREAD] == "thread-a"
    assert metadata[META_KEY_RUN] == "run-a"


def test_boxlite_private_fresh_hook_is_per_run_and_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.boxlite.provider import BoxliteProvider

    closed: list[str] = []

    class Box:
        def __init__(self, sandbox_id: str):
            self.id = sandbox_id

        def close_private(self):
            closed.append(self.id)

    provider = BoxliteProvider.__new__(BoxliteProvider)
    provider._lock = threading.Lock()
    provider._boxes = {}
    provider._warm_pool = {}
    provider._skip_health_check_warm_ids = set()
    provider._thread_boxes = {}
    monkeypatch.setattr(
        provider,
        "_create_box",
        lambda sandbox_id, *, volumes=(): Box(sandbox_id),
    )
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    first = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-a",
        mounts=(),
    )
    second = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-b",
        mounts=(),
    )
    assert first != second
    provider._destroy_private_sandbox(first)
    assert closed == [first]
    assert first not in provider._boxes


def test_e2b_private_mount_is_root_provisioned_and_agent_user_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox_provider import (
        E2BSandboxProvider,
    )

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("trusted")
    command_calls: list[tuple[str, dict[str, object]]] = []
    write_calls: list[tuple[str, object, str | None]] = []

    class Commands:
        def run(self, command: str, **kwargs):
            command_calls.append((command, kwargs))
            if "import grp" in command:
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "uid": 1001,
                            "groups": ["deerflow_agent"],
                            "cap_eff": 0,
                            "no_new_privs": 1,
                            "sudo_returncode": 1,
                        }
                    ),
                    stderr="",
                    exit_code=0,
                )
            return SimpleNamespace(stdout="OK", stderr="", exit_code=0)

    class Files:
        def write(self, path: str, content: object, *, user: str | None = None):
            if user == "deerflow_agent":
                raise PermissionError("read-only")
            write_calls.append((path, content, user))

        def make_dir(self, _path: str, *, user: str | None = None):
            assert user == "root"

    client = SimpleNamespace(
        sandbox_id="e2b-private-user",
        commands=Commands(),
        files=Files(),
    )

    class ClientClass:
        @classmethod
        def create(cls, **_kwargs):
            return client

    provider = E2BSandboxProvider.__new__(E2BSandboxProvider)
    provider._lock = threading.Lock()
    provider._sandboxes = {}
    provider._config = {
        "template": "template",
        "idle_timeout": 10,
        "environment": {},
        "home_dir": "/home/user",
    }
    monkeypatch.setattr(provider, "_get_sandbox_cls", lambda: ClientClass)
    monkeypatch.setattr(provider, "_common_kwargs", lambda: {})
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    mount = RunScopedReadOnlyMount(
        run_id="run-a",
        container_path="/mnt/skills",
        host_path=str(skill_root),
    )

    sandbox_id = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-a",
        mounts=(mount,),
    )

    sandbox = provider._sandboxes[sandbox_id]
    assert sandbox._execution_user == "deerflow_agent"
    assert sandbox._read_only_roots == ("/mnt/skills",)
    assert write_calls == [("/mnt/skills/SKILL.md", b"trusted", "root")]
    assert any(kwargs.get("user") == "root" for _, kwargs in command_calls)
    assert any("import grp" in command and kwargs.get("user") == "deerflow_agent" for command, kwargs in command_calls)
    assert sum(kwargs.get("user") == "deerflow_agent" for _, kwargs in command_calls) >= 3


def test_e2b_private_adapter_forces_agent_user_and_rejects_skill_write() -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox import E2BSandbox

    calls: list[tuple[str, dict[str, object]]] = []
    file_calls: list[tuple[str, str, dict[str, object]]] = []

    class Commands:
        def run(self, command: str, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

    class Files:
        def read(self, path: str, **kwargs):
            file_calls.append(("read", path, kwargs))
            return b"data"

        def write(self, path: str, _content, **kwargs):
            file_calls.append(("write", path, kwargs))

    sandbox = E2BSandbox(
        "private",
        SimpleNamespace(commands=Commands(), files=Files()),
        home_dir="/home/deerflow_agent/user-data",
        execution_user="deerflow_agent",
        read_only_roots=("/mnt/skills",),
    )
    assert sandbox.execute_command("id") == "ok"
    assert sandbox.ping()
    assert sandbox.read_file("/mnt/user-data/workspace/read.txt") == "data"
    sandbox.write_file("/mnt/user-data/workspace/write.txt", "data")
    sandbox.update_file("/mnt/user-data/workspace/update.bin", b"data")
    sandbox.list_dir("/mnt/user-data/workspace")
    sandbox.glob("/mnt/user-data/workspace", "*.txt")
    sandbox.grep("/mnt/user-data/workspace", "needle", literal=True)
    assert calls and all(kwargs["user"] == "deerflow_agent" for _, kwargs in calls)
    assert file_calls and all(kwargs["user"] == "deerflow_agent" for _, _, kwargs in file_calls)
    with pytest.raises(PermissionError, match="read-only"):
        sandbox.update_file("/mnt//skills/./evil", b"bad")


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("uid", 0),
        ("groups", ["deerflow_agent", "sudo"]),
        ("groups", ["deerflow_agent", "docker"]),
        ("cap_eff", 1),
        ("no_new_privs", 0),
        ("sudo_returncode", 0),
    ],
)
def test_e2b_private_probe_rejects_unsafe_identity(
    field: str,
    unsafe_value: object,
) -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox_provider import (
        E2BSandboxProvider,
    )

    payload = {
        "uid": 1001,
        "groups": ["deerflow_agent"],
        "cap_eff": 0,
        "no_new_privs": 1,
        "sudo_returncode": 1,
    }
    payload[field] = unsafe_value
    client = SimpleNamespace(
        commands=SimpleNamespace(
            run=lambda *_args, **_kwargs: SimpleNamespace(
                stdout=json.dumps(payload),
                stderr="",
                exit_code=0,
            )
        )
    )

    with pytest.raises(SandboxRuntimeError, match="identity boundary"):
        E2BSandboxProvider._probe_private_runtime(client, ("/mnt/skills",))


@pytest.mark.parametrize("bypass", ["shell", "file_api"])
def test_e2b_private_probe_rejects_writable_skill_boundary(bypass: str) -> None:
    from deerflow.community.e2b_sandbox.e2b_sandbox_provider import (
        E2BSandboxProvider,
    )

    class Commands:
        def run(self, command: str, **_kwargs):
            if "import grp" in command:
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "uid": 1001,
                            "groups": ["deerflow_agent"],
                            "cap_eff": 0,
                            "no_new_privs": 1,
                            "sudo_returncode": 1,
                        }
                    ),
                    stderr="",
                    exit_code=0,
                )
            exit_code = 91 if bypass == "shell" and "if (: >" in command else 0
            return SimpleNamespace(stdout="", stderr="", exit_code=exit_code)

    class Files:
        def write(self, *_args, **_kwargs):
            if bypass != "file_api":
                raise PermissionError("read-only")

    client = SimpleNamespace(commands=Commands(), files=Files())
    with pytest.raises(SandboxRuntimeError, match="write probe|bypassed"):
        E2BSandboxProvider._probe_private_runtime(client, ("/mnt/skills",))


def test_boxlite_private_mount_uses_native_hypervisor_read_only_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.community.boxlite.provider import BoxliteProvider

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("trusted")
    create_calls: list[tuple[str, tuple[tuple[str, str, str], ...]]] = []

    class Box:
        id = "box-private-user"

        def close_private(self):
            return None

    provider = BoxliteProvider.__new__(BoxliteProvider)
    provider._lock = threading.Lock()
    provider._boxes = {}

    def create_box(sandbox_id: str, *, volumes=()):
        create_calls.append((sandbox_id, volumes))
        return Box()

    monkeypatch.setattr(provider, "_create_box", create_box)
    scope = PrivateResourceScope("project-a", "owner-a", 1)
    mount = RunScopedReadOnlyMount(
        run_id="run-a",
        container_path="/mnt/skills",
        host_path=str(skill_root),
    )
    sandbox_id = provider._acquire_private_fresh(
        scope=scope,
        thread_id="thread-a",
        run_id="run-a",
        mounts=(mount,),
    )
    assert sandbox_id in provider._boxes
    assert create_calls == [(sandbox_id, ((str(skill_root), "/mnt/skills", "ro"),))]
