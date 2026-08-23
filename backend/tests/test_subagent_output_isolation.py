from __future__ import annotations

import asyncio
import importlib
import io
import threading
import uuid
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.file_finalizer import PrivateFileFinalizer
from app.private_work.sandbox_files import PrivateFileRunScope, PrivateRunFileAuthority
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.file_authority import AuthorityManifest
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.sandbox.sandbox_provider import PrivateSandboxLease
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
)
from deerflow.subagents.config import SubagentConfig

task_module = importlib.import_module("deerflow.tools.builtins.task_tool")


class _MemorySandbox:
    id = "private-sandbox-1"

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self._readers: dict[str, io.BytesIO] = {}
        self._writers: dict[str, tuple[str, io.BytesIO]] = {}
        self.fail_list_roots: set[str] = set()
        self.fail_publish_paths: set[str] = set()
        self.fail_remove_paths: set[str] = set()

    def list_secure_files(
        self,
        root: str,
        *,
        max_entries: int,
        excluded_root_names: tuple[str, ...] = (),
    ) -> list[SimpleNamespace]:
        if root in self.fail_list_roots:
            raise OSError("injected secure scan failure")
        prefix = root.rstrip("/") + "/"
        excluded = set(excluded_root_names)
        entries: list[SimpleNamespace] = []
        for path, content in sorted(self.files.items()):
            if not path.startswith(prefix):
                continue
            relative = PurePosixPath(path[len(prefix) :])
            if relative.parts and relative.parts[0] in excluded:
                continue
            entries.append(
                SimpleNamespace(
                    path=path,
                    size=len(content),
                    file_type="regular",
                )
            )
            if len(entries) > max_entries:
                raise OSError("too many entries")
        return entries

    def open_regular_file(self, path: str) -> str:
        handle = uuid.uuid4().hex
        self._readers[handle] = io.BytesIO(self.files[path])
        return handle

    def read_regular_file(self, handle: str, size: int) -> bytes:
        return self._readers[handle].read(size)

    def close_regular_file(self, handle: str) -> None:
        self._readers.pop(handle).close()

    def begin_atomic_file(self, path: str) -> str:
        handle = uuid.uuid4().hex
        self._writers[handle] = (path, io.BytesIO())
        return handle

    def append_atomic_file(self, handle: str, content: bytes) -> None:
        self._writers[handle][1].write(content)

    def publish_atomic_file(self, handle: str) -> None:
        path, writer = self._writers.pop(handle)
        if path in self.fail_publish_paths:
            raise OSError("injected atomic publish failure")
        self.files[path] = writer.getvalue()

    def abort_atomic_file(self, handle: str) -> None:
        self._writers.pop(handle, None)

    def remove_file(self, path: str) -> None:
        if path in self.fail_remove_paths:
            raise OSError("injected file removal failure")
        self.files.pop(path, None)


class _FinalizerProbe:
    def __init__(self, sandbox: _MemorySandbox) -> None:
        self.sandbox = sandbox
        self.finalize_calls = 0
        self.mark_failed_calls = 0

    async def finalize(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.finalize_calls += 1
        assert not any(
            path.startswith(
                "/mnt/user-data/workspace/.deerflow/subagents/",
            )
            for path in self.sandbox.files
        )
        return "finalized"

    async def mark_failed(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.mark_failed_calls += 1
        assert not any(
            path.startswith(
                "/mnt/user-data/workspace/.deerflow/subagents/",
            )
            for path in self.sandbox.files
        )


def _authority(
    sandbox: _MemorySandbox,
    *,
    finalizer: object | None = None,
) -> PrivateRunFileAuthority:
    project = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="request-1",
    )
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            PrivateWorkContext.from_project(project),
            thread_id="thread-1",
            run_id="run-1",
        ),
        projection=object(),
        finalizer=finalizer or object(),
    )
    authority._sandbox = sandbox
    return authority


