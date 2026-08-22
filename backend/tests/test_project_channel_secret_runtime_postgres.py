from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable

import pytest
from support.private_thread_seed import seed_private_thread_database

from app.project_channels.models import ConfigureProjectChannelInstance
from app.project_channels.runtime import ProjectChannelRuntimeCoordinator
from app.project_channels.service import ProjectChannelInstanceService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class _FakeChannelRuntime:
    def __init__(self) -> None:
        self.configurations: list[tuple[str, str, dict[str, object]]] = []
        self.removals: list[str] = []
        self.active: dict[str, tuple[str, dict[str, object]]] = {}
        self.authorize: Callable[[str, str], Awaitable[bool]] | None = None

    def set_channel_instance_authority(
        self,
        authorize: Callable[[str, str], Awaitable[bool]],
    ) -> None:
        self.authorize = authorize

    async def configure_channel_instance(
        self,
        instance_id: str,
        provider: str,
        config: dict[str, object],
    ) -> bool:
        frozen = dict(config)
        self.configurations.append((instance_id, provider, frozen))
        self.active[instance_id] = (provider, frozen)
        return True

    async def remove_channel_instance(self, instance_id: str) -> bool:
        self.removals.append(instance_id)
        self.active.pop(instance_id, None)
        return True

    def get_channel_instance_status(self, instance_id: str) -> dict[str, bool]:
        return {"running": instance_id in self.active}

    async def deliver(self, instance_id: uuid.UUID) -> str | None:
        current = self.active.get(str(instance_id))
        if current is None or self.authorize is None:
            return None
        provider, config = current
        if not await self.authorize(provider, str(instance_id)):
            return None
        value = config.get("client_secret")
        return value if isinstance(value, str) else None


def _context(seed) -> ProjectContext:
    owner = seed.owner_a
    role = ProjectRole.ADMIN
    return ProjectContext(
        user_id=owner.user_id,
        project_id=owner.project_id,
        membership_id=owner.membership_id,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=owner.membership_version,
        request_id="channel-runtime-secret-generation",
    )


@pytest.mark.asyncio
async def test_channel_delivery_rebuilds_from_current_generation_and_clear_fails_closed(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"r" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    runtime = _FakeChannelRuntime()
    coordinator = ProjectChannelRuntimeCoordinator(
        seed.factory,
        runtime,
        start_heartbeat_tasks=False,
    )
    service = ProjectChannelInstanceService(
        seed.factory,
        runtime_coordinator=coordinator,
    )
    context = _context(seed)
    first_secret = "channel-runtime-first"
    second_secret = "channel-runtime-second"
    try:
        created = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="Runtime DingTalk",
                public_config={"client_id": "runtime-client"},
                secrets={"client_secret": first_secret},
                enabled=True,
            ),
        )

        assert created.id is not None
        assert await runtime.deliver(created.id) == first_secret
        assert runtime.configurations[-1][2]["client_secret"] == first_secret

        replaced = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="Runtime DingTalk",
                public_config={"client_id": "runtime-client"},
                secrets={"client_secret": second_secret},
                enabled=True,
            ),
        )

        assert replaced.secret_revision == created.secret_revision + 1
        assert await runtime.deliver(created.id) == second_secret
        assert [entry[2]["client_secret"] for entry in runtime.configurations] == [
            first_secret,
            second_secret,
        ]

        cleared = await service.clear_secret(
            context,
            "dingtalk",
            confirmed=True,
        )

        assert cleared.enabled is True
        assert cleared.secret_readiness == "unready"
        assert await runtime.deliver(created.id) is None
        assert str(created.id) in runtime.removals
        serialized = repr((runtime.configurations, runtime.removals))
        assert "ACT_WEAVE_SECRET_KEY" not in serialized
    finally:
        await coordinator.stop()
        await seed.engine.dispose()
