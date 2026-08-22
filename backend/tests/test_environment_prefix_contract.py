from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_RETIRED_PREFIXES = ("DEER" + "_FLOW_", "DEER" + "FLOW_")


def test_tracked_repository_has_no_retired_environment_prefixes() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    offenders: list[str] = []

    for raw_path in tracked:
        if not raw_path:
            continue
        relative_path = raw_path.decode("utf-8")
        path = REPOSITORY_ROOT / relative_path
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
            continue
        if any(prefix in text for prefix in _RETIRED_PREFIXES):
            offenders.append(relative_path)

    assert offenders == []