@pytest.mark.asyncio
async def test_restore_removes_stale_prior_run_scratch_before_projection() -> None:
    stale = "/mnt/user-data/workspace/.deerflow/subagents/prior-run/outputs/stale.md"
    user_owned = "/mnt/user-data/workspace/.deerflow/user-owned.md"
    sandbox = _MemorySandbox(
        {
            stale: b"must not reach the next run\n",
            user_owned: b"keep me\n",
        }
    )
    projected = False

    class Provider:
        async def acquire_private_async(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return PrivateSandboxLease(
                "private-sandbox-1",
                "run-1",
                "private/root",
            )

        def get(self, _sandbox_id: str) -> _MemorySandbox:
            return sandbox

    class Projection:
        async def restore(self, run_scope, observed_sandbox):  # type: ignore[no-untyped-def]
            nonlocal projected
            projected = True
            assert observed_sandbox is sandbox
            assert stale not in sandbox.files
            assert sandbox.files[user_owned] == b"keep me\n"
            return AuthorityManifest(entries=(), run_id=run_scope.run_id)

    authority = _authority(sandbox)
    authority._sandbox = None
    authority._provider = Provider()
    authority._projection = Projection()

    await authority.restore()

    assert projected is True
    assert stale not in sandbox.files
    assert sandbox.files[user_owned] == b"keep me\n"


@pytest.mark.asyncio
async def test_restore_treats_absent_runtime_scratch_root_as_empty() -> None:
    scratch_root = "/mnt/user-data/workspace/.deerflow/subagents"

    class MissingScratchSandbox(_MemorySandbox):
        def list_secure_files(
            self,
            root: str,
            *,
            max_entries: int,
            excluded_root_names: tuple[str, ...] = (),
        ) -> list[SimpleNamespace]:
            if root == scratch_root:
                raise FileNotFoundError(root)
            return super().list_secure_files(
                root,
                max_entries=max_entries,
                excluded_root_names=excluded_root_names,
            )

    sandbox = MissingScratchSandbox()
    projected = False

    class Provider:
        async def acquire_private_async(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return PrivateSandboxLease(
                "private-sandbox-1",
                "run-1",
                "private/root",
            )

        def get(self, _sandbox_id: str) -> _MemorySandbox:
            return sandbox

    class Projection:
        async def restore(self, run_scope, observed_sandbox):  # type: ignore[no-untyped-def]
            nonlocal projected
            projected = True
            assert observed_sandbox is sandbox
            return AuthorityManifest(entries=(), run_id=run_scope.run_id)

    authority = _authority(sandbox)
    authority._sandbox = None
    authority._provider = Provider()
    authority._projection = Projection()

    await authority.restore()

    assert projected is True


@pytest.mark.asyncio
async def test_delegated_outputs_require_lead_promotion_before_finalization() -> None:
    existing_path = "/mnt/user-data/outputs/existing.md"
    delegated_path = "/mnt/user-data/outputs/research.md"
    sandbox = _MemorySandbox({existing_path: b"lead baseline\n"})
    authority = _authority(sandbox)

    async with authority.delegated_output_scope("task-call-1") as capture:
        sandbox.files[f"{capture.output_root}/existing.md"] = b"subagent overwrite\n"
        sandbox.files[f"{capture.output_root}/research.md"] = b"delegated research\n"

    assert sandbox.files[existing_path] == b"lead baseline\n"
    assert delegated_path not in sandbox.files
    assert len(capture.promotable_paths) == 2
    isolated = {path: sandbox.files[path] for path in capture.promotable_paths}
    assert set(isolated.values()) == {
        b"subagent overwrite\n",
        b"delegated research\n",
    }
    assert all(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in isolated)
    assert {promotion.source_path for promotion in capture.promotions} == {
        existing_path,
        delegated_path,
    }
    assert {promotion.source_path: promotion.scratch_path for promotion in capture.promotions} == {
        source_path: next(path for path, content in isolated.items() if content == (b"subagent overwrite\n" if source_path == existing_path else b"delegated research\n")) for source_path in (existing_path, delegated_path)
    }


@pytest.mark.asyncio
async def test_concurrent_delegated_scopes_are_isolated_from_each_other_and_lead() -> None:
    lead_path = "/mnt/user-data/outputs/lead.md"
    sandbox = _MemorySandbox({lead_path: b"lead baseline\n"})
    authority = _authority(sandbox)
    both_entered = asyncio.Event()
    release = asyncio.Event()
    entered = 0

    async def run_task(task_id: str):
        nonlocal entered
        async with authority.delegated_output_scope(task_id) as capture:
            entered += 1
            sandbox.files[f"{capture.output_root}/{task_id}.md"] = f"{task_id}\n".encode()
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await release.wait()
        return capture

    first = asyncio.create_task(run_task("first"))
    second = asyncio.create_task(run_task("second"))
    await both_entered.wait()
    sandbox.files[lead_path] = b"lead concurrent write\n"
    release.set()
    first_capture, second_capture = await asyncio.gather(first, second)

    assert first_capture.output_root != second_capture.output_root
    assert sandbox.files[lead_path] == b"lead concurrent write\n"
    assert "/mnt/user-data/outputs/first.md" not in sandbox.files
    assert "/mnt/user-data/outputs/second.md" not in sandbox.files
    assert [promotion.source_path for promotion in first_capture.promotions] == ["/mnt/user-data/outputs/first.md"]
    assert [promotion.source_path for promotion in second_capture.promotions] == ["/mnt/user-data/outputs/second.md"]


@pytest.mark.asyncio
async def test_scratch_scan_failure_blocks_finalization_fail_closed() -> None:
    sandbox = _MemorySandbox()
    finalizer = _FinalizerProbe(sandbox)
    authority = _authority(sandbox, finalizer=finalizer)
    authority._manifest = SimpleNamespace()

    with pytest.raises(OSError, match="injected secure scan failure"):
        async with authority.delegated_output_scope("task-call-1") as capture:
            sandbox.files[f"{capture.output_root}/research.md"] = b"research\n"
            sandbox.fail_list_roots.add(capture.output_root)

    with pytest.raises(PrivateWorkUnavailable):
        await authority.finalize()
    assert finalizer.finalize_calls == 0
    assert not any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)


@pytest.mark.asyncio
async def test_successful_finalization_removes_exact_delegated_scratch_files() -> None:
    sandbox = _MemorySandbox()
    finalizer = _FinalizerProbe(sandbox)
    authority = _authority(sandbox, finalizer=finalizer)
    authority._manifest = SimpleNamespace()

    async with authority.delegated_output_scope("task-call-1") as capture:
        sandbox.files[f"{capture.output_root}/research.md"] = b"research\n"

    assert any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)
    assert await authority.finalize() == "finalized"
    assert finalizer.finalize_calls == 1
    assert not any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)


