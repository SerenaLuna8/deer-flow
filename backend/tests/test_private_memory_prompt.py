from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import PrivateWorkForbidden
from app.private_work.memory_service import PrivateMemoryService
from deerflow.agents.memory.storage import ProjectMemoryStorage, create_empty_memory
from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from deerflow.config.memory_config import MemoryConfig
from deerflow.runtime.private_scope import PrivateResourceScope


class _Membership:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.seen: list[PrivateResourceScope] = []

    async def is_active(self, scope: PrivateResourceScope) -> bool:
        self.seen.append(scope)
        return self.active


class _ForbiddenStorage:
    async def load(self, **_kwargs):
        raise AssertionError("inactive project membership must not read memory")


@pytest_asyncio.fixture()
async def private_memory_runtime(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield seed
    finally:
        await seed.engine.dispose()


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        memory=MemoryConfig(
            enabled=True,
            injection_enabled=True,
            token_counting="char",
        )
    )


def _memory(summary: str, *, fact: str | None = None) -> dict:
    memory = create_empty_memory()
    memory["user"]["workContext"] = {
        "summary": summary,
        "updatedAt": "2026-07-15T09:00:00Z",
    }
    if fact:
        memory["facts"] = [
            {
                "content": fact,
                "category": "preference",
                "confidence": 0.9,
                "source": "manual",
            }
        ]
    return memory


async def _save(storage: ProjectMemoryStorage, scope: PrivateResourceScope, namespace: str, memory: dict) -> None:
    current = await storage.load(scope=scope, namespace=namespace)
    await storage.save(
        memory,
        scope=scope,
        namespace=namespace,
        expected_version=current.version,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_prompt_injects_only_selected_project_owner_namespace(private_memory_runtime: M4ThreadSeed) -> None:
    seed = private_memory_runtime
    storage = ProjectMemoryStorage(seed.factory)
    namespace = "agent:researcher"
    await _save(storage, seed.owner_a_scope, namespace, _memory("owner A context", fact="A-only runnable preference"))
    await _save(storage, seed.owner_b_scope, namespace, _memory("owner B secret context", fact="B-only fact"))
    membership = _Membership()
    middleware = DynamicContextMiddleware(
        agent_name="researcher",
        app_config=_config(),
        project_memory_storage=storage,
        project_memory_revalidator=membership,
    )

    with mock.patch(
        "deerflow.agents.lead_agent.prompt._get_memory_context",
        side_effect=AssertionError("private prompt must not fall back to file memory"),
    ):
        result = await middleware.abefore_agent(
            {"messages": [HumanMessage(content="Continue", id="message-a")]},
            Runtime(context={"private_scope": seed.owner_a_scope}),
        )

    assert result is not None
    injected = "\n".join(str(message.content) for message in result["messages"])
    assert "owner A context" in injected
    assert "A-only runnable preference" in injected
    assert "owner B secret context" not in injected
    assert "B-only fact" not in injected
    assert membership.seen == [seed.owner_a_scope]
    assert isinstance(result["messages"][0], SystemMessage)
    assert isinstance(result["messages"][1], HumanMessage)


@pytest.mark.asyncio
async def test_private_prompt_does_not_read_or_inject_memory_for_inactive_membership() -> None:
    scope = PrivateResourceScope("project-a", "owner-a", 7)
    membership = _Membership(active=False)
    middleware = DynamicContextMiddleware(
        agent_name="researcher",
        app_config=_config(),
        project_memory_storage=_ForbiddenStorage(),
        project_memory_revalidator=membership,
    )

    with mock.patch(
        "deerflow.agents.lead_agent.prompt._get_memory_context",
        side_effect=AssertionError("private prompt must not fall back to file memory"),
    ):
        result = await middleware.abefore_agent(
            {"messages": [HumanMessage(content="Continue", id="message-a")]},
            Runtime(context={"private_scope": scope}),
        )

    assert result is not None
    assert membership.seen == [scope]
    assert len(result["messages"]) == 2
    assert all("<memory>" not in str(message.content) for message in result["messages"])


@pytest.mark.asyncio
async def test_legacy_async_prompt_keeps_file_memory_path() -> None:
    middleware = DynamicContextMiddleware(app_config=_config())
    with mock.patch(
        "deerflow.agents.lead_agent.prompt._get_memory_context",
        return_value="<memory>\nlegacy file context\n</memory>",
    ):
        result = await middleware.abefore_agent(
            {"messages": [HumanMessage(content="Continue", id="legacy-message")]},
            Runtime(context={}),
        )

    assert result is not None
    assert "legacy file context" in "\n".join(str(message.content) for message in result["messages"])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_memory_service_crud_and_viewer_boundary(private_memory_runtime: M4ThreadSeed) -> None:
    seed = private_memory_runtime
    service = PrivateMemoryService(seed.factory)

    initial = await service.list(seed.owner_a, namespace="default")
    imported = await service.import_memory(
        seed.owner_a,
        _memory("service owner context", fact="Initial fact"),
        namespace="default",
        expected_version=initial.version,
    )
    fact_id = imported.memory["facts"][0]["id"]
    status = await service.status(seed.owner_a, namespace="default")
    assert status.version == imported.version
    assert status.fact_count == 1

    updated = await service.update(
        seed.owner_a,
        fact_id,
        namespace="default",
        expected_version=imported.version,
        content="Updated fact",
        confidence=0.95,
    )
    assert updated.memory["facts"][0]["content"] == "Updated fact"
    assert (await service.export(seed.owner_a, namespace="default"))["facts"][0]["confidence"] == 0.95

    deleted = await service.delete(
        seed.owner_a,
        fact_id,
        namespace="default",
        expected_version=updated.version,
    )
    assert deleted.memory["facts"] == []
    assert await service.reload(seed.owner_a, namespace="default") == deleted

    viewer_memory = await service.list(seed.viewer, namespace="default")
    assert viewer_memory.memory["facts"] == []
    assert (await service.export(seed.viewer, namespace="default"))["facts"] == []
    with pytest.raises(PrivateWorkForbidden):
        await service.import_memory(
            seed.viewer,
            _memory("viewer must not write"),
            namespace="default",
            expected_version=viewer_memory.version,
        )
