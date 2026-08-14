from __future__ import annotations

import subprocess
import time

import pytest

from deerflow.sandbox.local import local_sandbox
from deerflow.sandbox.local.local_sandbox import (
    LocalProcessSpawnDeadlineExpired,
    LocalSandbox,
)


def test_posix_spawn_deadline_is_checked_immediately_before_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = False

    def forbidden_popen(*args: object, **kwargs: object) -> object:
        nonlocal spawned
        del args, kwargs
        spawned = True
        raise AssertionError("expired process must not be created")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    sandbox = LocalSandbox("local-run:test:thread:run")

    with pytest.raises(LocalProcessSpawnDeadlineExpired):
        sandbox.execute_prepared_command_result(
            "printf safe",
            shell="/bin/sh",
            prepared_base_env={"PATH": "/usr/bin:/bin"},
            timeout=1,
            spawn_authorization_guard=lambda: time.monotonic() - 1,
        )

    assert spawned is False


def test_windows_spawn_deadline_is_checked_immediately_before_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = False

    def forbidden_run(*args: object, **kwargs: object) -> object:
        nonlocal spawned
        del args, kwargs
        spawned = True
        raise AssertionError("expired process must not be created")

    monkeypatch.setattr(local_sandbox.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", forbidden_run)
    sandbox = LocalSandbox("local-run:test:thread:run")

    with pytest.raises(LocalProcessSpawnDeadlineExpired):
        sandbox.execute_prepared_command_result(
            "echo safe",
            shell="cmd.exe",
            prepared_base_env={"PATH": r"C:\Windows\System32"},
            timeout=1,
            spawn_authorization_guard=lambda: time.monotonic() - 1,
        )

    assert spawned is False
