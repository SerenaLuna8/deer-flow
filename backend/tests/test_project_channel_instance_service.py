from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.project_channels.credential_store import ProjectChannelCredentialRef
from app.project_channels.errors import ChannelInstanceForbidden
from app.project_channels.models import ConfigureProjectChannelInstance
from app.project_channels.service import ProjectChannelInstanceService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-channel-service",
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self

    async def execute(self, _statement):
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_configure_creates_instance_credential_and_exact_binding_atomically() -> None:
    context = _context()
    instance_id = uuid.uuid4()
    credential_ref = ProjectChannelCredentialRef(uuid.uuid4(), uuid.uuid4())
    instance = SimpleNamespace(
        id=instance_id,
        project_id=context.project_id,
        provider="feishu",
        display_name="Project Feishu",
        desired_status="enabled",
        observed_status="stopped",
        public_config={"app_id": "cli_example"},
        revision=1,
        last_error_code=None,
        updated_at=None,
        deleted_at=None,
    )

    async def set_observed(_session, **kwargs):
        instance.observed_status = kwargs["observed_status"]
        instance.last_error_code = kwargs["last_error_code"]
        return instance

    repository = SimpleNamespace(
        get_project_provider_instance=AsyncMock(return_value=None),
        get_credential_binding=AsyncMock(return_value=None),
        create_instance=AsyncMock(return_value=instance),
        replace_credential_binding=AsyncMock(),
        set_observed_status=AsyncMock(side_effect=set_observed),
    )
    credential_repository = SimpleNamespace(lock_project=AsyncMock())
    credential_store = SimpleNamespace(create=AsyncMock(return_value=credential_ref))
    runtime = SimpleNamespace(reconcile=AsyncMock())
    audit = SimpleNamespace(project_updated=AsyncMock())
    service = ProjectChannelInstanceService(
        _Session,
        repository=repository,
        credential_repository_factory=lambda _session: credential_repository,
        credential_store_factory=lambda _repository: credential_store,
        runtime_coordinator=runtime,
        audit=audit,
    )
    secret = "never-return-channel-secret"

    view = await service.configure(
        context,
        "feishu",
        ConfigureProjectChannelInstance(
            display_name="Project Feishu",
            public_config={"app_id": "cli_example"},
            credentials={"app_secret": secret},
            enabled=True,
        ),
    )

    credential_repository.lock_project.assert_awaited_once_with(context)
    create_kwargs = repository.create_instance.await_args.kwargs
    assert create_kwargs["project_id"] == context.project_id
    assert create_kwargs["provider"] == "feishu"
    assert len(create_kwargs["provider_identity_digest"]) == 64
    assert secret not in repr(create_kwargs)
    credential_store.create.assert_awaited_once()
    assert credential_store.create.await_args.kwargs["payload"] == {"env": {"FEISHU_APP_SECRET": secret}}
    binding_call = repository.replace_credential_binding.await_args
    assert isinstance(binding_call.args[0], _Session)
    assert binding_call.kwargs == {
        "project_id": context.project_id,
        "channel_instance_id": instance_id,
        "credential_id": credential_ref.credential_id,
        "credential_version_id": credential_ref.credential_version_id,
        "actor_user_id": str(context.user_id),
    }
    runtime.reconcile.assert_awaited_once_with(instance_id)
    audit.project_updated.assert_awaited_once()
    assert isinstance(audit.project_updated.await_args.args[0], _Session)
    assert audit.project_updated.await_args.args[1] is context
    assert view.provider == "feishu"
    assert view.status == "starting"
    assert view.credential_configured is True
    assert secret not in repr(view)


@pytest.mark.asyncio
async def test_list_synthesizes_unconfigured_provider_catalog() -> None:
    context = _context(ProjectRole.VIEWER)
    repository = SimpleNamespace(list_project_instances=AsyncMock(return_value=[]))
    credential_repository = SimpleNamespace(lock_project=AsyncMock())
    service = ProjectChannelInstanceService(
        _Session,
        repository=repository,
        credential_repository_factory=lambda _session: credential_repository,
    )

    views = await service.list(context)

    assert {view.provider for view in views} == {
        "feishu",
        "slack",
        "telegram",
        "discord",
        "dingtalk",
        "wecom",
        "wechat",
    }
    assert all(view.id is None for view in views)
    assert all(view.status == "unconfigured" for view in views)


@pytest.mark.asyncio
async def test_non_admin_is_rejected_before_storage() -> None:
    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("forbidden request must not open storage")

    service = ProjectChannelInstanceService(ExplodingFactory())
    with pytest.raises(ChannelInstanceForbidden):
        await service.configure(
            _context(ProjectRole.EDITOR),
            "feishu",
            ConfigureProjectChannelInstance(
                display_name=None,
                public_config={"app_id": "cli_example"},
                credentials={"app_secret": "secret"},
                enabled=True,
            ),
        )


@pytest.mark.asyncio
async def test_delete_revokes_exact_binding_credential_and_runtime() -> None:
    context = _context()
    instance_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    instance = SimpleNamespace(
        id=instance_id,
        revision=3,
    )
    binding = SimpleNamespace(credential_id=credential_id)
    repository = SimpleNamespace(
        get_project_provider_instance=AsyncMock(return_value=instance),
        get_credential_binding=AsyncMock(return_value=binding),
        revoke_credential_binding=AsyncMock(return_value=True),
        soft_delete_instance=AsyncMock(return_value=instance),
    )
    credential_repository = SimpleNamespace(lock_project=AsyncMock())
    credential_store = SimpleNamespace(revoke=AsyncMock())
    runtime = SimpleNamespace(remove=AsyncMock(return_value=True))
    audit = SimpleNamespace(project_updated=AsyncMock())
    service = ProjectChannelInstanceService(
        _Session,
        repository=repository,
        credential_repository_factory=lambda _session: credential_repository,
        credential_store_factory=lambda _repository: credential_store,
        runtime_coordinator=runtime,
        audit=audit,
    )

    await service.delete(context, "feishu")

    repository.revoke_credential_binding.assert_awaited_once()
    credential_store.revoke.assert_awaited_once_with(
        context,
        credential_id=credential_id,
        provider="feishu",
    )
    soft_delete_call = repository.soft_delete_instance.await_args
    assert isinstance(soft_delete_call.args[0], _Session)
    assert soft_delete_call.kwargs == {
        "project_id": context.project_id,
        "channel_instance_id": instance_id,
        "expected_revision": 3,
        "actor_user_id": str(context.user_id),
    }
    audit.project_updated.assert_awaited_once()
    runtime.remove.assert_awaited_once_with(instance_id)
