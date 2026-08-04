from __future__ import annotations

import asyncio
import uuid
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.project_channels.runtime import (
    ChannelRuntimeMaterializationError,
    ProjectChannelCredentialMaterializer,
    ProjectChannelRuntimeConfig,
    ProjectChannelRuntimeCoordinator,
)
from app.shared_assets.crypto import encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


class _MaterialSession(_Session):
    def __init__(self, material):
        self.material = material

    async def execute(self, _statement):
        return SimpleNamespace(one_or_none=lambda: self.material)


def _lease(instance_id: uuid.UUID):
    return SimpleNamespace(
        channel_instance_id=instance_id,
        project_id=uuid.uuid4(),
        holder_id=uuid.uuid4(),
        lease_token="runtime-token",
        fencing_generation=3,
    )


def _runtime_repository(row, lease):
    return SimpleNamespace(
        get_instance=AsyncMock(return_value=row),
        get_credential_binding=AsyncMock(
            return_value=SimpleNamespace(
                binding_revision=1,
                credential_version_id=uuid.uuid4(),
            )
        ),
        claim_instance_lease=AsyncMock(return_value=lease),
        renew_instance_lease=AsyncMock(return_value=lease),
        release_instance_lease=AsyncMock(return_value=True),
        set_observed_status_with_lease=AsyncMock(return_value=row),
    )


@pytest.mark.asyncio
async def test_reconcile_claims_exact_instance_and_starts_only_that_runtime() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    credential_version_id = repository.get_credential_binding.return_value.credential_version_id
    secret = "runtime-only-secret"
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=ProjectChannelRuntimeConfig(
                instance_id=instance_id,
                provider="feishu",
                config={
                    "enabled": True,
                    "app_id": "cli_example",
                    "app_secret": secret,
                },
                instance_revision=1,
                binding_revision=1,
                credential_version_id=credential_version_id,
            )
        )
    )
    channel_service = SimpleNamespace(
        configure_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )

    assert await coordinator.reconcile(instance_id) is True

    repository.claim_instance_lease.assert_awaited_once()
    call = channel_service.configure_channel_instance.await_args
    assert call.args[:2] == (str(instance_id), "feishu")
    assert call.args[2]["app_secret"] == secret
    statuses = [item.kwargs["observed_status"] for item in repository.set_observed_status_with_lease.await_args_list]
    assert statuses == ["starting", "running"]
    for item in repository.set_observed_status_with_lease.await_args_list:
        assert item.kwargs["lease_token"] == lease.lease_token
        assert item.kwargs["fencing_generation"] == lease.fencing_generation
    assert secret not in repr(materializer.load.return_value)


@pytest.mark.asyncio
async def test_reconcile_records_clear_safe_error_when_provider_start_fails() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    repository = _runtime_repository(row, _lease(instance_id))
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=ProjectChannelRuntimeConfig(
                instance_id=instance_id,
                provider="feishu",
                config={"enabled": True, "app_id": "cli_example", "app_secret": "secret"},
            )
        )
    )
    channel_service = SimpleNamespace(
        configure_channel_instance=AsyncMock(return_value=False),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )

    assert await coordinator.reconcile(instance_id) is False
    error_call = repository.set_observed_status_with_lease.await_args_list[-1]
    assert error_call.kwargs["channel_instance_id"] == instance_id
    assert error_call.kwargs["observed_status"] == "error"
    assert error_call.kwargs["last_error_code"] == "channel_provider_start_failed"


@pytest.mark.asyncio
async def test_disabled_instance_is_removed_without_claiming_a_lease() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="disabled",
        deleted_at=None,
    )
    repository = SimpleNamespace(
        get_instance=AsyncMock(return_value=row),
        claim_instance_lease=AsyncMock(),
        set_observed_status_with_lease=AsyncMock(return_value=row),
    )
    channel_service = SimpleNamespace(
        remove_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=SimpleNamespace(),
        start_heartbeat_tasks=False,
    )

    assert await coordinator.reconcile(instance_id) is True
    repository.claim_instance_lease.assert_not_awaited()
    channel_service.remove_channel_instance.assert_awaited_once_with(str(instance_id))
    repository.set_observed_status_with_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_distinguishes_unreadable_credentials_from_provider_failure() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    repository = _runtime_repository(row, _lease(instance_id))
    materializer = SimpleNamespace(load=AsyncMock(side_effect=ChannelRuntimeMaterializationError("channel_credentials_unavailable")))
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        SimpleNamespace(
            configure_channel_instance=AsyncMock(),
            remove_channel_instance=AsyncMock(return_value=True),
        ),
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )

    assert await coordinator.reconcile(instance_id) is False
    assert repository.set_observed_status_with_lease.await_args_list[-1].kwargs["last_error_code"] == "channel_credentials_unavailable"


