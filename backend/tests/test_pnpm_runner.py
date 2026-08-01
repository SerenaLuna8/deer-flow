from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "pnpm.py"
FRONTEND_DIR = REPO_ROOT / "frontend"


def _load_runner() -> ModuleType:
    assert RUNNER_PATH.is_file(), "Module 16 requires the shared scripts/pnpm.py runner"
    spec = importlib.util.spec_from_file_location("module16_pnpm_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_command(
    bin_dir: Path,
    name: str,
    label: str,
    *,
    exit_code: int = 0,
) -> Path:
    if os.name == "nt":
        path = bin_dir / f"{name}.cmd"
        path.write_text(
            f"@echo off\r\necho {label}^|%CD%^|%*\r\nexit /b {exit_code}\r\n",
            encoding="utf-8",
        )
    else:
        path = bin_dir / name
        path.write_text(
            f"#!/bin/sh\nprintf '%s|%s|%s\\n' '{label}' \"$PWD\" \"$*\"\nexit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    return path


def _run_runner(
    path: Path,
    *arguments: str,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(path)
    return subprocess.run(
        [sys.executable, str(RUNNER_PATH), *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


def test_find_pnpm_command_prefers_direct_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner()
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: "/toolchain/pnpm" if name == "pnpm" else None,
    )

    assert runner.find_pnpm_command() == ["/toolchain/pnpm"]


def test_find_pnpm_command_falls_back_to_corepack(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner()
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: "/toolchain/corepack" if name == "corepack" else None,
    )

    assert runner.find_pnpm_command() == ["/toolchain/corepack", "pnpm"]


def test_find_pnpm_command_falls_back_to_corepack_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: r"C:\Program Files\nodejs\corepack.cmd" if name == "corepack.cmd" else None,
    )

    assert runner.find_pnpm_command() == [
        r"C:\Program Files\nodejs\corepack.cmd",
        "pnpm",
    ]


def test_runner_uses_frontend_cwd_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "find_pnpm_command", lambda: ["/toolchain/pnpm"])
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.run_pnpm(["run", "dev"]) == 0
    assert observed == {
        "command": ["/toolchain/pnpm", "run", "dev"],
        "check": False,
        "shell": False,
        "cwd": REPO_ROOT / "frontend",
    }


def test_runner_normalizes_signal_and_propagates_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "find_pnpm_command", lambda: ["/toolchain/pnpm"])
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], -9),
    )
    assert runner.run_pnpm(["test"]) == 137

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 42),
    )
    assert runner.run_pnpm(["test"]) == 42


def test_runner_returns_126_when_selected_binary_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "find_pnpm_command", lambda: ["/toolchain/pnpm"])

    def fail_to_launch(*_args, **_kwargs):
        raise OSError("cannot execute selected binary")

    monkeypatch.setattr(runner.subprocess, "run", fail_to_launch)

    assert runner.run_pnpm(["test"]) == 126


def test_runner_returns_127_when_pnpm_and_corepack_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "find_pnpm_command", lambda: None)

    assert runner.run_pnpm(["test"]) == 127


def test_corepack_only_preview_uses_runner_without_nested_bare_pnpm(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_command(bin_dir, "corepack", "corepack")

    result = _run_runner(bin_dir, "run", "preview")

    assert result.returncode == 0
    assert result.stdout.strip() == f"corepack|{FRONTEND_DIR}|pnpm run preview"
    assert result.stderr.strip() == "Using pnpm via Corepack."

    package_json = json.loads((FRONTEND_DIR / "package.json").read_text(encoding="utf-8"))
    assert package_json["scripts"]["preview"] == "next build --webpack && next start"
    assert "pnpm" not in package_json["scripts"]["preview"]


def test_all_local_entrypoints_use_the_shared_runner_and_role_accurate_banner() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    frontend_makefile = (FRONTEND_DIR / "Makefile").read_text(encoding="utf-8")
    serve = (REPO_ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")
    check_script = (REPO_ROOT / "scripts" / "check.py").read_text(encoding="utf-8")
    doctor_script = (REPO_ROOT / "scripts" / "doctor.py").read_text(encoding="utf-8")
    support_bundle = (REPO_ROOT / "scripts" / "support_bundle.py").read_text(encoding="utf-8")

    assert "./scripts/pnpm.py install" in makefile
    assert "PNPM = $(PYTHON) ../scripts/pnpm.py" in frontend_makefile
    assert '"$DEERFLOW_PNPM_PYTHON" "$DEERFLOW_PNPM_RUNNER" install --silent' in serve
    assert 'DEERFLOW_PNPM_RUNNER="$REPO_ROOT/scripts/pnpm.py"' in serve
    assert 'FRONTEND_CMD=\'"$DEERFLOW_PNPM_PYTHON" "$DEERFLOW_PNPM_RUNNER" run dev\'' in serve
    assert '"\\$DEERFLOW_PNPM_RUNNER\\" run preview"' in serve
    assert 'Path(__file__).with_name("pnpm.py")' in check_script
    assert 'Path(__file__).with_name("pnpm.py")' in doctor_script
    assert 'project_root / "scripts" / "pnpm.py"' in support_bundle

    assert "(admission/query/SSE)" in serve
    assert "(Agent graph execution)" in serve
    assert "Gateway runtime" not in serve
    assert "REST API + agent runtime" not in serve


def test_make_dry_runs_do_not_invoke_bare_pnpm() -> None:
    root = subprocess.run(
        ["make", "-n", "install"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    frontend = subprocess.run(
        [
            "make",
            "-n",
            "install",
            "build",
            "dev",
            "test",
            "test-e2e",
            "lint",
            "format",
            "build-static",
        ],
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )

    assert root.returncode == 0, root.stderr
    assert "./scripts/pnpm.py install" in root.stdout
    assert "cd frontend && pnpm install" not in root.stdout

    assert frontend.returncode == 0, frontend.stderr
    assert "../scripts/pnpm.py" in frontend.stdout
    for bare_invocation in (
        "pnpm install",
        "pnpm build",
        "pnpm dev",
        "pnpm test",
        "pnpm test:e2e",
        "pnpm lint",
        "pnpm format:write",
    ):
        assert bare_invocation not in frontend.stdout
