import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from deerflow.sandbox.tooling import runtime as sandbox_runtime
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
    ("invoke", "expected", "call_site"),
    [
        pytest.param(
            _call_ls,
            f"Error: Permission denied: {_REQUESTED_PATH}",
            "deerflow.sandbox.tooling.files",
            id="ls",
        ),
        pytest.param(
            _call_glob,
            f"Error: Permission denied: {_REQUESTED_PATH}",
            "deerflow.sandbox.tooling.search_tools",
            id="glob",
        ),
        pytest.param(
            _call_grep,
            f"Error: Permission denied: {_REQUESTED_PATH}",
            "deerflow.sandbox.tooling.search_tools",
            id="grep",
        ),
        pytest.param(
            _call_str_replace,
            f"Error: Permission denied accessing file: {_REQUESTED_PATH}",
            "deerflow.sandbox.tooling.files",
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
    call_site: str,
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
            f"{call_site}.ensure_sandbox_initialized",
            fail_with_permission_error,
        )
    else:
        monkeypatch.setattr(
            f"{call_site}.ensure_sandbox_initialized",
            lambda _runtime: object(),
        )
        monkeypatch.setattr(
            f"{call_site}.ensure_thread_directories_exist",
            fail_with_permission_error,
        )

    assert invoke(runtime) == expected


@pytest.mark.asyncio
async def test_sync_and_async_initialization_use_matching_provider_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sandbox:
        id = "sandbox-1"

    class Provider:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.sandbox = Sandbox()

        def acquire(self, thread_id: str, *, user_id: str | None = None) -> str:
            assert thread_id == "thread-1"
            del user_id
            self.calls.append("acquire")
            return self.sandbox.id

        async def acquire_async(
            self,
            thread_id: str,
            *,
            user_id: str | None = None,
        ) -> str:
            assert thread_id == "thread-1"
            del user_id
            self.calls.append("acquire_async")
            return self.sandbox.id

        def get(self, sandbox_id: str) -> Sandbox:
            assert sandbox_id == self.sandbox.id
            return self.sandbox

    def runtime() -> SimpleNamespace:
        return SimpleNamespace(
            state={},
            context={"thread_id": "thread-1"},
            config={},
        )

    sync_provider = Provider()
    monkeypatch.setattr(
        sandbox_runtime,
        "get_sandbox_provider",
        lambda: sync_provider,
    )
    sync_runtime = runtime()
    assert sandbox_runtime.ensure_sandbox_initialized(sync_runtime) is sync_provider.sandbox
    assert sandbox_runtime.ensure_sandbox_initialized(sync_runtime) is sync_provider.sandbox
    assert sync_provider.calls == ["acquire"]

    async_provider = Provider()
    monkeypatch.setattr(
        sandbox_runtime,
        "get_sandbox_provider",
        lambda: async_provider,
    )
    async_runtime = runtime()
    assert await sandbox_runtime.ensure_sandbox_initialized_async(async_runtime) is async_provider.sandbox
    assert await sandbox_runtime.ensure_sandbox_initialized_async(async_runtime) is async_provider.sandbox
    assert async_provider.calls == ["acquire_async"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["before_sandbox_exec", "before_sandbox_write"],
)
async def test_async_tool_wrapper_authorizes_before_off_thread_invocation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    events: list[str] = []
    owner_thread_id = threading.get_ident()
    invoked_thread_id: int | None = None
    runtime = SimpleNamespace(
        state={},
        context={"thread_id": "thread-1"},
        config={},
    )

    async def initialize(_runtime: object) -> object:
        events.append("initialize")
        return object()

    async def authorize(context: object, seen_operation: str) -> None:
        assert context is runtime.context
        assert seen_operation == operation
        events.append(f"authorize:{seen_operation}")

    def invoke(call_runtime: object, payload: object) -> str:
        nonlocal invoked_thread_id
        assert call_runtime is runtime
        assert payload == "payload"
        invoked_thread_id = threading.get_ident()
        events.append("invoke")
        return "OK"

    monkeypatch.setattr(
        sandbox_runtime,
        "ensure_sandbox_initialized_async",
        initialize,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.sandbox.check_authorization_boundary",
        authorize,
    )

    result = await sandbox_runtime._run_sync_tool_after_async_sandbox_init(
        invoke,
        runtime,
        "payload",
        authorization_operation=operation,
    )

    assert result == "OK"
    assert events == [
        "initialize",
        f"authorize:{operation}",
        "invoke",
    ]
    assert invoked_thread_id is not None
    assert invoked_thread_id != owner_thread_id
