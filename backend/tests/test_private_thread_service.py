from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


def _service(seed: M4ThreadSeed, raw=None, *, branch_copy_hook=None):
    from app.private_work.checkpointer import ProjectScopedCheckpointer
    from app.private_work.thread_service import PrivateThreadService

    raw_saver = raw or InMemorySaver()
    scoped = ProjectScopedCheckpointer(raw_saver, seed.factory)
    return (
        PrivateThreadService(
            seed.factory,
            scoped,
            branch_copy_hook=branch_copy_hook,
        ),
        raw_saver,
        scoped,
    )


def _checkpoint_with_messages(*messages, version: str = "messages-v1"):
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["messages"] = list(messages)
    checkpoint["channel_versions"]["messages"] = version
    return checkpoint


async def _root_checkpoint_id(raw, thread_id: str) -> str:
    items = [
        item
        async for item in raw.alist(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                }
            }
        )
    ]
    assert items
    configurable = items[-1].config["configurable"]
    checkpoint_id = configurable.get("checkpoint_id")
    assert isinstance(checkpoint_id, str) and checkpoint_id
    return checkpoint_id


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_search_patch_and_delete(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    service, raw, _scoped = _service(seed)
    created = await service.create(
        seed.owner_a,
        thread_id="service-thread",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
        display_name="Service Thread",
        metadata={"topic": "private"},
    )
    assert created.version == 1
    assert created.agent_asset_id == seed.project_agent_id
    assert [item.thread_id for item in await service.search(seed.owner_a)] == ["service-thread"]
    assert await service.get(seed.owner_b, "service-thread") is None

    raw_tuple = await raw.aget_tuple({"configurable": {"thread_id": "service-thread", "checkpoint_ns": ""}})
    assert raw_tuple is not None
    assert raw_tuple.metadata["deerflow_private_scope"] == {
        "project_id": str(seed.owner_a.project_id),
        "owner_user_id": str(seed.owner_a.user_id),
    }

    patched = await service.patch(
        seed.owner_a,
        "service-thread",
        expected_version=created.version,
        display_name="Renamed Thread",
    )
    assert patched.display_name == "Renamed Thread"
    with pytest.raises(PrivateWorkConflict):
        await service.patch(
            seed.owner_a,
            "service-thread",
            expected_version=created.version,
            display_name="Stale",
        )

    await service.delete(
        seed.owner_a,
        "service-thread",
        expected_version=patched.version,
    )
    assert await service.get(seed.owner_a, "service-thread") is None
    async with seed.engine.connect() as connection:
        status = await connection.scalar(
            text(
                """SELECT checkpoint_delete_status FROM threads_meta
                WHERE thread_id='service-thread'"""
            )
        )
    assert status == "complete"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_requires_capability_and_executable_agent(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import (
        PrivateThreadRepository,
        ThreadAgentRef,
    )

    service, _raw, _scoped = _service(seed)
    with pytest.raises(PrivateWorkForbidden):
        await service.create(
            seed.viewer,
            thread_id="viewer-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.factory() as session:
        async with session.begin():
            viewer_thread = await PrivateThreadRepository(session).create(
                scope=seed.viewer.resource_scope,
                thread_id="viewer-owned-thread",
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
    await service.delete(
        seed.viewer,
        viewer_thread.thread_id,
        expected_version=viewer_thread.version,
    )
    assert await service.get(seed.viewer, viewer_thread.thread_id) is None

    owner_thread = await service.create(
        seed.owner_a,
        thread_id="owner-thread-hidden-from-viewer",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    with pytest.raises(PrivateWorkNotFound):
        await service.delete(
            seed.viewer,
            owner_thread.thread_id,
            expected_version=owner_thread.version,
        )

    with pytest.raises(PrivateWorkNotFound):
        await service.create(
            seed.owner_a,
            thread_id="wrong-project-agent-thread",
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )

    system_thread = await service.create(
        seed.owner_a,
        thread_id="system-agent-thread",
        agent=ThreadAgentRef(seed.system_agent_id, "system"),
    )
    assert system_thread.agent_scope == "system"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_without_explicit_agent_uses_builtin_main(
    seed: M4ThreadSeed,
) -> None:
    service, _raw, _scoped = _service(seed)

    created = await service.create(
        seed.owner_a,
        thread_id="default-main-thread",
        agent=None,
    )

    assert created.agent_asset_id == seed.system_agent_id
    assert created.agent_scope == "system"


@pytest.mark.asyncio
async def test_private_thread_service_builtin_main_fallback_resolves_complete_dependency_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.private_work.context import PrivateWorkContext
    from app.private_work.thread_service import PrivateThreadService
    from app.projects.capabilities import capabilities_for
    from app.projects.context import ProjectContext
    from app.projects.models import ProjectRole
    from app.shared_assets.models import (
        AgentPayload,
        AssetKind,
        AssetScope,
        AssetSelection,
        ResolvedAgentSnapshot,
    )

    main_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.RUNNER,
        capabilities=capabilities_for(ProjectRole.RUNNER),
        membership_version=1,
        request_id="req-main-closure",
    )
    context = PrivateWorkContext.from_project(actor)
    resolved = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.SYSTEM,
        asset_id=main_id,
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(uuid.uuid4(),),
        payload=AgentPayload(
            description="Main",
            soul="Main",
            model_ref="test-model",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )
    session = AsyncMock(spec=AsyncSession)
    query_result = Mock()
    query_result.scalar_one_or_none.return_value = main_id
    session.execute.return_value = query_result
    asset_resolver = SimpleNamespace(
        resolve_project_asset_snapshot_in_session=AsyncMock(
            return_value=resolved,
        )
    )
    default_agent_service = SimpleNamespace(resolve_configured_agent_in_session=AsyncMock(return_value=None))
    service = object.__new__(PrivateThreadService)
    service._asset_resolver = asset_resolver
    service._default_agent_service = default_agent_service
    executable_check = AsyncMock()
    monkeypatch.setattr(
        "app.private_work.thread_service.require_executable_agent",
        executable_check,
    )

    selected = await service._resolve_default_agent(session, context, actor)

    assert selected.asset_id == main_id
    assert selected.scope == "system"
    asset_resolver.resolve_project_asset_snapshot_in_session.assert_awaited_once_with(
        session,
        actor,
        AssetSelection(AssetKind.AGENT, main_id),
    )
    executable_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_builtin_main_is_executable_without_a_project_system_agent_binding() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.private_work.context import PrivateWorkContext
    from app.private_work.executable_agent import require_executable_agent
    from app.private_work.thread_repository import ThreadAgentRef
    from app.projects.capabilities import capabilities_for
    from app.projects.context import ProjectContext
    from app.projects.models import ProjectRole

    main_id = uuid.uuid4()
    version_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.RUNNER,
        capabilities=capabilities_for(ProjectRole.RUNNER),
        membership_version=1,
        request_id="req-main-no-binding",
    )
    context = PrivateWorkContext.from_project(actor)
    asset_result = Mock()
    asset_result.scalar_one_or_none.return_value = SimpleNamespace(
        id=main_id,
        source_key="builtin:agent:project-assistant",
        current_published_version_id=version_id,
    )
    version_result = Mock()
    version_result.scalar_one_or_none.return_value = version_id
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [asset_result, version_result]

    await require_executable_agent(
        session,
        context,
        ThreadAgentRef(main_id, "system"),
    )

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_private_thread_service_builtin_main_resolution_failure_is_stable_conflict() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.private_work.context import PrivateWorkContext
    from app.private_work.errors import PrivateWorkDefaultAgentUnavailable
    from app.private_work.thread_service import PrivateThreadService
    from app.projects.capabilities import capabilities_for
    from app.projects.context import ProjectContext
    from app.projects.models import ProjectRole
    from app.shared_assets.errors import AssetResolutionUnavailable

    main_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.RUNNER,
        capabilities=capabilities_for(ProjectRole.RUNNER),
        membership_version=1,
        request_id="req-main-unavailable",
    )
    context = PrivateWorkContext.from_project(actor)
    session = AsyncMock(spec=AsyncSession)
    query_result = Mock()
    query_result.scalar_one_or_none.return_value = main_id
    session.execute.return_value = query_result
    service = object.__new__(PrivateThreadService)
    service._default_agent_service = SimpleNamespace(resolve_configured_agent_in_session=AsyncMock(return_value=None))
    service._asset_resolver = SimpleNamespace(
        resolve_project_asset_snapshot_in_session=AsyncMock(
            side_effect=AssetResolutionUnavailable(actor.request_id),
        )
    )

    with pytest.raises(PrivateWorkDefaultAgentUnavailable) as captured:
        await service._resolve_default_agent(session, context, actor)

    assert captured.value.code == "DEFAULT_AGENT_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_create_uses_configured_project_default_and_explicit_override(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef
    from deerflow.persistence.projects import ProjectDefaultAgentRow

    async with seed.factory() as session:
        async with session.begin():
            session.add(
                ProjectDefaultAgentRow(
                    project_id=seed.owner_a.project_id,
                    agent_asset_id=seed.project_agent_id,
                    revision=1,
                    created_by_user_id=str(seed.owner_a.user_id),
                    updated_by_user_id=str(seed.owner_a.user_id),
                )
            )

    service, _raw, _scoped = _service(seed)
    default_thread = await service.create(
        seed.owner_a,
        thread_id="configured-default-thread",
        agent=None,
    )
    explicit_thread = await service.create(
        seed.owner_a,
        thread_id="explicit-override-thread",
        agent=ThreadAgentRef(seed.system_agent_id, "system"),
    )

    assert default_thread.agent_asset_id == seed.project_agent_id
    assert default_thread.agent_scope == "project"
    assert explicit_thread.agent_asset_id == seed.system_agent_id
    assert explicit_thread.agent_scope == "system"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_configured_default_fails_closed_when_agent_becomes_unavailable(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.errors import PrivateWorkDefaultAgentUnavailable
    from deerflow.persistence.projects import ProjectDefaultAgentRow
    from deerflow.persistence.shared_assets import AgentRow

    async with seed.factory() as session:
        async with session.begin():
            session.add(
                ProjectDefaultAgentRow(
                    project_id=seed.owner_a.project_id,
                    agent_asset_id=seed.project_agent_id,
                    revision=1,
                    created_by_user_id=str(seed.owner_a.user_id),
                    updated_by_user_id=str(seed.owner_a.user_id),
                )
            )
            agent = await session.get(AgentRow, seed.project_agent_id)
            assert agent is not None
            agent.status = "suspended"

    service, _raw, _scoped = _service(seed)
    with pytest.raises(PrivateWorkDefaultAgentUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="unavailable-default-thread",
            agent=None,
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_builtin_main_fallback_does_not_require_binding(
    seed: M4ThreadSeed,
) -> None:
    from sqlalchemy import update

    from app.private_work.thread_service import _BUILTIN_MAIN_AGENT_SOURCE_KEY
    from deerflow.persistence.shared_assets import ProjectSystemAgentBindingRow
    from deerflow.persistence.shared_assets.agent_model import AgentRow

    async with seed.factory() as session:
        async with session.begin():
            binding = await session.get(
                ProjectSystemAgentBindingRow,
                (seed.owner_a.project_id, seed.system_agent_id),
            )
            assert binding is not None
            binding.enabled = False
            await session.execute(update(AgentRow).where(AgentRow.id == seed.system_agent_id).values(source_key=_BUILTIN_MAIN_AGENT_SOURCE_KEY))

    service, _raw, _scoped = _service(seed)
    created = await service.create(
        seed.owner_a,
        thread_id="available-main-thread",
        agent=None,
    )

    assert created.agent_asset_id == seed.system_agent_id
    assert created.agent_scope == "system"


class _FailingRootSaver(InMemorySaver):
    async def aput(self, *_args, **_kwargs):
        raise RuntimeError("root checkpoint unavailable")


class _WriteThenRaiseSaver(InMemorySaver):
    def __init__(self, *, cleanup_fails: bool = False) -> None:
        super().__init__()
        self.cleanup_fails = cleanup_fails

    async def aput(self, *args, **kwargs):
        await super().aput(*args, **kwargs)
        raise RuntimeError("checkpoint commit result was ambiguous")

    async def adelete_thread(self, thread_id: str) -> None:
        if self.cleanup_fails:
            raise RuntimeError("checkpoint cleanup unavailable")
        await super().adelete_thread(thread_id)


class _FailingLatestHeadSaver(InMemorySaver):
    failing_thread_id: str | None = None

    async def aget_tuple(self, config):
        configurable = config.get("configurable", {})
        if configurable.get("thread_id") == self.failing_thread_id and not configurable.get("checkpoint_id"):
            raise RuntimeError("latest checkpoint lookup unavailable")
        return await super().aget_tuple(config)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_compensates_row_when_root_checkpoint_fails(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    service, _raw, _scoped = _service(seed, _FailingRootSaver())
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="failed-root-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.engine.connect() as connection:
        count = await connection.scalar(
            text(
                """SELECT count(*) FROM threads_meta
                WHERE thread_id='failed-root-thread'"""
            )
        )
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_cleans_ambiguous_root_checkpoint_before_row(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    raw = _WriteThenRaiseSaver()
    service, _raw, _scoped = _service(seed, raw)
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-root-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    assert await raw.aget_tuple({"configurable": {"thread_id": "ambiguous-root-thread", "checkpoint_ns": ""}}) is None
    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM threads_meta WHERE thread_id='ambiguous-root-thread'")) == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_keeps_retry_tombstone_when_ambiguous_cleanup_fails(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    raw = _WriteThenRaiseSaver(cleanup_fails=True)
    service, _raw, _scoped = _service(seed, raw)
    with pytest.raises(PrivateWorkUnavailable):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-cleanup-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )

    async with seed.engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """SELECT deleted_at, checkpoint_delete_status
                    FROM threads_meta WHERE thread_id='ambiguous-cleanup-thread'"""
                )
            )
        ).one()
    assert row.deleted_at is not None
    assert row.checkpoint_delete_status == "retry_required"

    with pytest.raises(PrivateWorkConflict):
        await service.create(
            seed.owner_a,
            thread_id="ambiguous-cleanup-thread",
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


class _BranchCopyHook:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str]] = []

    async def copy_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
        *,
        session=None,
    ) -> None:
        self.calls.append((scope, source_thread_id, target_thread_id))

    async def rollback_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        return None


