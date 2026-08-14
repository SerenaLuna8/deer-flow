from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.aio_sandbox_provider import DEFAULT_IMAGE
from deerflow.community.aio_sandbox.backend import wait_for_sandbox_ready
from deerflow.community.aio_sandbox.local_backend import (
    LocalContainerBackend,
    _is_container_name_conflict,
)
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.community.remote_file_authority import PRIVATE_ROOT_BOOTSTRAP_SCRIPT


def _backend(*, runtime: str = "container") -> LocalContainerBackend:
    backend = object.__new__(LocalContainerBackend)
    backend._image = "example.invalid/aio-sandbox:1.0"
    backend._base_port = 31415
    backend._container_prefix = "deer-flow-sandbox"
    backend._config_mounts = []
    backend._environment = {}
    backend._runtime = runtime
    return backend


def test_default_aio_image_is_pinned_to_structured_bash_runtime() -> None:
    assert DEFAULT_IMAGE.endswith("/all-in-one-sandbox:1.11.0")


@pytest.mark.parametrize("runtime", ["container", "docker"])
def test_private_root_bootstrap_uses_fixed_root_exec_argv(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    backend = _backend(runtime=runtime)
    calls: list[tuple[list[str], dict[str, object]]] = []
    info = SandboxInfo(
        sandbox_id="private-test1234",
        sandbox_url="http://192.168.64.5:8080",
        container_name="deer-flow-sandbox-private-test1234",
        container_id="runtime-container-id",
    )

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        fake_run,
    )

    backend.initialize_private_roots(info)

    assert calls == [
        (
            [
                runtime,
                "exec",
                "--user",
                "0:0",
                "deer-flow-sandbox-private-test1234",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-c",
                PRIVATE_ROOT_BOOTSTRAP_SCRIPT,
            ],
            {
                "capture_output": True,
                "text": True,
                "check": True,
                "timeout": 30,
            },
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(
            7,
            ["container", "exec"],
            stderr="permission denied",
        ),
        subprocess.TimeoutExpired(["container", "exec"], 30),
    ],
)
def test_private_root_bootstrap_fails_closed_on_exec_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    backend = _backend()
    info = SandboxInfo(
        sandbox_id="private-test1234",
        sandbox_url="http://192.168.64.5:8080",
        container_name="deer-flow-sandbox-private-test1234",
        container_id="runtime-container-id",
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RuntimeError, match="Failed to initialize private sandbox roots"):
        backend.initialize_private_roots(info)


@pytest.mark.parametrize("runtime", ["container", "docker"])
def test_private_create_excludes_every_config_mount(
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    backend = _backend(runtime=runtime)
    backend._config_mounts = [
        SimpleNamespace(
            host_path="/host/private-rw",
            container_path="/data/rw",
            read_only=False,
        ),
        SimpleNamespace(
            host_path="/host/private-ro",
            container_path="/data/ro",
            read_only=True,
        ),
    ]
    starts: list[tuple[str, int | None, object, bool]] = []

    def start(
        name: str,
        port: int | None,
        extra_mounts: object = None,
        *,
        include_config_mounts: bool = True,
    ) -> str:
        starts.append((name, port, extra_mounts, include_config_mounts))
        return name

    monkeypatch.setattr(backend, "_start_container", start)
    if runtime == "container":
        monkeypatch.setattr(
            backend,
            "_wait_for_apple_container_network",
            lambda _name, timeout=10: _apple_container_payload()[0],
        )
    else:
        monkeypatch.setattr(
            "deerflow.community.aio_sandbox.local_backend.get_free_port",
            lambda **_kwargs: 31415,
        )

    info = backend.create_private(
        None,
        "private-test1234",
        extra_mounts=[("/run/skill", "/mnt/skills/private", True)],
    )

    assert info.sandbox_id == "private-test1234"
    assert starts == [
        (
            "deer-flow-sandbox-private-test1234",
            None if runtime == "container" else 31415,
            [("/run/skill", "/mnt/skills/private", True)],
            False,
        )
    ]


@pytest.mark.parametrize(
    ("runtime", "port"),
    [("container", None), ("docker", 31415)],
)
def test_container_start_log_redacts_mount_host_sources(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    runtime: str,
    port: int | None,
) -> None:
    backend = _backend(runtime=runtime)
    secret_source = "/host/private/run-secret/skill"
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="container-id\n",
            stderr="",
        ),
    )

    with caplog.at_level(logging.INFO):
        backend._start_container(
            "deer-flow-sandbox-private-test1234",
            port,
            [(secret_source, "/mnt/skills/private", True)],
            include_config_mounts=False,
        )

    assert secret_source not in caplog.text
    assert "<redacted>" in caplog.text
    assert "/mnt/skills/private" in caplog.text


