from collections.abc import Callable
from types import SimpleNamespace

import pytest

from deerflow.sandbox.tools import glob_tool, grep_tool, ls_tool, str_replace_tool

_REQUESTED_PATH = "/mnt/user-data/workspace/missing.txt"


def _call_ls(runtime: object) -> str:
    return ls_tool.func(
        runtime=runtime,
        description="list files",
        path=_REQUESTED_PATH,
    )


def _call_glob(runtime: object) -> str:
    return glob_tool.func(
        runtime=runtime,
        description="find files",
        pattern="*.txt",
        path=_REQUESTED_PATH,
    )


def _call_grep(runtime: object) -> str:
    return grep_tool.func(
        runtime=runtime,
        description="search files",
        pattern="needle",
        path=_REQUESTED_PATH,
    )


def _call_str_replace(runtime: object) -> str:
    return str_replace_tool.func(
        runtime=runtime,
        description="replace text",
        path=_REQUESTED_PATH,
        old_str="before",
        new_str="after",
    )


@pytest.mark.parametrize(
    ("invoke", "expected"),
    [
        pytest.param(_call_ls, f"Error: Permission denied: {_REQUESTED_PATH}", id="ls"),
        pytest.param(_call_glob, f"Error: Permission denied: {_REQUESTED_PATH}", id="glob"),
        pytest.param(_call_grep, f"Error: Permission denied: {_REQUESTED_PATH}", id="grep"),
        pytest.param(
            _call_str_replace,
            f"Error: Permission denied accessing file: {_REQUESTED_PATH}",
            id="str-replace",
        ),
    ],
)
@pytest.mark.parametrize(
    "failure_stage",
    ["sandbox-initialization", "thread-directory-initialization"],
)
def test_path_errors_keep_the_requested_path_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[object], str],
    expected: str,
    failure_stage: str,
) -> None:
    runtime = SimpleNamespace(
        state={},
        context={"thread_id": "thread-1"},
        config={},
    )

    def fail_with_permission_error(_runtime: object) -> None:
        raise PermissionError("initialization denied")

    if failure_stage == "sandbox-initialization":
        monkeypatch.setattr(
            "deerflow.sandbox.tools.ensure_sandbox_initialized",
            fail_with_permission_error,
        )
    else:
        monkeypatch.setattr(
            "deerflow.sandbox.tools.ensure_sandbox_initialized",
            lambda _runtime: object(),
        )
        monkeypatch.setattr(
            "deerflow.sandbox.tools.ensure_thread_directories_exist",
            fail_with_permission_error,
        )

    assert invoke(runtime) == expected