class _NoRecursiveScopedRead:
    """Require both branch selections to use the caller's locked DB session."""

    def __init__(self, delegate, source_thread_id: str) -> None:
        self._delegate = delegate
        self._source_thread_id = source_thread_id
        self.public_get_calls = 0
        self.locked_get_configs: list[dict[str, object]] = []

    async def aget_tuple(self, config):
        if config.get("configurable", {}).get("thread_id") == self._source_thread_id:
            self.public_get_calls += 1
            raise AssertionError("branch checkpoint selection escaped the source Thread lock")
        return await self._delegate.aget_tuple(config)

    async def aget_tuple_already_authorized(self, config, *, session):
        self.locked_get_configs.append(config)
        return await self._delegate.aget_tuple_already_authorized(config, session=session)

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


class _FailingBranchCopyHook(_BranchCopyHook):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_calls: list[tuple[object, str, str]] = []

    async def copy_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
        *,
        session=None,
    ) -> None:
        await super().copy_branch_authority(
            scope,
            source_thread_id,
            target_thread_id,
            session=session,
        )
        raise RuntimeError("authority copy failed after a partial copy")

    async def rollback_branch_authority(
        self,
        scope,
        source_thread_id: str,
        target_thread_id: str,
    ) -> None:
        self.rollback_calls.append((scope, source_thread_id, target_thread_id))


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_branch_uses_database_authority_copy_hook_only(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
        display_name="Source",
    )
    source_saver = scoped.for_context(seed.owner_a)
    source_config = await source_saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )
    checkpoint_id = source_config["configurable"]["checkpoint_id"]

    replay_base_checkpoint_id = await _root_checkpoint_id(
        _raw,
        source.thread_id,
    )
    branch_saver = _NoRecursiveScopedRead(
        scoped.for_context(seed.owner_a),
        source.thread_id,
    )
    scoped.for_context = lambda _context: branch_saver

    branch = await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="branch-target",
        checkpoint_id=checkpoint_id,
        expected_source_version=source.version,
        replay_base_checkpoint_id=replay_base_checkpoint_id,
        display_name="Branch",
    )

    assert branch.thread_id == "branch-target"
    assert branch.metadata["branch_parent_thread_id"] == source.thread_id
    assert branch.metadata["workspace_clone_mode"] == "current_thread_authority_copy"
    assert branch.metadata["branch_source_head_checkpoint_id"] == checkpoint_id
    assert branch.metadata["branch_parent_visible_tail_message_id"] == "assistant-tail"
    assert hook.calls == [(seed.owner_a, source.thread_id, branch.thread_id)]
    assert branch_saver.public_get_calls == 0
    assert len(branch_saver.locked_get_configs) == 2
    assert branch_saver.locked_get_configs[0]["configurable"]["checkpoint_id"] == checkpoint_id
    assert branch_saver.locked_get_configs[1]["configurable"]["checkpoint_id"] == replay_base_checkpoint_id
    assert await service.get(seed.owner_a, branch.thread_id) == branch
    target_item = await _raw.aget_tuple({"configurable": {"thread_id": branch.thread_id, "checkpoint_ns": ""}})
    assert [message.id for message in target_item.checkpoint["channel_values"]["messages"]] == ["assistant-tail"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_historical_branch_never_copies_current_authority(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="historical-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    saver = scoped.for_context(seed.owner_a)
    historical = await saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(
            HumanMessage(content="first", id="human-1"),
            AIMessage(content="first answer", id="assistant-1"),
            version="messages-v1",
        ),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )
    latest = await saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(
            HumanMessage(content="first", id="human-1"),
            AIMessage(content="first answer", id="assistant-1"),
            HumanMessage(content="second", id="human-2"),
            AIMessage(content="second answer", id="assistant-2"),
            version="messages-v2",
        ),
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": "messages-v2"},
    )

    branch = await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="historical-branch-target",
        checkpoint_id=historical["configurable"]["checkpoint_id"],
        expected_source_version=source.version,
        replay_base_checkpoint_id=await _root_checkpoint_id(
            _raw,
            source.thread_id,
        ),
    )

    assert hook.calls == []
    assert branch.metadata["workspace_clone_mode"] == "historical_skip"
    assert branch.metadata["branch_source_head_checkpoint_id"] == latest["configurable"]["checkpoint_id"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_metadata_only_head_keeps_visible_assistant_turn_current(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="metadata-head-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    saver = scoped.for_context(seed.owner_a)
    visible_turn = _checkpoint_with_messages(
        HumanMessage(content="question", id="human-visible"),
        AIMessage(content="answer", id="assistant-visible"),
        version="messages-v1",
    )
    selected = await saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        visible_turn,
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )
    head = _checkpoint_with_messages(
        HumanMessage(content="question", id="human-visible"),
        AIMessage(content="answer", id="assistant-visible"),
        version="messages-v2",
    )
    head["channel_values"]["title"] = "Title updated after the answer"
    latest = await saver.aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        head,
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": "messages-v2"},
    )

    branch = await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="metadata-head-branch-target",
        checkpoint_id=selected["configurable"]["checkpoint_id"],
        expected_source_version=source.version,
        replay_base_checkpoint_id=await _root_checkpoint_id(
            _raw,
            source.thread_id,
        ),
    )

    assert hook.calls == [(seed.owner_a, source.thread_id, branch.thread_id)]
    assert branch.metadata["workspace_clone_mode"] == "current_thread_authority_copy"
    assert branch.metadata["branch_parent_visible_tail_message_id"] == "assistant-visible"
    assert branch.metadata["branch_source_head_checkpoint_id"] == latest["configurable"]["checkpoint_id"]


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("run_status", "finalization_status"),
    (("running", "pending"), ("success", "finalizing")),
)
async def test_private_thread_branch_rejects_active_or_finalizing_source_run(
    seed: M4ThreadSeed,
    run_status: str,
    finalization_status: str,
) -> None:
    from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id=f"incomplete-run-source-{run_status}",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    selected = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )
    async with seed.factory() as session, session.begin():
        run = await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=source.thread_id,
            request=PrivateRunCreate(status=run_status),
        )
        await session.execute(
            text(
                """UPDATE runs SET finalization_status=:finalization_status
                WHERE run_id=:run_id"""
            ),
            {
                "run_id": run.run_id,
                "finalization_status": finalization_status,
            },
        )

    target_thread_id = f"incomplete-run-target-{run_status}"
    with pytest.raises(PrivateWorkConflict):
        await service.branch(
            seed.owner_a,
            source_thread_id=source.thread_id,
            target_thread_id=target_thread_id,
            checkpoint_id=selected["configurable"]["checkpoint_id"],
            expected_source_version=source.version,
            replay_base_checkpoint_id=await _root_checkpoint_id(
                _raw,
                source.thread_id,
            ),
        )

    assert hook.calls == []
    assert await service.get(seed.owner_a, target_thread_id) is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_latest_lookup_failure_fails_closed_to_historical_skip(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _BranchCopyHook()
    failing_saver = _FailingLatestHeadSaver()
    service, _raw, scoped = _service(
        seed,
        raw=failing_saver,
        branch_copy_hook=hook,
    )
    source = await service.create(
        seed.owner_a,
        thread_id="head-lookup-failure-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    failing_saver.failing_thread_id = source.thread_id
    checkpoint = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    branch = await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="head-lookup-failure-target",
        checkpoint_id=checkpoint["configurable"]["checkpoint_id"],
        expected_source_version=source.version,
        replay_base_checkpoint_id=await _root_checkpoint_id(
            _raw,
            source.thread_id,
        ),
    )

    assert branch.metadata["workspace_clone_mode"] == "historical_skip"
    assert branch.metadata["branch_source_head_checkpoint_id"] is None
    assert hook.calls == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_latest_selection_and_copy_share_source_thread_lock(
    seed: M4ThreadSeed,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from app.private_work.thread_repository import ThreadAgentRef

    class LockCheckingHook(_BranchCopyHook):
        async def copy_branch_authority(
            self,
            scope,
            source_thread_id: str,
            target_thread_id: str,
            *,
            session=None,
        ) -> None:
            assert session is not None
            assert session.in_transaction()
            with pytest.raises(DBAPIError):
                async with seed.engine.begin() as connection:
                    await connection.execute(
                        text(
                            """SELECT thread_id FROM threads_meta
                            WHERE project_id=:project_id AND owner_user_id=:owner
                              AND thread_id=:thread_id FOR UPDATE NOWAIT"""
                        ),
                        {
                            "project_id": seed.owner_a.project_id,
                            "owner": seed.owner_a_scope.owner_user_id,
                            "thread_id": source_thread_id,
                        },
                    )
            await super().copy_branch_authority(
                scope,
                source_thread_id,
                target_thread_id,
                session=session,
            )

    hook = LockCheckingHook()
    service, _raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="locked-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    latest = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    await service.branch(
        seed.owner_a,
        source_thread_id=source.thread_id,
        target_thread_id="locked-branch-target",
        checkpoint_id=latest["configurable"]["checkpoint_id"],
        expected_source_version=source.version,
        replay_base_checkpoint_id=await _root_checkpoint_id(
            _raw,
            source.thread_id,
        ),
    )

    assert len(hook.calls) == 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_service_branch_rolls_back_checkpoint_and_authority_hook(
    seed: M4ThreadSeed,
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    hook = _FailingBranchCopyHook()
    service, raw, scoped = _service(seed, branch_copy_hook=hook)
    source = await service.create(
        seed.owner_a,
        thread_id="failed-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    source_config = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    with pytest.raises(PrivateWorkUnavailable):
        await service.branch(
            seed.owner_a,
            source_thread_id=source.thread_id,
            target_thread_id="failed-branch-target",
            checkpoint_id=source_config["configurable"]["checkpoint_id"],
            expected_source_version=source.version,
            replay_base_checkpoint_id=await _root_checkpoint_id(
                raw,
                source.thread_id,
            ),
        )

    assert hook.rollback_calls == []
    assert await raw.aget_tuple({"configurable": {"thread_id": "failed-branch-target", "checkpoint_ns": ""}}) is None
    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM threads_meta WHERE thread_id='failed-branch-target'")) == 0


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_private_thread_branch_db_failure_does_not_delete_existing_target_files(
    seed: M4ThreadSeed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from app.private_work.file_service import PrivateFileService
    from app.private_work.thread_repository import ThreadAgentRef

    async def chunks():
        yield b"existing target authority"

    file_service = PrivateFileService(seed.factory)
    service, _raw, scoped = _service(seed, branch_copy_hook=file_service)
    source = await service.create(
        seed.owner_a,
        thread_id="db-failure-branch-source",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    await service.create(
        seed.owner_a,
        thread_id="db-failure-existing-target",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    existing = await file_service.upload(
        seed.owner_a,
        thread_id="db-failure-existing-target",
        logical_path="workspace/sentinel.txt",
        media_type="text/plain",
        chunks=chunks(),
    )
    selected = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    async def fail_agent_lookup(*_args, **_kwargs):
        raise DBAPIError("SELECT agents", {}, RuntimeError("database unavailable"), False)

    monkeypatch.setattr(service, "_require_executable_agent", fail_agent_lookup)

    with pytest.raises(PrivateWorkUnavailable):
        await service.branch(
            seed.owner_a,
            source_thread_id=source.thread_id,
            target_thread_id="db-failure-existing-target",
            checkpoint_id=selected["configurable"]["checkpoint_id"],
            expected_source_version=source.version,
            replay_base_checkpoint_id=await _root_checkpoint_id(
                _raw,
                source.thread_id,
            ),
        )

    ready = await file_service.list_ready(
        seed.owner_a,
        thread_id="db-failure-existing-target",
    )
    assert [(item.id, item.logical_path, item.sha256) for item in ready] == [(existing.id, existing.logical_path, existing.sha256)]


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("target_context", "target_agent", "expected_error"),
    (
        ("same", "project_a", PrivateWorkConflict),
        ("cross", "project_b", PrivateWorkNotFound),
    ),
)
async def test_private_thread_branch_target_collision_hides_cross_scope_existence(
    seed: M4ThreadSeed,
    target_context: str,
    target_agent: str,
    expected_error: type[Exception],
) -> None:
    from app.private_work.thread_repository import ThreadAgentRef

    service, _raw, scoped = _service(seed)
    source = await service.create(
        seed.owner_a,
        thread_id=f"collision-source-{target_context}",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    target_id = f"collision-target-{target_context}"
    target_owner = seed.owner_a if target_context == "same" else seed.project_b_owner_a
    target_agent_id = seed.project_agent_id if target_agent == "project_a" else seed.project_b_agent_id
    await service.create(
        target_owner,
        thread_id=target_id,
        agent=ThreadAgentRef(target_agent_id, "project"),
    )
    selected = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    with pytest.raises(expected_error):
        await service.branch(
            seed.owner_a,
            source_thread_id=source.thread_id,
            target_thread_id=target_id,
            checkpoint_id=selected["configurable"]["checkpoint_id"],
            expected_source_version=source.version,
            replay_base_checkpoint_id=await _root_checkpoint_id(
                _raw,
                source.thread_id,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("target_context", "target_agent", "expected_error"),
    (
        ("same", "project_a", PrivateWorkConflict),
        ("cross", "project_b", PrivateWorkNotFound),
    ),
)
async def test_private_thread_branch_savepoint_loser_rechecks_collision_scope(
    seed: M4ThreadSeed,
    target_context: str,
    target_agent: str,
    expected_error: type[Exception],
) -> None:
    from sqlalchemy import event

    from app.private_work.thread_repository import ThreadAgentRef
    from app.private_work.thread_service import PrivateThreadService

    normal_service, _raw, scoped = _service(seed)
    source = await normal_service.create(
        seed.owner_a,
        thread_id=f"savepoint-source-{target_context}",
        agent=ThreadAgentRef(seed.project_agent_id, "project"),
    )
    target_id = f"savepoint-target-{target_context}"
    target_owner = seed.owner_a if target_context == "same" else seed.project_b_owner_a
    target_agent_id = seed.project_agent_id if target_agent == "project_a" else seed.project_b_agent_id
    await normal_service.create(
        target_owner,
        thread_id=target_id,
        agent=ThreadAgentRef(target_agent_id, "project"),
    )
    selected = await scoped.for_context(seed.owner_a).aput(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_ns": ""}},
        _checkpoint_with_messages(AIMessage(content="done", id="assistant-tail")),
        {"source": "loop", "step": 0, "parents": {}},
        {"messages": "messages-v1"},
    )

    class StaleTargetPreflightService(PrivateThreadService):
        target_checks = 0

        async def _raise_if_branch_target_exists(self, session, context, target_thread_id):
            self.target_checks += 1
            if self.target_checks == 1:
                return
            await super()._raise_if_branch_target_exists(
                session,
                context,
                target_thread_id,
            )

    service = StaleTargetPreflightService(seed.factory, scoped)
    unique_violations: list[str | None] = []

    def capture_integrity_error(exception_context) -> None:
        statement = exception_context.statement or ""
        if "INSERT INTO threads_meta" in statement:
            unique_violations.append(getattr(exception_context.original_exception, "sqlstate", None))

    event.listen(seed.engine.sync_engine, "handle_error", capture_integrity_error)
    try:
        with pytest.raises(expected_error):
            await service.branch(
                seed.owner_a,
                source_thread_id=source.thread_id,
                target_thread_id=target_id,
                checkpoint_id=selected["configurable"]["checkpoint_id"],
                expected_source_version=source.version,
                replay_base_checkpoint_id=await _root_checkpoint_id(
                    _raw,
                    source.thread_id,
                ),
            )
    finally:
        event.remove(seed.engine.sync_engine, "handle_error", capture_integrity_error)

    assert service.target_checks == 2
    assert unique_violations == ["23505"]
    async with seed.engine.connect() as connection:
        coordinates = (
            await connection.execute(
                text(
                    """SELECT project_id,owner_user_id FROM threads_meta
                    WHERE thread_id=:thread_id"""
                ),
                {"thread_id": target_id},
            )
        ).one()
    assert coordinates == (target_owner.project_id, str(target_owner.user_id))


def test_private_thread_service_does_not_read_host_thread_directories() -> None:
    from app.private_work.thread_service import (
        BranchAuthorityCopyHook,
        BranchCheckpointSelection,
        PrivateThreadService,
    )

    source = inspect.getsource(PrivateThreadService)
    assert "get_paths" not in source
    assert "shutil" not in source
    assert "sandbox_user_data_dir" not in source
    assert inspect.iscoroutinefunction(BranchAuthorityCopyHook.rollback_branch_authority)
    assert not hasattr(BranchCheckpointSelection, "rollback_branch_authority")
