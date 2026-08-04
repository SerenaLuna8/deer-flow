from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.channels.base import Channel
from app.channels.connection_identity import attach_connection_identity
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.channels.service import ChannelService
from app.gateway.routers import project_connections
from app.private_work.connection_inbound import ConnectionInboundResolver, ProjectInboundDispatcher, ProviderIdentity
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.run_admission import PrivateRunInboundAuthority
from app.project_channels.runtime import ProjectChannelRuntimeCoordinator


class _RecordingChannel(Channel):
    def __init__(
        self,
        bus: MessageBus,
        config: dict[str, Any] | None = None,
        *,
        channel_instance_id: str | None = None,
    ) -> None:
        resolved_instance_id = channel_instance_id or (config or {}).get("channel_instance_id")
        super().__init__(
            name="feishu",
            bus=bus,
            config={"channel_instance_id": resolved_instance_id},
        )
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


class _FailingStopChannel(_RecordingChannel):
    async def stop(self) -> None:
        raise RuntimeError("provider stop failed")


class _FailedStartAndStopChannel(_RecordingChannel):
    creations = 0

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(bus, config)
        type(self).creations += 1

    async def start(self) -> None:
        raise RuntimeError("provider start failed")

    async def stop(self) -> None:
        raise RuntimeError("provider cleanup failed")


@pytest.mark.anyio
async def test_channel_routes_outbound_to_exact_instance_and_stamps_inbound() -> None:
    bus = MessageBus()
    first = _RecordingChannel(bus, channel_instance_id="instance-a")
    second = _RecordingChannel(bus, channel_instance_id="instance-b")
    await first.start()
    await second.start()

    inbound = first._make_inbound("chat-1", "user-1", "hello")
    await bus.publish_outbound(
        OutboundMessage(
            channel_name="feishu",
            channel_instance_id="instance-a",
            chat_id="chat-1",
            thread_id="thread-1",
            text="reply",
        )
    )

    assert first.channel_instance_id == "instance-a"
    assert inbound.channel_instance_id == "instance-a"
    assert [message.text for message in first.sent] == ["reply"]
    assert second.sent == []


def test_manager_dedupe_identity_contains_channel_instance() -> None:
    first = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
        workspace_id="tenant-1",
        provider_delivery_id="delivery-1",
    )
    second = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-b",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
        workspace_id="tenant-1",
        provider_delivery_id="delivery-1",
    )

    first_key = ChannelManager._inbound_dedupe_key(first)
    second_key = ChannelManager._inbound_dedupe_key(second)

    assert first_key == (
        "instance-a",
        "feishu",
        "tenant-1",
        "chat-1",
        "delivery-1",
    )
    assert second_key != first_key


@pytest.mark.anyio
async def test_connection_identity_lookup_is_scoped_to_channel_instance() -> None:
    calls: list[dict[str, Any]] = []

    class _Repository:
        async def find_connection_by_external_identity(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "id": "connection-1",
                "project_id": str(uuid.uuid4()),
                "owner_user_id": str(uuid.uuid4()),
                "membership_version": 3,
            }

    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )
    await attach_connection_identity(
        inbound,
        repo=_Repository(),
        provider="feishu",
        workspace_id="tenant-1",
    )

    assert calls == [
        {
            "provider": "feishu",
            "channel_instance_id": "instance-a",
            "external_account_id": "user-1",
            "workspace_id": "tenant-1",
        }
    ]


@pytest.mark.anyio
async def test_project_dispatcher_passes_instance_to_resolver_and_authority() -> None:
    identities: list[Any] = []
    launched: list[tuple[Any, ...]] = []
    authority = PrivateRunInboundAuthority(
        connection_id="connection-1",
        channel_instance_id="instance-a",
        provider="feishu",
        external_account_id="user-1",
        workspace_id="tenant-1",
        external_conversation_id="chat-1",
        external_topic_id=None,
    )
    resolved = SimpleNamespace(
        context=object(),
        thread_id="thread-1",
        authority=authority,
    )

    class _Resolver:
        async def resolve(self, identity: Any) -> Any:
            identities.append(identity)
            return resolved

    async def _launch(*args: Any) -> dict[str, Any]:
        launched.append(args)
        return {"messages": []}

    message = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
        workspace_id="tenant-1",
    )
    result = await ProjectInboundDispatcher(_Resolver(), _launch).dispatch(message)

    assert identities[0].channel_instance_id == "instance-a"
    assert launched[0][-1].channel_instance_id == "instance-a"
    assert result.resolved is resolved


