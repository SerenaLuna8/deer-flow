from __future__ import annotations

from dataclasses import dataclass

from app.private_work.connection_service import ProjectConnectionService
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelCredentialCipher,
)

from .m4_private_threads import M4ThreadSeed, seed_m4_thread_database


@dataclass(frozen=True)
class M4ChannelConnectionRuntime:
    seed: M4ThreadSeed
    repository: ChannelConnectionRepository
    service: ProjectConnectionService

    async def begin_connect(self, provider: str) -> str:
        challenge = await self.service.begin_connect(
            self.seed.owner_a,
            provider,
            self.seed.project_agent_id,
        )
        return challenge.state

    async def list_connections(self) -> list[dict[str, object]]:
        return await self.service.list(self.seed.owner_a)

    def assert_owner_a_scope(self, connection: dict[str, object]) -> None:
        scope = self.seed.owner_a_scope
        assert connection["project_id"] == scope.project_id
        assert connection["owner_user_id"] == scope.owner_user_id


async def make_m4_channel_connection_runtime(
    database_url: str,
    *,
    cipher_key: str,
) -> M4ChannelConnectionRuntime:
    seed = await seed_m4_thread_database(database_url)
    repository = ChannelConnectionRepository(
        seed.factory,
        cipher=ChannelCredentialCipher.from_key(cipher_key),
    )
    return M4ChannelConnectionRuntime(
        seed=seed,
        repository=repository,
        service=ProjectConnectionService(seed.factory, repository=repository),
    )
