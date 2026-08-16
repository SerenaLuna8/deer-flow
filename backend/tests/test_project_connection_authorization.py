from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.gateway.routers.project_channel_instances import list_project_channel_instances
from app.gateway.routers.project_connections import (
    begin_project_connection,
    disconnect_project_connection,
    list_project_connection_providers,
    list_project_connections,
)
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkForbidden
from app.project_channels.errors import ChannelInstanceForbidden
from app.project_channels.service import ProjectChannelInstanceService
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

NON_CHANNEL_ADMIN_ROLES = (
    ProjectRole.RUNNER,
    ProjectRole.EDITOR,
    ProjectRole.VIEWER,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _CapabilityEnforcingRevalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[Capability, ...]] = []

    async def require(
        self,
        session,
        context: PrivateWorkContext,
        *capabilities: Capability,
        lock: bool = False,
    ) -> ProjectContext:
        del session, lock
        self.calls.append(capabilities)
        if any(capability not in context.capabilities for capability in capabilities):
            raise PrivateWorkForbidden(context.request_id)
        return ProjectContext(
            user_id=context.user_id,
            project_id=context.project_id,
            membership_id=context.membership_id,
            role=context.role,
            capabilities=context.capabilities,
            membership_version=context.membership_version,
            request_id=context.request_id,
        )


def _project_context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=7,
        request_id=f"connection-auth-{role.value}",
    )


