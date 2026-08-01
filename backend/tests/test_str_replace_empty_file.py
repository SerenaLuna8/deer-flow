"""Regression tests for empty files and empty ``old_str`` values."""

from pathlib import Path
from types import SimpleNamespace

from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import str_replace_tool


def _local_runtime(tmp_path: Path) -> SimpleNamespace:
    for sub in ("workspace", "uploads", "outputs"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local:t1"},
            "thread_data": {
                "workspace_path": str(tmp_path / "workspace"),
                "uploads_path": str(tmp_path / "uploads"),
                "outputs_path": str(tmp_path / "outputs"),
            },
        },
        context={"thread_id": "t1"},
    )


def _str_replace(
    tmp_path: Path,
    monkeypatch,
    *,
    old_str: str,
    new_str: str = "x",
    content: str = "",
    replace_all: bool = False,
) -> tuple[str, str]:
    runtime = _local_runtime(tmp_path)
    target = tmp_path / "outputs" / "empty.txt"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        "deerflow.sandbox.tools.ensure_sandbox_initialized",
        lambda runtime: LocalSandbox("t1"),
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tools.ensure_thread_directories_exist",
        lambda runtime: None,
    )
    result = str_replace_tool.func(
        runtime=runtime,
        description="replace content",
        path="/mnt/user-data/outputs/empty.txt",
        old_str=old_str,
        new_str=new_str,
        replace_all=replace_all,
    )
    return result, target.read_text(encoding="utf-8")


def test_empty_file_with_non_empty_old_str_reports_not_found(
    tmp_path,
    monkeypatch,
) -> None:
    result, _ = _str_replace(tmp_path, monkeypatch, old_str="something")
    assert result.startswith("Error: String to replace not found in file")
    assert "empty.txt" in result


def test_empty_file_with_empty_old_str_returns_ok(tmp_path, monkeypatch) -> None:
    result, _ = _str_replace(tmp_path, monkeypatch, old_str="")
    assert result == "OK"


def test_non_empty_file_with_empty_old_str_is_a_no_op(
    tmp_path,
    monkeypatch,
) -> None:
    source = "def main():\n    return 1\n"
    result, after = _str_replace(
        tmp_path,
        monkeypatch,
        old_str="",
        new_str="# header\n",
        content=source,
    )
    assert result == "OK"
    assert after == source


def test_non_empty_file_with_empty_old_str_and_replace_all_is_a_no_op(
    tmp_path,
    monkeypatch,
) -> None:
    source = "def main():\n    return 1\n"
    result, after = _str_replace(
        tmp_path,
        monkeypatch,
        old_str="",
        new_str="X",
        content=source,
        replace_all=True,
    )
    assert result == "OK"
    assert after == source