@pytest.mark.asyncio
async def test_failed_run_removes_exact_delegated_scratch_files() -> None:
    user_owned = "/mnt/user-data/workspace/.deerflow/user-owned.md"
    sandbox = _MemorySandbox({user_owned: b"keep me\n"})
    finalizer = _FinalizerProbe(sandbox)
    authority = _authority(sandbox, finalizer=finalizer)

    async with authority.delegated_output_scope("task-call-1") as capture:
        sandbox.files[f"{capture.output_root}/research.md"] = b"research\n"

    await authority.mark_failed()

    assert finalizer.mark_failed_calls == 1
    assert not any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)
    assert sandbox.files[user_owned] == b"keep me\n"


@pytest.mark.asyncio
async def test_scratch_cleanup_failure_still_attempts_failed_run_settlement() -> None:
    scratch = "/mnt/user-data/workspace/.deerflow/subagents/capture/outputs/research.md"
    sandbox = _MemorySandbox({scratch: b"research\n"})
    sandbox.fail_remove_paths.add(scratch)

    class FailureFinalizer:
        mark_failed_calls = 0

        async def mark_failed(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            self.mark_failed_calls += 1

    finalizer = FailureFinalizer()
    authority = _authority(sandbox, finalizer=finalizer)

    with pytest.raises(OSError, match="injected file removal failure"):
        await authority.mark_failed()

    assert finalizer.mark_failed_calls == 1


@pytest.mark.asyncio
async def test_cancelled_delegated_task_is_cleaned_when_run_is_marked_failed() -> None:
    sandbox = _MemorySandbox()
    finalizer = _FinalizerProbe(sandbox)
    authority = _authority(sandbox, finalizer=finalizer)
    entered = asyncio.Event()

    async def delegated_task() -> None:
        async with authority.delegated_output_scope("task-call-1") as capture:
            sandbox.files[f"{capture.output_root}/research.md"] = b"research\n"
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(delegated_task())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)
    await authority.mark_failed()
    assert finalizer.mark_failed_calls == 1
    assert not any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)