@pytest.mark.asyncio
async def test_materializer_decrypts_only_the_exact_bound_version_without_repr_leak() -> None:
    project_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    version_id = uuid.uuid4()
    secret = "materialized-only-secret"
    keyring = CredentialKeyring(
        active_key_id="runtime-key",
        _keys=MappingProxyType({"runtime-key": b"r" * 32}),
    )
    encrypted = encrypt_credential_payload(
        {"env": {"FEISHU_APP_SECRET": secret}},
        AssetScope.PROJECT,
        project_id,
        version_id,
        keyring,
    )
    material = (
        SimpleNamespace(
            id=instance_id,
            project_id=project_id,
            provider="feishu",
            revision=1,
            public_config={
                "app_id": "cli_example",
                "domain": "https://open.feishu.cn",
            },
        ),
        SimpleNamespace(binding_revision=4),
        SimpleNamespace(credential_type="channel.feishu"),
        SimpleNamespace(id=version_id),
        SimpleNamespace(
            key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
        ),
    )
    config = await ProjectChannelCredentialMaterializer(
        lambda: _MaterialSession(material),
        keyring=keyring,
    ).load(instance_id)

    assert config.provider == "feishu"
    assert config.config == {
        "enabled": True,
        "app_id": "cli_example",
        "domain": "https://open.feishu.cn",
        "app_secret": secret,
    }
    assert config.instance_revision == 1
    assert config.binding_revision == 4
    assert config.credential_version_id == version_id
    assert secret not in repr(config)


@pytest.mark.asyncio
async def test_materializer_rejects_injected_non_official_feishu_domain_without_echo() -> None:
    project_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    version_id = uuid.uuid4()
    injected_domain = "https://open.feishu.cn.evil.example"
    keyring = CredentialKeyring(
        active_key_id="runtime-key",
        _keys=MappingProxyType({"runtime-key": b"r" * 32}),
    )
    encrypted = encrypt_credential_payload(
        {"env": {"FEISHU_APP_SECRET": "secret"}},
        AssetScope.PROJECT,
        project_id,
        version_id,
        keyring,
    )
    material = (
        SimpleNamespace(
            id=instance_id,
            project_id=project_id,
            provider="feishu",
            revision=1,
            public_config={
                "app_id": "cli_example",
                "domain": injected_domain,
            },
        ),
        SimpleNamespace(binding_revision=1),
        SimpleNamespace(credential_type="channel.feishu"),
        SimpleNamespace(id=version_id),
        SimpleNamespace(
            key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
        ),
    )

    with pytest.raises(ChannelRuntimeMaterializationError) as exc_info:
        await ProjectChannelCredentialMaterializer(
            lambda: _MaterialSession(material),
            keyring=keyring,
        ).load(instance_id)

    assert exc_info.value.code == "channel_credentials_unavailable"
    assert injected_domain not in str(exc_info.value)
    assert injected_domain not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_reconcile_starts_lease_heartbeat_before_provider_configuration() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=ProjectChannelRuntimeConfig(
                instance_id=instance_id,
                provider="feishu",
                config={"enabled": True, "app_id": "cli_example", "app_secret": "secret"},
            )
        )
    )
    channel_service = SimpleNamespace(
        configure_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )
    heartbeat_started = False

    def mark_heartbeat_started(_instance_id):
        nonlocal heartbeat_started
        heartbeat_started = True

    coordinator._ensure_heartbeat = Mock(side_effect=mark_heartbeat_started)

    async def configure(*_args):
        assert heartbeat_started is True
        return True

    channel_service.configure_channel_instance.side_effect = configure

    assert await coordinator.reconcile(instance_id) is True


