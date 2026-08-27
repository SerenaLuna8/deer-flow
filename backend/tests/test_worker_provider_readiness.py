from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.worker.app import _handlers_for_run_mount_readiness
from app.worker.service import WorkerService
from deerflow.config.worker_config import WorkerConfig
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider


class _Registry:
    def __init__(self) -> None:
        self.registration: tuple[object, ...] | None = None

    async def register(self, *args: object, **kwargs: object) -> None:
        self.registration = (*args, kwargs)


@pytest.mark.parametrize(
    ("allow_host_bash", "skills_container_path", "expected"),
    [
        (False, "/mnt/skills", True),
        (True, "/mnt/skills", False),
        (False, "/custom/skills", False),
    ],
)
def test_local_provider_readiness_matches_exact_run_mount_configuration(
    monkeypatch: pytest.MonkeyPatch,
    allow_host_bash: bool,
    skills_container_path: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        "deerflow.config.get_app_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(allow_host_bash=allow_host_bash),
            skills=SimpleNamespace(container_path=skills_container_path),
        ),
    )
    provider = object.__new__(LocalSandboxProvider)

    assert provider.run_readonly_mounts_ready() is expected


def test_local_provider_readiness_fails_closed_when_config_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("deerflow.config.get_app_config", unavailable)
    provider = object.__new__(LocalSandboxProvider)

    assert provider.run_readonly_mounts_ready() is False


@pytest.mark.asyncio
async def test_provider_unready_worker_registers_without_run_capabilities() -> None:
    registry = _Registry()
    handlers = _handlers_for_run_mount_readiness(
        {
            "private_run": object(),
            "automation_run": object(),
            "memory_seal": object(),
        },
        ready=False,
    )
    service = WorkerService(
        None,
        registry,  # type: ignore[arg-type]
        handlers,  # type: ignore[arg-type]
        WorkerConfig(),
    )

    await service._register()

    assert registry.registration is not None
    _worker_id, capabilities, _capacity, _kwargs = registry.registration
    assert capabilities == frozenset({"memory_seal"})


def test_provider_ready_worker_keeps_both_run_capabilities() -> None:
    handlers = {
        "private_run": object(),
        "automation_run": object(),
        "memory_seal": object(),
    }

    assert _handlers_for_run_mount_readiness(handlers, ready=True) == handlers
    assert handlers.keys() == {
        "private_run",
        "automation_run",
        "memory_seal",
    }