def test_container_start_failure_does_not_disclose_runtime_stderr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend()
    secret_source = "/host/private/run-secret/skill"

    def fail(cmd: list[str], **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            125,
            cmd,
            stderr=f"bind source path does not exist: {secret_source}",
        )

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        fail,
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc_info:
            backend._start_container(
                "deer-flow-sandbox-private-test1234",
                None,
                [(secret_source, "/mnt/skills/private", True)],
                include_config_mounts=False,
            )

    assert secret_source not in caplog.text
    assert secret_source not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def _apple_container_payload(
    *,
    container_id: str = "deer-flow-sandbox-test1234",
    state: str = "running",
    ipv4_address: str = "192.168.64.5/24",
    managed: bool = True,
) -> list[dict[str, object]]:
    labels = {
        "io.actweave.sandbox.managed": "true",
        "io.actweave.sandbox.schema": "1",
    }
    if not managed:
        labels = {}
    return [
        {
            "id": container_id,
            "configuration": {
                "id": container_id,
                "creationDate": "2026-08-14T09:00:00Z",
                "labels": labels,
            },
            "status": {
                "state": state,
                "networks": [{"network": "default", "ipv4Address": ipv4_address}],
            },
        }
    ]


def test_apple_start_uses_managed_labels_without_publishing_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(cmd)
        return SimpleNamespace(stdout="apple-container-id\n", stderr="", returncode=0)

    monkeypatch.setattr("deerflow.community.aio_sandbox.local_backend.subprocess.run", fake_run)

    assert backend._start_container("deer-flow-sandbox-test1234", None) == "apple-container-id"
    command = captured[0]
    assert "-p" not in command
    assert "--publish" not in command
    assert ["--label", "io.actweave.sandbox.managed=true"] == command[command.index("--label") : command.index("--label") + 2]
    assert "io.actweave.sandbox.schema=1" in command


def test_apple_list_running_uses_structured_list_output(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        assert cmd == ["container", "list", "--format", "json"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload()),
            stderr="",
        )

    monkeypatch.setattr("deerflow.community.aio_sandbox.local_backend.subprocess.run", fake_run)

    infos = backend.list_running()

    assert calls == [["container", "list", "--format", "json"]]
    assert len(infos) == 1
    assert infos[0].sandbox_id == "test1234"
    assert infos[0].container_name == "deer-flow-sandbox-test1234"
    assert infos[0].sandbox_url == "http://192.168.64.5:8080"
    assert infos[0].created_at > 0


def test_apple_list_running_ignores_unmanaged_prefix_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload(managed=False)),
            stderr="",
        ),
    )

    assert backend.list_running() == []


def test_apple_is_alive_parses_inspect_without_docker_format_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        assert cmd == ["container", "inspect", "deer-flow-sandbox-test1234"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload()),
            stderr="",
        )

    monkeypatch.setattr("deerflow.community.aio_sandbox.local_backend.subprocess.run", fake_run)

    assert backend._is_container_running("deer-flow-sandbox-test1234") is True


def test_apple_is_alive_rejects_malformed_inspect_output(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )

    with pytest.raises(RuntimeError, match="Failed to parse Apple Container inspect output"):
        backend._is_container_running("deer-flow-sandbox-test1234")


def test_apple_discover_uses_one_inspect_and_private_vm_address(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        assert cmd == ["container", "inspect", "deer-flow-sandbox-test1234"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload()),
            stderr="",
        )

    monkeypatch.setattr("deerflow.community.aio_sandbox.local_backend.subprocess.run", fake_run)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.wait_for_sandbox_ready",
        lambda url, timeout: url == "http://192.168.64.5:8080" and timeout == 5,
    )

    info = backend.discover("test1234")

    assert info is not None
    assert info.sandbox_url == "http://192.168.64.5:8080"
    assert calls == [["container", "inspect", "deer-flow-sandbox-test1234"]]


