from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.worker.app import _handlers_for_run_mount_readiness
from app.worker.service import WorkerService
from deerflow.config.worker_config import WorkerConfig
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


class _Registry:
    def __init__(self) -> None:
        self.registration: tuple[object, ...] | None = None

    async def register(self, *args: object, **kwargs: object) -> None:
        self.registration = (*args, kwargs)


@pytest.mark.parametrize(
    ("test_paths", "case_marker", "expected_nodeid"),
    [
        (
            ("tests/test_run_skill_mount_lease.py",),
            "p01_native_local",
            "tests/test_run_skill_mount_lease.py::test_native_local_mount_readback_is_read_only_and_release_proves_absence",
        ),
        (
            (
                "tests/test_aio_private_sandbox_lifecycle.py",
                "tests/test_aio_local_container_backend.py",
            ),
            "p02_native_aio",
            "tests/test_aio_private_sandbox_lifecycle.py::test_real_apple_container_typed_run_mount_probe_and_release",
        ),
    ],
)
def test_native_provider_release_commands_collect_one_registered_real_probe(
    test_paths: tuple[str, ...],
    case_marker: str,
    expected_nodeid: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--strict-markers",
            "--collect-only",
            "-q",
            *test_paths,
            "-m",
            f"provider_integration and {case_marker}",
        ],
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    collected_nodeids = [line.strip() for line in result.stdout.splitlines() if "::test_" in line]
    assert collected_nodeids == [expected_nodeid]


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