@pytest.mark.asyncio
async def test_cancel_while_waiting_to_finish_does_not_leave_active_scratch() -> None:
    sandbox = _MemorySandbox()
    finalizer = _FinalizerProbe(sandbox)
    authority = _authority(sandbox, finalizer=finalizer)
    first_scan_started = threading.Event()
    allow_first_scan = threading.Event()
    original_scan = authority._secure_regular_files
    first_root: str | None = None

    def blocking_scan(root: str, *, max_entries: int, missing_ok: bool):
        if root == first_root:
            first_scan_started.set()
            assert allow_first_scan.wait(timeout=2)
        return original_scan(
            root,
            max_entries=max_entries,
            missing_ok=missing_ok,
        )

    authority._secure_regular_files = blocking_scan  # type: ignore[method-assign]
    both_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    entered = 0

    async def run_task(task_id: str, release: asyncio.Event) -> None:
        nonlocal entered, first_root
        async with authority.delegated_output_scope(task_id) as capture:
            if task_id == "first":
                first_root = capture.output_root
            sandbox.files[f"{capture.output_root}/{task_id}.md"] = task_id.encode()
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()

    first = asyncio.create_task(run_task("first", release_first))
    second = asyncio.create_task(run_task("second", release_second))
    await both_entered.wait()
    release_first.set()
    assert await asyncio.to_thread(first_scan_started.wait, 1)

    release_second.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second.cancel()
    assert not second.done()

    allow_first_scan.set()
    await first
    with pytest.raises(asyncio.CancelledError):
        await second

    assert authority._active_delegated_output_roots == set()
    await authority.mark_failed()
    assert finalizer.mark_failed_calls == 1
    assert not any(path.startswith("/mnt/user-data/workspace/.deerflow/subagents/") for path in sandbox.files)


def test_file_finalization_excludes_only_the_runtime_owned_scratch_root() -> None:
    sandbox = _MemorySandbox(
        {
            "/mnt/user-data/workspace/keep.py": b"print('keep')\n",
            "/mnt/user-data/workspace/.deerflow/subagents/capture/outputs/tmp.md": (b"delegated\n"),
        }
    )
    authority = _authority(sandbox)

    scanned = PrivateFileFinalizer(object())._scan(
        authority._run_scope,
        sandbox,
    )

    assert [item.logical_path for item in scanned] == ["workspace/keep.py"]


@pytest.mark.asyncio
async def test_private_task_without_delegated_output_scope_fails_before_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_run = MagicMock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        SimpleNamespace(run=lifecycle_run),
    )
    monkeypatch.setattr(
        task_module,
        "get_stream_writer",
        lambda: lambda _event: None,
    )
    monkeypatch.setattr(
        task_module,
        "get_available_subagent_names",
        lambda **_kwargs: ["general-purpose"],
    )
    monkeypatch.setattr(
        task_module,
        "get_subagent_config",
        lambda *_args, **_kwargs: SubagentConfig(
            name="general-purpose",
            description="output isolation probe",
            model="inherit",
            timeout_seconds=2,
        ),
    )
    factory = ParentExecutionBindingFactory(
        PrivateRunParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=SimpleNamespace(),
            asset_context=None,
            private_runtime=object(),
            model_name="private-model",
            thinking_enabled=False,
            reasoning_effort=None,
            runtime_skills=(),
            runtime_agent_catalog=None,
            tool_groups=(),
        )
    )
    runtime = SimpleNamespace(
        state={},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
            RuntimeContextKeys.PRIVATE_SCOPE: object(),
            RuntimeContextKeys.FILE_AUTHORITY: SimpleNamespace(),
        },
        config={
            "metadata": {},
            "callbacks": [],
            "configurable": {},
        },
        store=None,
    )

    command = await task_module.task_tool.coroutine(
        runtime=runtime,
        description="reject missing output isolation",
        prompt="do not run",
        subagent_type="general-purpose",
        tool_call_id="call-missing-output-isolation",
    )

    messages = command.update["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, ToolMessage)
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert message.additional_kwargs["subagent_error"] == ("SUBAGENT_EXECUTION_FAILED")
    lifecycle_run.assert_not_called()
