from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.binding_service import BindingService, SystemAssetBinding
from app.shared_assets.bootstrap import bootstrap_system_assets
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_service import CreateCredential, CredentialService
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.mcp_service import (
    CreateMcpServer,
    McpCredentialSlot,
    McpDefinition,
    McpService,
)
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    McpServerVersionRow,
    SkillVersionRow,
)


@dataclass(frozen=True)
class PublishedSystemCatalog:
    agent_v1: uuid.UUID


@dataclass
class M3Scenario:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    system_admin: SystemAssetGovernanceContext
    project_admin: ProjectContext
    project_editor: ProjectContext
    other_project_admin: ProjectContext
    agents: AgentService
    mcp_servers: McpService
    credentials: CredentialService
    bindings: BindingService
    resolver: ProjectAssetResolver
    system_agent_id: uuid.UUID | None = None
    system_agent_v1: uuid.UUID | None = None
    system_agent_asset_version: int | None = None
    project_agent_id: uuid.UUID | None = None
    project_mcp_id: uuid.UUID | None = None
    project_mcp_version_id: uuid.UUID | None = None
    project_mcp_asset_version: int | None = None
    project_credential_id: uuid.UUID | None = None
    project_credential_version_id: uuid.UUID | None = None
    project_credential_asset_version: int | None = None
    project_credential_secret_sentinel: str | None = field(
        default=None,
        repr=False,
    )
    project_mcp_is_approved: bool = False

    @classmethod
    async def create(cls, database_url: str) -> M3Scenario:
        engine = create_async_engine(database_url)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        system_admin = await cls._seed_system_admin(engine)
        project_admin = await cls._seed_project(
            engine,
            session_factory,
            label="m3-primary",
            role="admin",
        )
        project_editor = await cls._seed_member(
            engine,
            session_factory,
            project_id=project_admin.project_id,
            label="m3-editor",
            role="editor",
        )
        other_project_admin = await cls._seed_project(
            engine,
            session_factory,
            label="m3-other",
            role="admin",
        )
        keyring = CredentialKeyring(
            active_key_id="m3-scenario",
            _keys={"m3-scenario": secrets.token_bytes(32)},
        )
        return cls(
            engine=engine,
            session_factory=session_factory,
            system_admin=system_admin,
            project_admin=project_admin,
            project_editor=project_editor,
            other_project_admin=other_project_admin,
            agents=AgentService(session_factory),
            mcp_servers=McpService(session_factory),
            credentials=CredentialService(session_factory, keyring=keyring),
            bindings=BindingService(session_factory),
            resolver=ProjectAssetResolver(session_factory, keyring=keyring),
        )

    async def close(self) -> None:
        await self.engine.dispose()

    async def bootstrap_system_catalog(self) -> PublishedSystemCatalog:
        await bootstrap_system_assets(self.session_factory)
        async with self.session_factory() as session:
            system_agent = (
                await session.execute(
                    select(AgentRow).where(
                        AgentRow.source_key == "builtin:agent:project-assistant",
                    )
                )
            ).scalar_one()
            system_v1 = await session.get(
                AgentVersionRow,
                system_agent.current_published_version_id,
            )
        if system_v1 is None:
            raise AssertionError("packaged system Agent has no published version")
        self.system_agent_id = system_agent.id
        self.system_agent_v1 = system_v1.id
        self.system_agent_asset_version = system_agent.version

        project_agent = await self.agents.create_asset(
            self.project_admin,
            CreateAgent("m3-project-agent", "M3 Project Agent"),
        )
        project_agent_draft = await self.agents.create_version(
            self.project_admin,
            project_agent.id,
            self._agent_payload("Project Agent"),
            expected_asset_version=project_agent.version,
        )
        await self.agents.publish(
            self.project_admin,
            project_agent.id,
            project_agent_draft.id,
            expected_asset_version=project_agent.version + 1,
        )
        self.project_agent_id = project_agent.id

        secret_sentinel = f"m3-sentinel-{secrets.token_urlsafe(24)}"
        credential = await self.credentials.create(
            self.project_admin,
            CreateCredential("m3-project-token", "M3 Project Token", "token"),
            {"env": {"M3_PROJECT_TOKEN": secret_sentinel}},
        )
        self.project_credential_id = credential.id
        self.project_credential_version_id = credential.current_version_id
        self.project_credential_asset_version = credential.version
        self.project_credential_secret_sentinel = secret_sentinel

        project_mcp = await self.mcp_servers.create_asset(
            self.project_admin,
            CreateMcpServer("m3-project-mcp", "M3 Project MCP"),
        )
        project_mcp_draft = await self.mcp_servers.create_version(
            self.project_admin,
            project_mcp.id,
            McpDefinition(
                description="M3 project MCP release scenario",
                transport="http",
                url="https://m3-scenario.example.test/mcp",
                credential_slots=(
                    McpCredentialSlot(
                        name="primary",
                        purpose="Project API token",
                        payload_schema={"env": ["M3_PROJECT_TOKEN"]},
                    ),
                ),
            ),
            expected_asset_version=project_mcp.version,
        )
        await self.mcp_servers.submit_approval(
            self.project_admin,
            project_mcp.id,
            project_mcp_draft.id,
            expected_asset_version=project_mcp.version + 1,
        )
        self.project_mcp_id = project_mcp.id
        self.project_mcp_version_id = project_mcp_draft.id
        self.project_mcp_asset_version = project_mcp.version + 2

        return PublishedSystemCatalog(agent_v1=system_v1.id)

    async def bind_system_agent(self, version_id: uuid.UUID) -> SystemAssetBinding:
        async with self.session_factory() as session:
            skill_dependencies = tuple(
                (
                    await session.execute(
                        select(
                            SkillVersionRow.skill_id,
                            AgentVersionSkillRefRow.skill_version_id,
                        )
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == AgentVersionSkillRefRow.skill_version_id,
                        )
                        .where(
                            AgentVersionSkillRefRow.agent_version_id == version_id,
                        )
                        .order_by(SkillVersionRow.skill_id)
                    )
                ).all()
            )
            mcp_dependencies = tuple(
                (
                    await session.execute(
                        select(
                            McpServerVersionRow.mcp_server_id,
                            AgentVersionMcpRefRow.mcp_server_version_id,
                        )
                        .join(
                            McpServerVersionRow,
                            McpServerVersionRow.id == AgentVersionMcpRefRow.mcp_server_version_id,
                        )
                        .where(
                            AgentVersionMcpRefRow.agent_version_id == version_id,
                        )
                        .order_by(McpServerVersionRow.mcp_server_id)
                    )
                ).all()
            )
        for asset_id, dependency_version_id in skill_dependencies:
            await self.bindings.enable(
                self.project_admin,
                AssetSelection(
                    kind=AssetKind.SKILL,
                    asset_id=asset_id,
                    version_id=dependency_version_id,
                ),
            )
        for asset_id, dependency_version_id in mcp_dependencies:
            await self.bindings.enable(
                self.project_admin,
                AssetSelection(
                    kind=AssetKind.MCP,
                    asset_id=asset_id,
                    version_id=dependency_version_id,
                ),
            )
        return await self.bindings.enable(
            self.project_admin,
            AssetSelection(
                kind=AssetKind.AGENT,
                asset_id=self._required(self.system_agent_id),
                version_id=version_id,
            ),
        )

    async def attempt_runtime_system_agent_version(self) -> uuid.UUID:
        asset_id = self._required(self.system_agent_id)
        expected = self._required(self.system_agent_asset_version)
        draft = await self.agents.create_version(
            self.system_admin,
            asset_id,
            self._agent_payload("System Agent V2"),
            expected_asset_version=expected,
        )
        return draft.id

    async def resolve_bound_agent(self) -> ResolvedAgentSnapshot:
        snapshot = await self.resolver.resolve_project_asset_snapshot(
            self.project_admin,
            AssetSelection(
                kind=AssetKind.AGENT,
                asset_id=self._required(self.system_agent_id),
            ),
        )
        if not isinstance(snapshot, ResolvedAgentSnapshot):
            raise AssertionError("expected resolved Agent snapshot")
        return snapshot

    async def editor_approve_project_mcp(self) -> object:
        return await self.mcp_servers.approve(
            self.project_editor,
            self._required(self.project_mcp_id),
            self._required(self.project_mcp_version_id),
            {"primary": self._required(self.project_credential_version_id)},
            expected_asset_version=self._required(self.project_mcp_asset_version),
        )

    async def other_project_read_project_agent(self) -> object:
        return await self.agents.get(
            self.other_project_admin,
            self._required(self.project_agent_id),
        )

    async def suspend_bound_system_agent(self) -> object:
        expected = self._required(self.system_agent_asset_version)
        return await self.agents.suspend(
            self.system_admin,
            self._required(self.system_agent_id),
            expected_asset_version=expected,
        )

    async def resolve_project_mcp_before_revoke(self) -> ResolvedMcpSnapshot:
        if not self.project_mcp_is_approved:
            await self.mcp_servers.approve(
                self.project_admin,
                self._required(self.project_mcp_id),
                self._required(self.project_mcp_version_id),
                {"primary": self._required(self.project_credential_version_id)},
                expected_asset_version=self._required(self.project_mcp_asset_version),
            )
            self.project_mcp_asset_version = self._required(self.project_mcp_asset_version) + 1
            self.project_mcp_is_approved = True
        return await self.resolve_project_mcp()

    async def revoke_project_credential(self) -> object:
        expected = self._required(self.project_credential_asset_version)
        result = await self.credentials.revoke(
            self.project_admin,
            self._required(self.project_credential_id),
            expected_credential_version=expected,
        )
        self.project_credential_asset_version = expected + 1
        return result

    async def resolve_project_mcp(self) -> ResolvedMcpSnapshot:
        snapshot = await self.resolver.resolve_project_asset_snapshot(
            self.project_admin,
            AssetSelection(
                kind=AssetKind.MCP,
                asset_id=self._required(self.project_mcp_id),
            ),
        )
        if not isinstance(snapshot, ResolvedMcpSnapshot):
            raise AssertionError("expected resolved MCP snapshot")
        return snapshot

    def credential_secret_sentinel(self) -> str:
        return self._required(self.project_credential_secret_sentinel)

    @staticmethod
    def _agent_payload(description: str) -> AgentPayload:
        return AgentPayload(
            description=description,
            soul="Keep shared asset governance deterministic.",
            model_ref="default",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        )

    @staticmethod
    def _required[T](value: T | None) -> T:
        if value is None:
            raise AssertionError("scenario setup is incomplete")
        return value

    @staticmethod
    async def _seed_system_admin(
        engine: AsyncEngine,
    ) -> SystemAssetGovernanceContext:
        user_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"m3-system-{user_id}@example.com",
                    "now": datetime.now(UTC),
                },
            )
        return SystemAssetGovernanceContext(
            user_id=user_id,
            request_id="req-m3-system",
        )

    @classmethod
    async def _seed_project(
        cls,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        label: str,
        role: str,
    ) -> ProjectContext:
        user_id = uuid.uuid4()
        project_id = uuid.uuid4()
        membership_id = uuid.uuid4()
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"{label}-{user_id}@example.com",
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id,created_at,updated_at)
                    VALUES (:id,:slug,:name,:user,:now,:now)"""
                ),
                {
                    "id": project_id,
                    "slug": f"{label}-{str(project_id)[:8]}",
                    "name": label,
                    "user": str(user_id),
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version)
                    VALUES (:id,:project,:user,:role,'active',1)"""
                ),
                {
                    "id": membership_id,
                    "project": project_id,
                    "user": str(user_id),
                    "role": role,
                },
            )
        async with session_factory() as session:
            return await resolve_project_context(
                session,
                user_id,
                project_id,
                f"req-{label}",
            )

    @staticmethod
    async def _seed_member(
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        project_id: uuid.UUID,
        label: str,
        role: str,
    ) -> ProjectContext:
        user_id = uuid.uuid4()
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(user_id),
                    "email": f"{label}-{user_id}@example.com",
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version)
                    VALUES (:id,:project,:user,:role,'active',1)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "user": str(user_id),
                    "role": role,
                },
            )
        async with session_factory() as session:
            return await resolve_project_context(
                session,
                user_id,
                project_id,
                f"req-{label}",
            )
