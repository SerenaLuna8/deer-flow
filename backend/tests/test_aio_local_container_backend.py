from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.aio_sandbox_provider import DEFAULT_IMAGE
from deerflow.community.aio_sandbox.backend import wait_for_sandbox_ready
from deerflow.community.aio_sandbox.local_backend import (
    LocalContainerBackend,
    _is_container_name_conflict,
)
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo


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