def _connection_service(
    *,
    repository,
    revalidator: _CapabilityEnforcingRevalidator,
    context_resolver=None,
) -> ProjectConnectionService:
    return ProjectConnectionService(
        _SessionFactory(),
        repository=repository,
        revalidator=revalidator,
        context_resolver=context_resolver,
        state_factory=lambda: "fixed-state",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_connection_list_requires_project_channel_management(role: ProjectRole) -> None:
    context = PrivateWorkContext.from_project(_project_context(role))
    repository = SimpleNamespace(list_connections=AsyncMock(return_value=[]))
    revalidator = _CapabilityEnforcingRevalidator()
    service = _connection_service(repository=repository, revalidator=revalidator)

    with pytest.raises(PrivateWorkForbidden):
        await service.list(context)

    assert revalidator.calls == [(Capability.PROJECT_CHANNELS_MANAGE,)]
    repository.list_connections.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_connection_begin_requires_project_channel_management(role: ProjectRole) -> None:
    context = PrivateWorkContext.from_project(_project_context(role))
    repository = SimpleNamespace(create_oauth_state_within_cap=AsyncMock(return_value=True))
    revalidator = _CapabilityEnforcingRevalidator()
    service = _connection_service(repository=repository, revalidator=revalidator)

    with pytest.raises(PrivateWorkForbidden):
        await service.begin_connect(
            context,
            "telegram",
            uuid.uuid4(),
            channel_instance_id=str(uuid.uuid4()),
        )

    assert revalidator.calls == [(Capability.PROJECT_CHANNELS_MANAGE,)]
    repository.create_oauth_state_within_cap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_connection_disconnect_requires_project_channel_management(role: ProjectRole) -> None:
    context = PrivateWorkContext.from_project(_project_context(role))
    repository = SimpleNamespace(disconnect_connection=AsyncMock(return_value=True))
    revalidator = _CapabilityEnforcingRevalidator()
    service = _connection_service(repository=repository, revalidator=revalidator)

    with pytest.raises(PrivateWorkForbidden):
        await service.disconnect(context, "connection-1")

    assert revalidator.calls == [(Capability.PROJECT_CHANNELS_MANAGE,)]
    repository.disconnect_connection.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_connection_callback_revalidates_project_channel_management(role: ProjectRole) -> None:
    project_context = _project_context(role)
    channel_instance_id = str(uuid.uuid4())
    consumed_state = {
        "project_id": project_context.project_id,
        "owner_user_id": project_context.user_id,
        "membership_version": project_context.membership_version,
        "provider": "telegram",
        "channel_instance_id": channel_instance_id,
        "metadata": {
            "agent_asset_id": str(uuid.uuid4()),
            "agent_scope": "project",
            "membership_id": str(project_context.membership_id),
            "membership_version": project_context.membership_version,
            "request_id": project_context.request_id,
        },
    }
    repository = SimpleNamespace(
        consume_oauth_state=AsyncMock(return_value=consumed_state),
        upsert_connection=AsyncMock(return_value={}),
    )
    revalidator = _CapabilityEnforcingRevalidator()

    async def resolve_context(session, user_id, project_id, request_id, *, lock):
        del session
        assert user_id == project_context.user_id
        assert project_id == project_context.project_id
        assert request_id == project_context.request_id
        assert lock is False
        return project_context

    service = _connection_service(
        repository=repository,
        revalidator=revalidator,
        context_resolver=resolve_context,
    )

    with pytest.raises(PrivateWorkForbidden):
        await service.complete_callback(
            "telegram",
            "fixed-state",
            "external-account",
            channel_instance_id=channel_instance_id,
        )

    assert revalidator.calls == [(Capability.PROJECT_CHANNELS_MANAGE,)]
    repository.upsert_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_instance_list_requires_project_channel_management_before_io() -> None:
    admin = _project_context(ProjectRole.ADMIN)
    context_without_manage = ProjectContext(
        user_id=admin.user_id,
        project_id=admin.project_id,
        membership_id=admin.membership_id,
        role=admin.role,
        capabilities=admin.capabilities - {Capability.PROJECT_CHANNELS_MANAGE},
        membership_version=admin.membership_version,
        request_id=admin.request_id,
    )

    def unexpected_session():
        raise AssertionError("channel instance storage must not be opened before authorization")

    service = ProjectChannelInstanceService(unexpected_session)

    with pytest.raises(ChannelInstanceForbidden):
        await service.list(context_without_manage)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_channel_instance_router_rejects_non_admin_roles(role: ProjectRole) -> None:
    context = _project_context(role)
    service = SimpleNamespace(list=AsyncMock(return_value=()))

    with pytest.raises(HTTPException) as exc_info:
        await list_project_channel_instances(context=context, service=service)

    assert exc_info.value.status_code == 403
    service.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_list_allows_project_channel_management() -> None:
    context = PrivateWorkContext.from_project(_project_context(ProjectRole.ADMIN))
    expected = [{"id": "connection-1"}]
    repository = SimpleNamespace(list_connections=AsyncMock(return_value=expected))
    revalidator = _CapabilityEnforcingRevalidator()
    service = _connection_service(repository=repository, revalidator=revalidator)

    assert await service.list(context) == expected
    assert revalidator.calls == [(Capability.PROJECT_CHANNELS_MANAGE,)]
    repository.list_connections.assert_awaited_once_with(context.resource_scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
async def test_connection_router_rejects_before_provider_runtime_reconciliation(
    role: ProjectRole,
) -> None:
    context = PrivateWorkContext.from_project(_project_context(role))

    with pytest.raises(HTTPException) as exc_info:
        await begin_project_connection(
            request=SimpleNamespace(),
            provider="feishu",
            body=SimpleNamespace(),
            context=context,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", NON_CHANNEL_ADMIN_ROLES)
@pytest.mark.parametrize("route", ("providers", "list", "disconnect"))
async def test_connection_router_rejects_every_read_and_mutation_route(
    role: ProjectRole,
    route: str,
) -> None:
    context = PrivateWorkContext.from_project(_project_context(role))
    request = SimpleNamespace()

    with pytest.raises(HTTPException) as exc_info:
        if route == "providers":
            await list_project_connection_providers(request=request, context=context)
        elif route == "list":
            await list_project_connections(request=request, context=context)
        else:
            await disconnect_project_connection(
                request=request,
                connection_id="connection-1",
                context=context,
            )

    assert exc_info.value.status_code == 403
