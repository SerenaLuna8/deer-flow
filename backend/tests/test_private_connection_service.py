from __future__ import annotations

import importlib
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.private_work.connection_service import ProjectConnectionService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkForbidden, PrivateWorkNotFound
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.errors import ProjectNotFound
from app.projects.models import ProjectRole


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _Revalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[PrivateWorkContext, Capability]] = []

    async def require(
        self,
        _session: object,
        context: PrivateWorkContext,
        capability: Capability,
        **_kwargs: object,
    ) -> ProjectContext:
        self.calls.append((context, capability))
        if capability not in context.capabilities:
            raise PrivateWorkForbidden(context.request_id)
        return _project_context(
            role=context.role,
            project_id=context.project_id,
            user_id=context.user_id,
            membership_id=context.membership_id,
            membership_version=context.membership_version,
            request_id=context.request_id,
        )


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.consumed: dict[str, object] | None = None

    async def create_oauth_state_within_cap(self, **kwargs: object) -> bool:
        self.calls.append(("create", kwargs))
        return True

    async def consume_oauth_state(self, **kwargs: object) -> dict[str, object] | None:
        self.calls.append(("consume", kwargs))
        return self.consumed

    async def upsert_connection(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("upsert", kwargs))
        return {"id": "connection-1", **kwargs}

    async def list_connections(self, scope: object) -> list[dict[str, object]]:
        self.calls.append(("list", {"scope": scope}))
        return [{"id": "connection-1"}]

    async def disconnect_connection(self, **kwargs: object) -> bool:
        self.calls.append(("disconnect", kwargs))
        return True


def _project_context(
    *,
    role: ProjectRole = ProjectRole.RUNNER,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    membership_id: uuid.UUID | None = None,
    membership_version: int = 7,
    request_id: str = "request-1",
) -> ProjectContext:
    return ProjectContext(
        user_id=user_id or uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        membership_id=membership_id or uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=membership_version,
        request_id=request_id,
    )


def _private_context(**kwargs: object) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(_project_context(**kwargs))