@pytest.mark.asyncio
async def test_heartbeat_renews_without_waiting_for_busy_configuration_lock() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    channel_service = SimpleNamespace(
        get_channel_instance_status=Mock(return_value={"running": False}),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=SimpleNamespace(),
        start_heartbeat_tasks=False,
    )
    coordinator._leases[instance_id] = lease
    lock = coordinator._locks.setdefault(instance_id, asyncio.Lock())
    await lock.acquire()
    try:
        assert (
            await asyncio.wait_for(
                coordinator._heartbeat_once(instance_id),
                timeout=0.05,
            )
            is True
        )
    finally:
        lock.release()

    repository.renew_instance_lease.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_owner_monitor_retries_and_claims_after_current_owner_exits() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    repository.claim_instance_lease.side_effect = [None, lease]
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=ProjectChannelRuntimeConfig(
                instance_id=instance_id,
                provider="feishu",
                config={"enabled": True, "app_id": "cli_example", "app_secret": "secret"},
            )
        )
    )
    channel_service = SimpleNamespace(
        configure_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )
    coordinator._ensure_heartbeat = Mock()

    assert await coordinator.reconcile(instance_id) is True
    coordinator._ensure_heartbeat.assert_called_once_with(instance_id)
    channel_service.configure_channel_instance.assert_not_awaited()

    assert await coordinator._heartbeat_once(instance_id) is True
    channel_service.configure_channel_instance.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_reloads_running_instance_when_exact_closure_changes() -> None:
    instance_id = uuid.uuid4()
    first_version_id = uuid.uuid4()
    second_version_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=2,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    repository.get_credential_binding.return_value = SimpleNamespace(
        binding_revision=2,
        credential_version_id=second_version_id,
    )
    materializer = SimpleNamespace(
        load=AsyncMock(
            return_value=ProjectChannelRuntimeConfig(
                instance_id=instance_id,
                provider="feishu",
                config={"enabled": True, "app_id": "cli_updated", "app_secret": "new-secret"},
                instance_revision=2,
                binding_revision=2,
                credential_version_id=second_version_id,
            )
        )
    )
    channel_service = SimpleNamespace(
        get_channel_instance_status=Mock(return_value={"running": True}),
        configure_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=materializer,
        start_heartbeat_tasks=False,
    )
    coordinator._leases[instance_id] = lease
    coordinator._applied_closures[instance_id] = (1, 1, first_version_id)

    assert await coordinator._heartbeat_once(instance_id) is True
    channel_service.configure_channel_instance.assert_awaited_once()
    assert coordinator._applied_closures[instance_id] == (2, 2, second_version_id)


@pytest.mark.asyncio
async def test_heartbeat_failure_removes_local_runtime_and_drops_stale_lease() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="enabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    repository.renew_instance_lease.side_effect = RuntimeError("database unavailable")
    channel_service = SimpleNamespace(
        remove_channel_instance=AsyncMock(return_value=True),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=SimpleNamespace(),
        start_heartbeat_tasks=False,
    )
    coordinator._leases[instance_id] = lease

    assert await coordinator._heartbeat_once(instance_id) is False
    channel_service.remove_channel_instance.assert_awaited_once_with(str(instance_id))
    assert instance_id not in coordinator._leases


@pytest.mark.asyncio
async def test_remove_does_not_release_lease_when_provider_stop_fails() -> None:
    instance_id = uuid.uuid4()
    row = SimpleNamespace(
        id=instance_id,
        project_id=uuid.uuid4(),
        revision=1,
        desired_status="disabled",
        deleted_at=None,
    )
    lease = _lease(instance_id)
    repository = _runtime_repository(row, lease)
    channel_service = SimpleNamespace(
        remove_channel_instance=AsyncMock(return_value=False),
    )
    coordinator = ProjectChannelRuntimeCoordinator(
        _Session,
        channel_service,
        repository=repository,
        materializer=SimpleNamespace(),
        start_heartbeat_tasks=False,
    )
    coordinator._leases[instance_id] = lease

    assert await coordinator.remove(instance_id) is False
    assert coordinator._leases[instance_id] is lease
    repository.release_instance_lease.assert_not_awaited()
    repository.set_observed_status_with_lease.assert_not_awaited()