def test_private_run_inbound_authority_rejects_empty_instance_id() -> None:
    with pytest.raises(TypeError, match="channel_instance_id"):
        PrivateRunInboundAuthority(
            connection_id="connection-1",
            channel_instance_id="",
            provider="feishu",
            external_account_id="user-1",
            workspace_id=None,
            external_conversation_id="chat-1",
            external_topic_id=None,
        )


@pytest.mark.anyio
async def test_channel_service_manages_two_instances_of_one_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deerflow.reflection.resolve_class",
        lambda _path, base_class=None: _RecordingChannel,
    )
    service = ChannelService(channels_config={})
    await service.start()

    assert await service.configure_channel_instance(
        "instance-a",
        "feishu",
        {"enabled": True},
    )
    assert await service.configure_channel_instance(
        "instance-b",
        "feishu",
        {"enabled": True},
    )

    assert service.get_channel_instance("instance-a").channel_instance_id == "instance-a"
    assert service.get_channel_instance("instance-b").channel_instance_id == "instance-b"
    assert await service.remove_channel_instance("instance-a")
    assert service.get_channel_instance("instance-a") is None
    assert service.get_channel_instance("instance-b") is not None
    await service.stop()


@pytest.mark.anyio
async def test_project_channel_instance_receives_shared_group_binding_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _ConfiguredChannel(_RecordingChannel):
        def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
            captured.update(config)
            super().__init__(bus, config)

    monkeypatch.setattr(
        "deerflow.reflection.resolve_class",
        lambda _path, base_class=None: _ConfiguredChannel,
    )
    group_binding_service = object()
    service = ChannelService(
        channels_config={},
        channel_group_binding_service=group_binding_service,
    )
    await service.start()
    instance_id = str(uuid.uuid4())

    try:
        assert await service.configure_channel_instance(
            instance_id,
            "feishu",
            {"enabled": True},
        )
        assert captured["channel_group_binding_service"] is group_binding_service
        assert captured["channel_instance_id"] == instance_id
    finally:
        await service.stop()


@pytest.mark.anyio
async def test_channel_service_keeps_instance_references_when_stop_fails() -> None:
    class _StopFailureChannel(_RecordingChannel):
        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    service = ChannelService(channels_config={})
    instance_id = "instance-stop-failure"
    channel = _StopFailureChannel(
        service.bus,
        channel_instance_id=instance_id,
    )
    service._instance_configs[instance_id] = (
        "feishu",
        {"enabled": True},
    )
    service._channels[instance_id] = channel

    assert await service.remove_channel_instance(instance_id) is False
    assert service.get_channel_instance(instance_id) is channel
    assert service.get_channel_instance_status(instance_id) is not None


@pytest.mark.anyio
async def test_channel_service_retains_instance_when_stop_fails() -> None:
    instance_id = str(uuid.uuid4())
    service = ChannelService(channels_config={})
    channel = _FailingStopChannel(service.bus, channel_instance_id=instance_id)
    channel._running = True
    service._channels[instance_id] = channel
    service._instance_configs[instance_id] = (
        "feishu",
        {"enabled": True},
    )

    assert await service.remove_channel_instance(instance_id) is False
    assert service.get_channel_instance(instance_id) is channel
    assert instance_id in service._instance_configs


@pytest.mark.anyio
async def test_channel_service_does_not_start_replacement_when_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = str(uuid.uuid4())
    service = ChannelService(channels_config={})
    channel = _FailingStopChannel(service.bus, channel_instance_id=instance_id)
    channel._running = True
    service._channels[instance_id] = channel
    service._instance_configs[instance_id] = (
        "feishu",
        {"enabled": True},
    )
    resolve_class = MagicMock()
    monkeypatch.setattr("deerflow.reflection.resolve_class", resolve_class)

    assert await service.restart_channel_instance(instance_id) is False
    assert service.get_channel_instance(instance_id) is channel
    resolve_class.assert_not_called()


@pytest.mark.anyio
async def test_legacy_channel_remove_and_restart_retain_failed_stop() -> None:
    service = ChannelService(
        channels_config={"feishu": {"enabled": True}},
    )
    channel = _FailingStopChannel(service.bus)
    channel._running = True
    service._channels["feishu"] = channel

    assert await service.remove_channel("feishu") is False
    assert service.get_channel("feishu") is channel
    assert service.get_channel_config("feishu") == {"enabled": True}

    assert await service.restart_channel("feishu", reload_config=False) is False
    assert service.get_channel("feishu") is channel


