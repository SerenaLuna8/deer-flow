"""Core behavior tests for present_files path normalization."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

present_file_tool_module = importlib.import_module("deerflow.tools.builtins.present_file_tool")


def _make_runtime(outputs_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        state={"thread_data": {"outputs_path": outputs_path}},
        context={"thread_id": "thread-1"},
        config={},
    )


def test_present_files_normalizes_host_outputs_path(tmp_path):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    outputs_dir.mkdir(parents=True)
    artifact_path = outputs_dir / "report.md"
    artifact_path.write_text("ok")

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=[str(artifact_path)],
        tool_call_id="tc-1",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/report.md"]
    assert result.update["messages"][0].content == "Successfully presented files"


def test_present_files_keeps_virtual_outputs_path(tmp_path, monkeypatch):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    outputs_dir.mkdir(parents=True)
    artifact_path = outputs_dir / "summary.json"
    artifact_path.write_text("{}")

    monkeypatch.setattr(
        present_file_tool_module,
        "get_paths",
        lambda: SimpleNamespace(resolve_virtual_path=lambda thread_id, path, *, user_id=None: artifact_path),
    )

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=["/mnt/user-data/outputs/summary.json"],
        tool_call_id="tc-2",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/summary.json"]


def test_present_files_uses_config_thread_id_when_context_missing(tmp_path, monkeypatch):
    outputs_dir = tmp_path / "threads" / "thread-from-config" / "user-data" / "outputs"
    outputs_dir.mkdir(parents=True)
    artifact_path = outputs_dir / "summary.json"
    artifact_path.write_text("{}")

    monkeypatch.setattr(
        present_file_tool_module,
        "get_paths",
        lambda: SimpleNamespace(resolve_virtual_path=lambda thread_id, path: artifact_path),
    )

    runtime = SimpleNamespace(
        state={"thread_data": {"outputs_path": str(outputs_dir)}},
        context={},
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    result = present_file_tool_module.present_file_tool.func(
        runtime=runtime,
        filepaths=["/mnt/user-data/outputs/summary.json"],
        tool_call_id="tc-config",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/summary.json"]
    assert result.update["messages"][0].content == "Successfully presented files"


def test_present_files_rejects_paths_outside_outputs(tmp_path):
    outputs_dir = tmp_path / "threads" / "thread-1" / "user-data" / "outputs"
    workspace_dir = tmp_path / "threads" / "thread-1" / "user-data" / "workspace"
    outputs_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)
    leaked_path = workspace_dir / "notes.txt"
    leaked_path.write_text("leak")

    result = present_file_tool_module.present_file_tool.func(
        runtime=_make_runtime(str(outputs_dir)),
        filepaths=[str(leaked_path)],
        tool_call_id="tc-3",
    )

    assert "artifacts" not in result.update
    assert result.update["messages"][0].content == f"Error: Only files in /mnt/user-data/outputs can be presented: {leaked_path}"


def test_private_present_files_registers_opaque_authority_without_host_resolution(
    monkeypatch,
):
    authority = SimpleNamespace(record_presented_paths=Mock())
    runtime = SimpleNamespace(
        state={
            "thread_data": {
                "outputs_path": "/mnt/user-data/outputs",
            }
        },
        context={
            "thread_id": "thread-private",
            "__file_authority": authority,
        },
        config={},
    )

    def reject_host_resolution():
        raise AssertionError("private present_files used legacy host paths")

    monkeypatch.setattr(present_file_tool_module, "get_paths", reject_host_resolution)
    result = present_file_tool_module.present_file_tool.func(
        runtime=runtime,
        filepaths=["/mnt/user-data/outputs/report.md"],
        tool_call_id="tc-private",
    )

    assert result.update["artifacts"] == ["/mnt/user-data/outputs/report.md"]
    authority.record_presented_paths.assert_called_once_with(("/mnt/user-data/outputs/report.md",))


def test_private_present_files_invalid_batch_registers_nothing(monkeypatch):
    authority = SimpleNamespace(record_presented_paths=Mock())
    runtime = SimpleNamespace(
        state={"thread_data": {"outputs_path": "/mnt/user-data/outputs"}},
        context={
            "thread_id": "thread-private",
            "__file_authority": authority,
        },
        config={},
    )

    def reject_host_resolution():
        raise AssertionError("private present_files used legacy host paths")

    monkeypatch.setattr(present_file_tool_module, "get_paths", reject_host_resolution)
    result = present_file_tool_module.present_file_tool.func(
        runtime=runtime,
        filepaths=[
            "/mnt/user-data/outputs/report.md",
            "/mnt/user-data/workspace/private.txt",
        ],
        tool_call_id="tc-private-invalid",
    )

    assert "artifacts" not in result.update
    authority.record_presented_paths.assert_not_called()