def test_apple_create_does_not_reserve_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    payload = _apple_container_payload()[0]
    starts: list[tuple[str, int | None]] = []

    monkeypatch.setattr(
        backend,
        "_start_container",
        lambda name, port, _mounts=None: starts.append((name, port)) or name,
    )
    monkeypatch.setattr(backend, "_wait_for_apple_container_network", lambda _name, timeout=10: payload)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.get_free_port",
        lambda **_kwargs: pytest.fail("Apple Container must not reserve a host port"),
    )

    info = backend.create(None, "test1234")

    assert starts == [("deer-flow-sandbox-test1234", None)]
    assert info.sandbox_url == "http://192.168.64.5:8080"


def test_apple_create_cleans_up_if_network_address_never_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    stopped: list[str] = []

    monkeypatch.setattr(backend, "_start_container", lambda *_args, **_kwargs: "deer-flow-sandbox-test1234")
    monkeypatch.setattr(backend, "_wait_for_apple_container_network", lambda _name, timeout=10: None)
    monkeypatch.setattr(backend, "_stop_private_container", stopped.append)

    with pytest.raises(RuntimeError, match="network address"):
        backend.create(None, "test1234")

    assert stopped == ["deer-flow-sandbox-test1234"]


def test_apple_create_cleans_up_if_network_inspection_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    stopped: list[str] = []

    monkeypatch.setattr(backend, "_start_container", lambda *_args, **_kwargs: "deer-flow-sandbox-test1234")
    monkeypatch.setattr(
        backend,
        "_wait_for_apple_container_network",
        lambda _name, timeout=10: (_ for _ in ()).throw(RuntimeError("inspect unavailable")),
    )
    monkeypatch.setattr(backend, "_stop_private_container", stopped.append)

    with pytest.raises(RuntimeError, match="inspect unavailable"):
        backend.create(None, "test1234")

    assert stopped == ["deer-flow-sandbox-test1234"]


def test_apple_destroy_does_not_release_container_internal_port(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    stopped: list[str] = []
    info = SandboxInfo(
        sandbox_id="test1234",
        sandbox_url="http://192.168.64.5:8080",
        container_name="deer-flow-sandbox-test1234",
    )
    monkeypatch.setattr(backend, "_stop_container", stopped.append)
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.release_port",
        lambda _port: pytest.fail("Apple's internal port was never reserved on the host"),
    )

    backend.destroy(info)

    assert stopped == ["deer-flow-sandbox-test1234"]


def test_apple_is_alive_rejects_same_name_with_changed_network_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload(ipv4_address="192.168.64.9/24")),
            stderr="",
        ),
    )
    info = SandboxInfo(
        sandbox_id="test1234",
        sandbox_url="http://192.168.64.5:8080",
        container_name="deer-flow-sandbox-test1234",
    )

    assert backend.is_alive(info) is False


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Conflict. The container name is already in use by container abc", True),
        ("container with id deer-flow-sandbox-test1234 already exists", True),
        ("container with id deer-flow-sandbox-test1234 failed inspection", False),
        ("runtime state already exists", False),
    ],
)
def test_container_name_conflict_requires_a_complete_runtime_signature(message: str, expected: bool) -> None:
    assert _is_container_name_conflict(message) is expected


def test_private_sandbox_readiness_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    observations: list[bool] = []

    class FakeSession:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, *, timeout: int):
            assert url == "http://192.168.64.5:8080/v1/sandbox"
            assert timeout == 5
            observations.append(self.trust_env)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.backend.requests.Session",
        FakeSession,
    )
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.backend.requests.get",
        lambda *_args, **_kwargs: pytest.fail("readiness must use its proxy-controlled Session"),
    )

    assert wait_for_sandbox_ready("http://192.168.64.5:8080", timeout=1) is True
    assert observations == [False]


@pytest.mark.parametrize(
    "ipv4_address",
    ["", "not-an-ip", "0.0.0.0/0", "::1/128"],
)
def test_apple_list_running_fails_closed_on_unusable_network_address(
    monkeypatch: pytest.MonkeyPatch,
    ipv4_address: str,
) -> None:
    backend = _backend()
    monkeypatch.setattr(
        "deerflow.community.aio_sandbox.local_backend.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_apple_container_payload(ipv4_address=ipv4_address)),
            stderr="",
        ),
    )

    infos = backend.list_running()

    assert infos == []