@pytest.mark.anyio
async def test_channel_service_retains_failed_start_until_cleanup_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = str(uuid.uuid4())
    _FailedStartAndStopChannel.creations = 0
    monkeypatch.setattr(
        "deerflow.reflection.resolve_class",
        lambda _path, base_class=None: _FailedStartAndStopChannel,
    )
    service = ChannelService(channels_config={})
    service._running = True
    config = {"enabled": True}

    assert (
        await service.configure_channel_instance(
            instance_id,
            "feishu",
            config,
        )
        is False
    )
    failed_channel = service.get_channel_instance(instance_id)
    assert isinstance(failed_channel, _FailedStartAndStopChannel)

    assert (
        await service.configure_channel_instance(
            instance_id,
            "feishu",
            config,
        )
        is False
    )
    assert service.get_channel_instance(instance_id) is failed_channel
    assert _FailedStartAndStopChannel.creations == 1


def test_resolver_rejects_connection_returned_for_another_instance() -> None:
    identity = ProviderIdentity(
        provider="feishu",
        channel_instance_id=str(uuid.uuid4()),
        external_account_id="user-1",
        workspace_id="tenant-1",
        external_conversation_id="chat-1",
        external_topic_id=None,
    )

    with pytest.raises(PrivateWorkNotFound):
        ConnectionInboundResolver._require_connection_instance(
            {"channel_instance_id": str(uuid.uuid4())},
            identity,
            "request-1",
        )


@pytest.mark.anyio
async def test_feishu_binding_uses_exact_instance() -> None:
    from app.channels.feishu import FeishuChannel

    connection_service = AsyncMock()
    channel = FeishuChannel(
        MessageBus(),
        {
            "channel_instance_id": "instance-a",
            "connection_service": connection_service,
        },
    )
    channel._reply_card = AsyncMock()

    assert await channel._bind_connection_from_connect_code(
        message_id="message-1",
        chat_id="chat-1",
        user_id="user-1",
        code="bind-code",
    )

    connection_service.complete_callback.assert_awaited_once_with(
        "feishu",
        "bind-code",
        "user-1",
        "chat-1",
        channel_instance_id="instance-a",
        metadata={"chat_id": "chat-1", "message_id": "message-1"},
        status="connected",
    )


@pytest.mark.anyio
async def test_feishu_stop_fails_if_websocket_thread_is_still_alive() -> None:
    from app.channels.feishu import FeishuChannel

    class _StuckThread:
        def join(self, _timeout: float) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    channel = FeishuChannel(MessageBus(), {"channel_instance_id": "instance-a"})
    thread = _StuckThread()
    channel._thread = thread
    channel._running = True

    with pytest.raises(RuntimeError, match="did not stop"):
        await channel.stop()

    assert channel._thread is thread


@pytest.mark.anyio
async def test_feishu_start_waits_for_initial_websocket_success() -> None:
    from app.channels.feishu import FeishuChannel

    class _SuccessfulChannel(FeishuChannel):
        def _run_ws(self, _app_id: str, _app_secret: str, _domain: str) -> None:
            self._ws_start_succeeded = True
            self._ws_startup_event.set()
            self._ws_stop_event.wait()

    channel = _SuccessfulChannel(
        MessageBus(),
        {
            "channel_instance_id": "instance-a",
            "app_id": "cli-example",
            "app_secret": "startup-success-secret",
        },
    )

    await channel.start()

    assert channel.is_running is True
    assert channel._thread is not None
    await channel.stop()
    assert channel.is_running is False
    assert channel._thread is None


@pytest.mark.anyio
async def test_feishu_start_reports_initial_websocket_failure_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.channels.feishu import FeishuChannel

    class _RejectedChannel(FeishuChannel):
        def _run_ws(self, _app_id: str, _app_secret: str, _domain: str) -> None:
            self._ws_start_succeeded = False
            self._ws_startup_event.set()

    secret = "must-not-appear-in-startup-errors"
    channel = _RejectedChannel(
        MessageBus(),
        {
            "channel_instance_id": "instance-a",
            "app_id": "cli-example",
            "app_secret": secret,
        },
    )

    await channel.start()

    assert channel.is_running is False
    assert channel._thread is None
    assert channel._api_client is None
    assert "startup failed" in caplog.text.lower()
    assert secret not in caplog.text


