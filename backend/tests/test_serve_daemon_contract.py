import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVE_SCRIPT = REPO_ROOT / "scripts" / "serve.sh"


def test_macos_daemon_uses_a_launchd_supervisor_instead_of_nohup_children() -> None:
    source = SERVE_SCRIPT.read_text(encoding="utf-8")
    start = source.index("_start_macos_daemon() {")
    block = source[start : source.index("# Pick a runnable Python", start)]

    assert 'launchctl submit -l "$label"' in block
    assert '"$REPO_ROOT/scripts/serve.sh" "$mode_flag" "${start_args[@]}"' in block
    assert "--daemon" not in block
    assert 'if $DAEMON_MODE && [ "$(uname -s)" = "Darwin" ]; then' in block


def test_make_stop_unloads_macos_daemon_supervisors() -> None:
    source = SERVE_SCRIPT.read_text(encoding="utf-8")
    stop_block = source[source.index("stop_all() {") : source.index("# ── Action routing")]

    assert "_stop_macos_daemon_supervisors" in stop_block
    assert '"com.actweave.deerflow.dev"' in source
    assert '"com.actweave.deerflow.prod"' in source
    assert 'launchctl remove "$label"' in source


def test_make_stop_tolerates_listener_exit_during_port_reporting() -> None:
    source = SERVE_SCRIPT.read_text(encoding="utf-8")
    start = source.index("_report_reclaimed_ports() {")
    block = source[start : source.index("_kill_repo_processes() {", start)]

    assert 'files=$(lsof -b -w -p "$pid" 2>/dev/null) || continue' in block


def test_foreground_supervisor_fails_and_stops_peers_when_a_child_exits() -> None:
    source = SERVE_SCRIPT.read_text(encoding="utf-8")
    start = source.index("# ── Cleanup handler")
    block = source[start : source.index("# ── Helper: start a service", start)]
    completed = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
{block}
(sleep 0.1) &
remember_started_pid "$!" "Worker"
(sleep 30) &
printf 'survivor_pid=%s\n' "$!"
remember_started_pid "$!" "Gateway"
supervise_started_processes || cleanup "$?"
""",
            "serve-supervisor-test",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 1
    assert "Worker exited after startup" in completed.stderr
    survivor_pid = int(next(line.removeprefix("survivor_pid=") for line in completed.stdout.splitlines() if line.startswith("survivor_pid=")))
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(survivor_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("supervisor left a peer child process running")
