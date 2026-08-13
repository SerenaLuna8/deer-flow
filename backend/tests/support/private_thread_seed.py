from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import AgentPayload
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True)
class PrivateThreadSeed:
    engine: AsyncEngine
    factory: async_sessionmaker
    owner_a: PrivateWorkContext
    owner_b: PrivateWorkContext
    project_agent_id: uuid.UUID

    @property
    def owner_a_scope(self) -> PrivateResourceScope:
        return self.owner_a.resource_scope

    @property
    def owner_b_scope(self) -> PrivateResourceScope:
        return self.owner_b.resource_scope


def _project_context(
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    membership_id: uuid.UUID,
    role: ProjectRole,
    request_id: str,
) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id=request_id,
        )
    )


async def seed_private_thread_database(database_url: str) -> PrivateThreadSeed:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_a_id = uuid.uuid4()
    owner_b_id = uuid.uuid4()
    project_a_id = uuid.uuid4()
    memberships = {
        "owner_a": uuid.uuid4(),
        "owner_b": uuid.uuid4(),
    }
    project_agent_id = uuid.uuid4()
    project_agent_version_id = uuid.uuid4()
    project_agent_checksum = agent_payload_checksum(
        AgentPayload(
            description="",
            soul="thread agent",
            model_ref="test-model",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        )
    )

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',now(),false,0)"""
            ),
            [
                {"id": str(owner_a_id), "email": f"{owner_a_id}@example.com"},
                {"id": str(owner_b_id), "email": f"{owner_b_id}@example.com"},
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id)
                VALUES (:id,:slug,:name,:owner)"""
            ),
            {
                "id": project_a_id,
                "slug": f"thread-a-{project_a_id.hex[:12]}",
                "name": "Thread Project A",
                "owner": str(owner_a_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES (:id,:project_id,:user_id,:role,'active',1)"""
            ),
            [
                {
                    "id": memberships["owner_a"],
                    "project_id": project_a_id,
                    "user_id": str(owner_a_id),
                    "role": "admin",
                },
                {
                    "id": memberships["owner_b"],
                    "project_id": project_a_id,
                    "user_id": str(owner_b_id),
                    "role": "runner",
                },
            ],
        )
        await connection.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                VALUES (:id,:scope,:project_id,:slug,:name,'active',1,:owner)"""
            ),
            {
                "id": project_agent_id,
                "scope": "project",
                "project_id": project_a_id,
                "slug": "project-thread-agent",
                "name": "Project Thread Agent",
                "owner": str(owner_a_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,soul,
                 model_ref,tool_groups,payload_checksum,created_by_user_id)
                VALUES (:id,:agent_id,1,'published','','thread agent','test-model',
                        '[]'::jsonb,:checksum,:owner)"""
            ),
            {
                "id": project_agent_version_id,
                "agent_id": project_agent_id,
                "checksum": project_agent_checksum,
                "owner": str(owner_a_id),
            },
        )
        await connection.execute(
            text(
                """UPDATE agents SET current_published_version_id=:version_id
                WHERE id=:agent_id"""
            ),
            {"agent_id": project_agent_id, "version_id": project_agent_version_id},
        )

    return PrivateThreadSeed(
        engine=engine,
        factory=factory,
        owner_a=_project_context(
            user_id=owner_a_id,
            project_id=project_a_id,
            membership_id=memberships["owner_a"],
            role=ProjectRole.ADMIN,
            request_id="req-owner-a",
        ),
        owner_b=_project_context(
            user_id=owner_b_id,
            project_id=project_a_id,
            membership_id=memberships["owner_b"],
            role=ProjectRole.RUNNER,
            request_id="req-owner-b",
        ),
        project_agent_id=project_agent_id,
    )
