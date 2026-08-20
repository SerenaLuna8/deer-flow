from pathlib import Path

from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.sandbox import SandboxFileInfo


def test_local_secure_scan_prunes_workspace_runtime_tree_before_entry_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime_bin = workspace / ".venv" / "bin"
    runtime_bin.mkdir(parents=True)
    (runtime_bin / "python3").symlink_to("/usr/bin/python3")
    (workspace / "result.txt").write_text("kept", encoding="utf-8")
    sandbox = LocalSandbox(
        "local-run:test:workspace-runtime",
        [PathMapping("/mnt/user-data/workspace", str(workspace))],
    )

    entries = tuple(
        sandbox.list_secure_files(
            "/mnt/user-data/workspace",
            max_entries=1,
            excluded_root_names=(".venv",),
        )
    )

    assert entries == (
        SandboxFileInfo(
            path="/mnt/user-data/workspace/result.txt",
            size=4,
            file_type="regular",
        ),
    )


def test_local_secure_scan_keeps_same_named_workspace_symlink_visible(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".venv").symlink_to("/tmp/runtime")
    sandbox = LocalSandbox(
        "local-run:test:workspace-symlink",
        [PathMapping("/mnt/user-data/workspace", str(workspace))],
    )

    entries = tuple(
        sandbox.list_secure_files(
            "/mnt/user-data/workspace",
            max_entries=1,
            excluded_root_names=(".venv",),
        )
    )

    assert entries == (
        SandboxFileInfo(
            path="/mnt/user-data/workspace/.venv",
            size=len("/tmp/runtime"),
            file_type="symlink",
        ),
    )
