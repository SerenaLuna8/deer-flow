"""SubagentExecutor acceptance test for the typed context carrier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_subagent_executor_installs_context_through_typed_carrier() -> None:
    completed = subprocess.run(
        [sys.executable, "tests/support/subagent_context_carrier_probe.py"],
        cwd=_BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "status": "completed",
        "keys": [
            "__authorization_boundary",
            "__authorization_checker",
            "__file_authority",
            "__guardrail_attribution",
            "__run_read_only_mounts",
            "__skill_scoped_secrets",
            "__skill_secret_provider",
            "app_config",
            "channel_user_id",
            "deerflow_trace_id",
            "is_subagent",
            "private_scope",
            "run_id",
            "thread_id",
            "user_id",
            "user_role",
        ],
        "is_subagent": True,
        "guardrail_is_subagent": True,
        "secret_copy": True,
    }
