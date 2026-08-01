from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _AsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback

    def begin(self):
        return self


@pytest.mark.asyncio
async def test_private_run_title_store_revalidates_before_persisting(monkeypatch) -> None:
    import app.reliability.execution as execution

    repository = SimpleNamespace(
        set_automatic_display_name=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        execution,
        "PrivateThreadRepository",
        lambda _session: repository,
    )
    boundary = SimpleNamespace(before_checkpoint_write=AsyncMock())
    scope = SimpleNamespace(
        project_id="11111111-1111-4111-8111-111111111111",
        owner_user_id="22222222-2222-4222-8222-222222222222",
    )
    store = execution._PrivateRunThreadMetadataStore(
        lambda: _AsyncSession(),
        scope=scope,
        boundary=boundary,
    )

    await store.update_display_name("thread-1", "首轮自动标题")

    boundary.before_checkpoint_write.assert_awaited_once_with()
    repository.set_automatic_display_name.assert_awaited_once_with(
        scope=scope,
        thread_id="thread-1",
        display_name="首轮自动标题",
    )


@pytest.mark.asyncio
async def test_automatic_title_update_only_targets_placeholder_names() -> None:
    from app.private_work.thread_repository import PrivateThreadRepository
    from deerflow.runtime.private_scope import PrivateResourceScope

    result = SimpleNamespace(scalar_one_or_none=lambda: "thread-1")
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    repository = PrivateThreadRepository(session)
    updated = await repository.set_automatic_display_name(
        scope=PrivateResourceScope(
            project_id="11111111-1111-4111-8111-111111111111",
            owner_user_id="22222222-2222-4222-8222-222222222222",
            membership_version=1,
        ),
        thread_id="thread-1",
        display_name="  首轮自动标题  ",
    )

    assert updated is True
    statement = session.execute.await_args.args[0]
    compiled = str(
        statement.compile(
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "threads_meta.display_name IS NULL" in compiled
    assert "threads_meta.display_name IN" in compiled
    assert "threads_meta.version = 1" in compiled
    assert "新对话" in compiled
    assert "version=" not in compiled


def test_auto_title_only_syncs_after_a_successful_complete_run() -> None:
    import deerflow.runtime.runs.worker as worker_module
    from app.reliability.execution import RunAgentPrivateExecutor
    from deerflow.runtime.runs.worker import run_agent

    executor_source = inspect.getsource(RunAgentPrivateExecutor.execute)
    traced_executor_source = inspect.getsource(
        RunAgentPrivateExecutor._execute_with_trace,
    )
    source = inspect.getsource(run_agent)
    module_source = inspect.getsource(worker_module)

    assert "return await self._execute_with_trace(" in executor_source
    assert "thread_store=_PrivateRunThreadMetadataStore(" in traced_executor_source
    assert "record.status is RunStatus.success" in source
    assert "_ensure_interrupted_title" not in module_source


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_persist_an_automatic_title() -> None:
    from deerflow.runtime.private_scope import PrivateResourceScope
    from deerflow.runtime.runs.manager import RunManager
    from deerflow.runtime.runs.schemas import RunStatus
    from deerflow.runtime.runs.worker import RunContext, run_agent

    run_manager = RunManager()
    record = await run_manager.register_persisted(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="project-agent",
        model_name="test-model",
        scope=PrivateResourceScope(
            project_id="project-1",
            owner_user_id="owner-1",
            membership_version=1,
        ),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = SimpleNamespace(
        aget_tuple=AsyncMock(
            return_value=SimpleNamespace(
                checkpoint={
                    "channel_values": {
                        "title": "不应保存的标题",
                    }
                }
            )
        )
    )
    thread_store = SimpleNamespace(
        update_display_name=AsyncMock(),
        update_status=AsyncMock(),
    )

    class Runtime:
        async def aclose(self) -> None:
            raise RuntimeError("cleanup failed")

    class Agent:
        metadata = {"model_name": "test-model"}

        async def astream(self, *_args, **_kwargs):
            yield {"messages": []}

    def agent_factory(*, config, private_runtime):
        del config, private_runtime
        return Agent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=checkpointer,
            thread_store=thread_store,
            private_agent_runtime=Runtime(),
        ),
        agent_factory=agent_factory,
        graph_input={},
        config={},
    )

    assert record.status is RunStatus.error
    assert record.error == "Private run cleanup failed"
    thread_store.update_display_name.assert_not_awaited()