def _service(
    repository: _Repository,
    revalidator: _Revalidator,
    *,
    resolver: object | None = None,
) -> ProjectConnectionService:
    return ProjectConnectionService(
        _SessionFactory(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        revalidator=revalidator,  # type: ignore[arg-type]
        context_resolver=resolver,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        state_factory=lambda: "server-state",
    )


@pytest.mark.anyio
async def test_begin_connect_freezes_scope_and_agent_ref_in_server_state() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    context = _private_context()
    agent_id = uuid.uuid4()

    challenge = await _service(repository, revalidator).begin_connect(
        context,
        "slack",
        agent_id,
        redirect_after="/projects/example/connections",
    )

    assert challenge.state == challenge.code == "server-state"
    method, call = repository.calls[-1]
    assert method == "create"
    assert call["scope"] == context.resource_scope
    assert call["metadata"] == {
        "agent_asset_id": str(agent_id),
        "agent_scope": "project",
        "membership_id": str(context.membership_id),
        "membership_version": context.membership_version,
        "request_id": context.request_id,
    }
    assert call["redirect_after"] == "/projects/example/connections"
    assert revalidator.calls[-1][1] is Capability.PRIVATE_WORK_CREATE


@pytest.mark.anyio
async def test_callback_uses_consumed_state_scope_and_revalidates_membership() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    state_project_id = uuid.uuid4()
    state_owner_id = uuid.uuid4()
    state_membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    repository.consumed = {
        "project_id": str(state_project_id),
        "owner_user_id": str(state_owner_id),
        "provider": "slack",
        "metadata": {
            "agent_asset_id": str(agent_id),
            "agent_scope": "project",
            "membership_id": str(state_membership_id),
            "membership_version": 7,
            "request_id": "state-request",
        },
    }
    resolver_calls: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    async def resolver(
        _session: object,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        request_id: str,
        **_kwargs: object,
    ) -> ProjectContext:
        resolver_calls.append((user_id, project_id, request_id))
        return _project_context(
            user_id=user_id,
            project_id=project_id,
            membership_id=state_membership_id,
            membership_version=7,
            request_id=request_id,
        )

    connection = await _service(
        repository,
        revalidator,
        resolver=resolver,
    ).complete_callback(
        "slack",
        "callback-state",
        "external-account",
        "workspace-1",
        metadata={"provider_value": "kept", "agent_asset_id": "untrusted"},
    )

    assert resolver_calls == [(state_owner_id, state_project_id, "state-request")]
    assert connection["scope"].project_id == str(state_project_id)
    assert connection["scope"].owner_user_id == str(state_owner_id)
    upsert = next(call for method, call in repository.calls if method == "upsert")
    assert upsert["metadata"] == {
        "provider_value": "kept",
        "agent_asset_id": str(agent_id),
        "agent_scope": "project",
    }
    assert revalidator.calls[-1][1] is Capability.PRIVATE_WORK_CREATE


@pytest.mark.anyio
async def test_callback_rejects_inactive_membership_without_upsert() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    repository.consumed = {
        "project_id": str(uuid.uuid4()),
        "owner_user_id": str(uuid.uuid4()),
        "metadata": {
            "membership_id": str(uuid.uuid4()),
            "membership_version": 7,
            "request_id": "state-request",
        },
    }

    async def inactive(*_args: object, **_kwargs: object) -> ProjectContext:
        raise ProjectNotFound()

    with pytest.raises(PrivateWorkNotFound) as raised:
        await _service(repository, revalidator, resolver=inactive).complete_callback("slack", "expired-authority", "external", "workspace")

    assert raised.value.request_id == "state-request"
    assert not any(method == "upsert" for method, _call in repository.calls)


@pytest.mark.anyio
async def test_callback_rejects_changed_membership_version_without_upsert() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    project_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    repository.consumed = {
        "project_id": str(project_id),
        "owner_user_id": str(owner_id),
        "metadata": {
            "membership_id": str(uuid.uuid4()),
            "membership_version": 7,
            "request_id": "state-request",
        },
    }

    async def changed(
        _session: object,
        user_id: uuid.UUID,
        resolved_project_id: uuid.UUID,
        request_id: str,
        **_kwargs: object,
    ) -> ProjectContext:
        return _project_context(
            user_id=user_id,
            project_id=resolved_project_id,
            membership_version=8,
            request_id=request_id,
        )

    with pytest.raises(PrivateWorkNotFound):
        await _service(repository, revalidator, resolver=changed).complete_callback("slack", "stale-authority", "external", "workspace")

    assert not any(method == "upsert" for method, _call in repository.calls)


@pytest.mark.anyio
async def test_list_and_disconnect_pass_the_exact_revalidated_scope() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    context = _private_context()
    service = _service(repository, revalidator)

    assert await service.list(context) == [{"id": "connection-1"}]
    await service.disconnect(context, "connection-1")

    list_call = next(call for method, call in repository.calls if method == "list")
    disconnect_call = next(call for method, call in repository.calls if method == "disconnect")
    assert list_call["scope"] == context.resource_scope
    assert disconnect_call == {
        "scope": context.resource_scope,
        "connection_id": "connection-1",
    }
    assert [capability for _context, capability in revalidator.calls] == [
        Capability.PRIVATE_WORK_READ_OWN,
        Capability.PRIVATE_WORK_CREATE,
    ]


@pytest.mark.anyio
async def test_viewer_can_list_but_cannot_begin_or_disconnect() -> None:
    repository = _Repository()
    revalidator = _Revalidator()
    context = _private_context(role=ProjectRole.VIEWER)
    service = _service(repository, revalidator)

    assert await service.list(context) == [{"id": "connection-1"}]
    with pytest.raises(PrivateWorkForbidden):
        await service.begin_connect(context, "slack", uuid.uuid4())
    with pytest.raises(PrivateWorkForbidden):
        await service.disconnect(context, "connection-1")

    assert [method for method, _call in repository.calls] == ["list"]


@pytest.mark.anyio
async def test_channel_service_injects_project_connection_service(monkeypatch) -> None:
    from app.channels.service import ChannelService

    captured: dict[str, object] = {}

    class FakeChannel:
        def __init__(self, *, bus: object, config: dict[str, object]) -> None:
            del bus
            captured.update(config)
            self.is_running = False

        async def start(self) -> None:
            self.is_running = True

    monkeypatch.setattr(
        "deerflow.reflection.resolve_class",
        lambda *_args, **_kwargs: FakeChannel,
    )
    repository = SimpleNamespace(session_factory=_SessionFactory())
    service = ChannelService(connection_repo=repository)

    assert await service._start_channel("slack", {"enabled": True}) is True
    assert captured["connection_repo"] is repository
    assert isinstance(captured["connection_service"], ProjectConnectionService)


@pytest.mark.anyio
async def test_slack_binding_prefers_project_connection_service() -> None:
    from app.channels.message_bus import MessageBus
    from app.channels.slack import SlackChannel

    repository = AsyncMock()
    connection_service = AsyncMock()
    connection_service.complete_callback.return_value = {
        "id": "connection-1",
        "owner_user_id": str(uuid.uuid4()),
    }
    channel = SlackChannel(
        bus=MessageBus(),
        config={
            "connection_repo": repository,
            "connection_service": connection_service,
        },
    )
    channel._web_client = MagicMock()

    assert await channel._bind_connection_from_connect_code(
        event={"user": "U123", "channel": "C123", "ts": "123.4"},
        team_id="T123",
        code="server-state",
    )
    connection_service.complete_callback.assert_awaited_once_with(
        "slack",
        "server-state",
        "U123",
        "T123",
        metadata={"team_id": "T123", "channel_id": "C123"},
        status="connected",
    )
    repository.consume_oauth_state.assert_not_awaited()
    repository.upsert_connection.assert_not_awaited()


@pytest.mark.parametrize(
    ("module_name", "class_name", "callback_name"),
    [
        ("app.channels.feishu", "FeishuChannel", "_bind_connection_from_connect_code"),
        ("app.channels.slack", "SlackChannel", "_bind_connection_from_connect_code"),
        ("app.channels.telegram", "TelegramChannel", "_bind_connection_from_start_token"),
        ("app.channels.discord", "DiscordChannel", "_bind_connection_from_connect_code"),
        ("app.channels.dingtalk", "DingTalkChannel", "_bind_connection_from_connect_code"),
        ("app.channels.wechat", "WechatChannel", "_bind_connection_from_connect_code"),
        ("app.channels.wecom", "WeComChannel", "_bind_connection_from_connect_code"),
    ],
)
def test_all_provider_binding_callbacks_delegate_to_project_connection_service(
    module_name: str,
    class_name: str,
    callback_name: str,
) -> None:
    channel_type = getattr(importlib.import_module(module_name), class_name)
    source = inspect.getsource(getattr(channel_type, callback_name))

    assert "complete_callback" in source
