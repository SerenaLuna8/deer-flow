"""Regression coverage for #3758: macOS nginx argv rewriting broke make stop."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVE_SH = REPO_ROOT / "scripts" / "serve.sh"


def _extract_shell_function(name: str) -> str:
    text = SERVE_SH.read_text(encoding="utf-8")
    marker = f"{name}() {{"
    start = text.index(marker)
    depth = 0
    chunks: list[str] = []

    for line in text[start:].splitlines(keepends=True):
        chunks.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(chunks)

    raise AssertionError(f"Could not extract shell function {name}")


def _is_repo_nginx_pid(
    *,
    command: str,
    args: str,
    repo_root: Path,
    deerflow_pid: bool = False,
) -> bool:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise serve.sh helpers")

    function = _extract_shell_function("_is_repo_nginx_pid")
    script = f"""
REPO_ROOT={shlex.quote(str(repo_root))}
DEERFLOW_ROOTS={shlex.quote(str(repo_root))}
FAKE_COMMAND={shlex.quote(command)}
FAKE_ARGS={shlex.quote(args)}
FAKE_DEERFLOW_PID={1 if deerflow_pid else 0}

_is_deerflow_pid() {{
    [ "$FAKE_DEERFLOW_PID" = "1" ]
}}

ps() {{
    case "$*" in
        *"-o comm="*) printf '%s\\n' "$FAKE_COMMAND" ;;
        *"-o args="*) printf '%s\\n' "$FAKE_ARGS" ;;
        *) return 1 ;;
    esac
}}

{function}

_is_repo_nginx_pid 12345
"""
    result = subprocess.run([bash, "-c", script], check=False)
    return result.returncode == 0


def test_repo_nginx_pid_accepts_macos_rewritten_master_command(tmp_path):
    repo_root = tmp_path / "deer-flow"
    nginx_conf = repo_root / "docker" / "nginx" / "nginx.local.conf"

    assert _is_repo_nginx_pid(
        command=f"nginx: master process /opt/homebrew/bin/nginx -c {nginx_conf}",
        args=f"nginx: master process /opt/homebrew/bin/nginx -c {nginx_conf} -p {repo_root}",
        repo_root=repo_root,
    )


def test_repo_nginx_pid_accepts_macos_rewritten_worker_after_repo_check(tmp_path):
    repo_root = tmp_path / "deer-flow"

    assert _is_repo_nginx_pid(
        command="nginx: worker process",
        args="nginx: worker process",
        repo_root=repo_root,
        deerflow_pid=True,
    )


@pytest.mark.parametrize(
    ("command", "args", "deerflow_pid"),
    [
        ("nginx: worker process", "nginx: worker process", False),
        ("python", "python -m nginx /tmp/deer-flow/docker/nginx/nginx.local.conf", True),
    ],
)
def test_repo_nginx_pid_rejects_unowned_or_non_nginx_processes(
    tmp_path,
    command: str,
    args: str,
    deerflow_pid: bool,
):
    assert not _is_repo_nginx_pid(
        command=command,
        args=args,
        repo_root=tmp_path / "deer-flow",
        deerflow_pid=deerflow_pid,
    )


def test_ordinary_start_does_not_call_broad_stop_all() -> None:
    text = SERVE_SH.read_text(encoding="utf-8")
    start_routing = text[text.index('if [ "$ACTION" = "stop" ]') : text.index("# ── Config check")]
    assert 'if [ "$ACTION" = "stop" ]' in start_routing
    assert 'if [ "$ACTION" = "restart" ]' in start_routing
    assert "ALREADY_STOPPED" not in start_routing


def test_foreground_cleanup_stops_only_invocation_started_processes() -> None:
    cleanup = _extract_shell_function("cleanup")
    assert "stop_started" in cleanup
    assert "stop_all" not in cleanup


def test_port_probe_trusts_available_lsof_before_fallbacks(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise serve.sh helpers")
    binary = tmp_path / "bin"
    binary.mkdir()
    lsof = binary / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    lsof.chmod(0o755)
    ss = binary / "ss"
    ss.write_text(
        "#!/bin/sh\nprintf 'State Local Address:Port\\nLISTEN 127.0.0.1:8001\\n'\n",
        encoding="utf-8",
    )
    ss.chmod(0o755)

    result = subprocess.run(
        [bash, "-c", f"{_extract_shell_function('_is_port_listening')}\n_is_port_listening 8001"],
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}"},
        check=False,
    )

    assert result.returncode == 1


def test_broad_stop_is_reachable_only_from_explicit_stop_or_restart() -> None:
    text = SERVE_SH.read_text(encoding="utf-8")
    routing = text[text.index("# ── Action routing") : text.index("# Mode label for banner")]
    assert routing.count("stop_all") == 2
    assert 'ACTION" = "stop"' in routing
    assert 'ACTION" = "restart"' in routing
