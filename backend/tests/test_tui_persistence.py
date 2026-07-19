"""Tests for explicitly project-scoped TUI Thread metadata persistence."""

import uuid

import pytest

from deerflow.private_scope import PrivateResourceScope
from deerflow.tui.persistence import ThreadMetaWriter, _LoopThread


class _Store:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, PrivateResourceScope]] = []

    async def get(self, thread_id: str, *, scope: PrivateResourceScope):
        self.calls.append(("get", scope))
        return self.rows.get(thread_id)

    async def create(self, thread_id: str, **kwargs):
        scope = kwargs["scope"]
        self.calls.append(("create", scope))
        self.rows[thread_id] = {
            "thread_id": thread_id,
            "display_name": None,
            "scope": scope,
            "agent_asset_id": kwargs["agent_asset_id"],
            "agent_scope": kwargs["agent_scope"],
        }
        return self.rows[thread_id]

    async def update_display_name(
        self,
        thread_id: str,
        title: str,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        self.calls.append(("update_display_name", scope))
        self.rows[thread_id]["display_name"] = title


@pytest.fixture
def scoped_writer():
    loop = _LoopThread()
    store = _Store()
    scope = PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )
    agent_asset_id = uuid.uuid4()
    writer = ThreadMetaWriter(
        loop,
        store,
        scope=scope,
        agent_asset_id=agent_asset_id,
        agent_scope="project",
    )
    try:
        yield writer, store, scope, agent_asset_id
    finally:
        loop.close()


def test_writer_rejects_missing_project_authority() -> None:
    loop = _LoopThread()
    try:
        with pytest.raises(TypeError, match="PrivateResourceScope"):
            ThreadMetaWriter(
                loop,
                _Store(),
                scope=None,
                agent_asset_id=uuid.uuid4(),
                agent_scope="project",
            )
    finally:
        loop.close()


def test_explicit_scope_create_and_title_use_same_authority(scoped_writer) -> None:
    writer, store, scope, agent_asset_id = scoped_writer
    writer.ensure_created("th-1", assistant_id="lead-agent")
    writer.ensure_created("th-1")
    writer.set_title("th-1", "Project-scoped title")

    assert writer.enabled is True
    assert writer.user_id == scope.owner_user_id
    assert store.rows["th-1"]["display_name"] == "Project-scoped title"
    assert store.rows["th-1"]["scope"] == scope
    assert store.rows["th-1"]["agent_asset_id"] == agent_asset_id
    assert {call_scope for _, call_scope in store.calls} == {scope}
    assert [name for name, _ in store.calls].count("create") == 1