@pytest.mark.anyio
async def test_feishu_start_timeout_cancels_late_connection_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.channels.feishu import FeishuChannel

    class _LateChannel(FeishuChannel):
        def _run_ws(self, _app_id: str, _app_secret: str, _domain: str) -> None:
            time.sleep(0.05)
            if self._running and not self._ws_stop_event.is_set():
                self._ws_start_succeeded = True
            self._ws_startup_event.set()

    monkeypatch.setattr(
        "app.channels.feishu.FEISHU_WS_START_TIMEOUT_SECONDS",
        0.01,
    )
    channel = _LateChannel(
        MessageBus(),
        {
            "channel_instance_id": "instance-a",
            "app_id": "cli-example",
            "app_secret": "timeout-secret",
        },
    )

    await channel.start()

    assert channel.is_running is False
    assert channel._ws_start_succeeded is False
    assert channel._thread is None


@pytest.mark.anyio
async def test_runtime_heartbeat_reloads_revision_changed_on_another_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = uuid.uuid4()
    holder_id = uuid.uuid4()
    claim = SimpleNamespace(
        channel_instance_id=instance_id,
        project_id=uuid.uuid4(),
        holder_id=holder_id,
        lease_token="lease-token",
        fencing_generation=3,
    )
    repository = SimpleNamespace(
        renew_instance_lease=AsyncMock(side_effect=[claim, None]),
        get_instance=AsyncMock(
            return_value=SimpleNamespace(
                id=instance_id,
                revision=2,
                desired_status="enabled",
                deleted_at=None,
            )
        ),
        set_observed_status_with_lease=AsyncMock(return_value=SimpleNamespace(id=instance_id)),
        release_instance_lease=AsyncMock(return_value=True),
    )
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=SimpleNamespace(
                instance_id=instance_id,
                provider="feishu",
                config={"enabled": True, "app_id": "cli-new", "app_secret": "secret"},
            )
        )
    )
    channel_service = SimpleNamespace(
        configure_channel_instance=AsyncMock(return_value=True),
        get_channel_instance_status=lambda _instance_id: {"running": True},
        remove_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        lambda: _RuntimeSession(),
        channel_service,
        repository=repository,
        materializer=materializer,
        holder_id=holder_id,
        start_heartbeat_tasks=False,
    )
    coordinator._leases[instance_id] = claim
    coordinator._applied_revisions = {instance_id: 1}
    monkeypatch.setattr("app.project_channels.runtime.asyncio.sleep", AsyncMock())

    await coordinator._heartbeat(instance_id)

    materializer.load.assert_awaited_once_with(instance_id)
    channel_service.configure_channel_instance.assert_awaited_once()
    assert coordinator._applied_revisions.get(instance_id) is None


def test_connection_status_is_scoped_to_current_channel_instance() -> None:
    current_instance_id = uuid.uuid4()
    old_instance_id = uuid.uuid4()
    rows = [
        {
            "provider": "feishu",
            "channel_instance_id": str(old_instance_id),
            "status": "connected",
        }
    ]

    assert (
        project_connections._connection_status_for_runtime(
            rows,
            provider="feishu",
            channel_instance_id=current_instance_id,
        )
        == "not_connected"
    )
    assert (
        project_connections._connection_status_for_runtime(
            rows,
            provider="feishu",
            channel_instance_id=old_instance_id,
        )
        == "connected"
    )


@pytest.mark.anyio
async def test_project_connection_fails_closed_without_runtime_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = uuid.uuid4()
    runtime = project_connections._ProjectProviderRuntime(
        instance_id=instance_id,
        provider="feishu",
        public_config={"app_id": "cli-example"},
        enabled=True,
        configured=True,
        running=True,
        observed_status="running",
    )

    async def _provider_config(_request: object):
        return SimpleNamespace(enabled=True), {}

    async def _project_runtimes(_context: object):
        return {"feishu": runtime}

    monkeypatch.setattr(project_connections, "_provider_config", _provider_config)
    monkeypatch.setattr(
        project_connections,
        "_project_provider_runtimes",
        _project_runtimes,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    context = SimpleNamespace(request_id="request-1")

    with pytest.raises(HTTPException) as raised:
        await project_connections._ready_provider(request, "feishu", context)

    assert raised.value.status_code == 503


class _RuntimeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self
