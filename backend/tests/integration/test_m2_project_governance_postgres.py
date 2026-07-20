from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.gateway.deps import project_session
from app.gateway.routers import project_invitations, project_members
from app.projects.context import resolve_project_context
from app.projects.errors import ProjectLastAdmin
from app.projects.invitation_models import ProjectInvitationInvalid
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig


@dataclass(frozen=True)
class GovernanceMatrixResult:
    cross_project_reads: int
    cross_project_mutations: int
    concurrent_invitation_successes: int
    last_admin_violations: int


async def _insert_user(connection, user_id: uuid.UUID, email: str) -> None:
    await connection.execute(
        text(
            """INSERT INTO users
            (id,email,system_role,created_at,needs_setup,token_version)
            VALUES (:id,:email,'user',:now,false,0)"""
        ),
        {"id": str(user_id), "email": email, "now": datetime.now(UTC)},
    )


async def _insert_project(connection, owner_id: uuid.UUID, slug: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO projects (id,slug,display_name,created_by_user_id)
            VALUES (:id,:slug,:display_name,:owner_id)"""
        ),
        {
            "id": project_id,
            "slug": slug,
            "display_name": slug.title(),
            "owner_id": str(owner_id),
        },
    )
    return project_id


async def _insert_membership(
    connection,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: ProjectRole,
) -> uuid.UUID:
    membership_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO project_memberships (id,project_id,user_id,role)
            VALUES (:id,:project_id,:user_id,:role)"""
        ),
        {
            "id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
            "role": role.value,
        },
    )
    return membership_id


def _assert_hidden(response: httpx.Response) -> None:
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_OR_MEMBER_NOT_FOUND"


async def _exercise_matrix(database_url: str) -> GovernanceMatrixResult:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users = {
        name: uuid.uuid4()
        for name in (
            "alpha_admin",
            "beta_admin_one",
            "beta_admin_two",
            "invitee",
            "pending_invitee",
        )
    }
    emails = {name: f"{name}@example.com" for name in users}

    try:
        async with engine.begin() as connection:
            for name, user_id in users.items():
                await _insert_user(connection, user_id, emails[name])
            alpha_id = await _insert_project(connection, users["alpha_admin"], "m2-alpha")
            beta_id = await _insert_project(connection, users["beta_admin_one"], "m2-beta")
            await _insert_membership(
                connection,
                alpha_id,
                users["alpha_admin"],
                ProjectRole.ADMIN,
            )
            beta_membership_one = await _insert_membership(
                connection,
                beta_id,
                users["beta_admin_one"],
                ProjectRole.ADMIN,
            )
            beta_membership_two = await _insert_membership(
                connection,
                beta_id,
                users["beta_admin_two"],
                ProjectRole.ADMIN,
            )

        async with factory() as session:
            alpha_context = await resolve_project_context(
                session,
                users["alpha_admin"],
                alpha_id,
                "m2-alpha-context",
            )
        async with factory() as session:
            beta_context_one = await resolve_project_context(
                session,
                users["beta_admin_one"],
                beta_id,
                "m2-beta-one-context",
            )
        async with factory() as session:
            beta_context_two = await resolve_project_context(
                session,
                users["beta_admin_two"],
                beta_id,
                "m2-beta-two-context",
            )

        now = datetime.now(UTC)
        async with factory() as session:
            beta_invitation = await InvitationService(InvitationRepository(session)).create(
                beta_context_one,
                emails["pending_invitee"],
                ProjectRole.VIEWER,
                now,
            )
        async with factory() as session:
            redeem_invitation = await InvitationService(InvitationRepository(session)).create(
                alpha_context,
                emails["invitee"],
                ProjectRole.EDITOR,
                now,
            )
            redeem_claim = await InvitationService(InvitationRepository(session)).claim(
                redeem_invitation.token,
                now,
            )

        async with engine.connect() as connection:
            beta_membership_before = (
                await connection.execute(
                    text(
                        """SELECT role,status,version FROM project_memberships
                        WHERE id=:membership_id"""
                    ),
                    {"membership_id": beta_membership_one},
                )
            ).one()
            beta_invitation_before = (
                await connection.execute(
                    text(
                        """SELECT status,version FROM project_invitations
                        WHERE id=:invitation_id"""
                    ),
                    {"invitation_id": beta_invitation.invitation.id},
                )
            ).one()

        app = FastAPI()
        app.include_router(project_members.router)
        app.include_router(project_invitations.router)

        async def request_session():
            async with factory() as session:
                yield session

        async def project_identity(request: Request) -> tuple[uuid.UUID, str]:
            return uuid.UUID(request.headers["x-test-user"]), "m2-matrix-request"

        app.dependency_overrides[project_session] = request_session
        app.dependency_overrides[project_members.authenticated_project_identity] = project_identity
        app.dependency_overrides[project_invitations.authenticated_project_identity] = project_identity
        audit_keyring = AuditHmacKeyring.from_environment()
        audit_service = AuditService(factory, audit_keyring)
        app.state.operational_audit_sink = OperationalAuditSink(
            audit_service,
            process_context=_bind_gateway_audit_process(audit_service),
        )
        app.state.project_quota_enforcer = ProjectQuotaEnforcer(QuotaService(factory, QuotaConfig(), source_ref_hasher=audit_keyring))
        headers = {"x-test-user": str(users["alpha_admin"])}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            hidden_reads = (
                await client.get(f"/api/projects/{beta_id}/members", headers=headers),
                await client.get(
                    f"/api/projects/{beta_id}/invitations",
                    headers=headers,
                ),
            )
            hidden_mutations = (
                await client.patch(
                    f"/api/projects/{alpha_id}/members/{beta_membership_one}",
                    headers=headers,
                    json={"role": "viewer", "version": 1},
                ),
                await client.request(
                    "DELETE",
                    f"/api/projects/{alpha_id}/members/{beta_membership_one}",
                    headers=headers,
                    json={"version": 1},
                ),
                await client.request(
                    "DELETE",
                    f"/api/projects/{alpha_id}/invitations/{beta_invitation.invitation.id}",
                    headers=headers,
                    json={"version": 1},
                ),
            )

        for response in (*hidden_reads, *hidden_mutations):
            _assert_hidden(response)

        async with engine.connect() as connection:
            beta_membership_after = (
                await connection.execute(
                    text(
                        """SELECT role,status,version FROM project_memberships
                        WHERE id=:membership_id"""
                    ),
                    {"membership_id": beta_membership_one},
                )
            ).one()
            beta_invitation_after = (
                await connection.execute(
                    text(
                        """SELECT status,version FROM project_invitations
                        WHERE id=:invitation_id"""
                    ),
                    {"invitation_id": beta_invitation.invitation.id},
                )
            ).one()

        redeem_ready = 0
        redeem_ready_lock = asyncio.Lock()
        both_redeemers_ready = asyncio.Event()
        release_redeemers = asyncio.Event()

        async def redeem_once():
            nonlocal redeem_ready
            async with factory() as session:
                async with redeem_ready_lock:
                    redeem_ready += 1
                    if redeem_ready == 2:
                        both_redeemers_ready.set()
                await release_redeemers.wait()
                return await InvitationService(InvitationRepository(session)).redeem(
                    users["invitee"],
                    emails["invitee"],
                    redeem_claim,
                    now,
                )

        redeem_tasks = [asyncio.create_task(redeem_once()) for _ in range(2)]
        await asyncio.wait_for(both_redeemers_ready.wait(), timeout=5)
        assert redeem_ready == 2
        release_redeemers.set()
        redeem_results = await asyncio.wait_for(
            asyncio.gather(*redeem_tasks, return_exceptions=True),
            timeout=10,
        )
        invitation_successes = sum(not isinstance(result, BaseException) for result in redeem_results)
        invitation_failures = [result for result in redeem_results if isinstance(result, BaseException)]
        assert len(invitation_failures) == 1
        assert isinstance(invitation_failures[0], ProjectInvitationInvalid)

        demotion_ready = 0
        demotion_ready_lock = asyncio.Lock()
        both_demotions_ready = asyncio.Event()
        release_demotions = asyncio.Event()

        async def demote_self(context, membership_id: uuid.UUID):
            nonlocal demotion_ready
            async with factory() as session:
                async with demotion_ready_lock:
                    demotion_ready += 1
                    if demotion_ready == 2:
                        both_demotions_ready.set()
                await release_demotions.wait()
                return await MembershipService(MembershipRepository(session)).change_role(
                    context,
                    membership_id,
                    ProjectRole.VIEWER,
                    expected_version=1,
                )

        demotion_tasks = [
            asyncio.create_task(demote_self(beta_context_one, beta_membership_one)),
            asyncio.create_task(demote_self(beta_context_two, beta_membership_two)),
        ]
        await asyncio.wait_for(both_demotions_ready.wait(), timeout=5)
        assert demotion_ready == 2
        release_demotions.set()
        demotion_results = await asyncio.wait_for(
            asyncio.gather(*demotion_tasks, return_exceptions=True),
            timeout=10,
        )
        assert sum(not isinstance(result, BaseException) for result in demotion_results) == 1
        demotion_failures = [result for result in demotion_results if isinstance(result, BaseException)]
        assert len(demotion_failures) == 1
        assert isinstance(demotion_failures[0], ProjectLastAdmin)

        async with engine.connect() as connection:
            active_admin_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM project_memberships
                        WHERE project_id=:project_id AND status='active' AND role='admin'"""
                    ),
                    {"project_id": beta_id},
                )
            ).scalar_one()
            invitee_membership_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM project_memberships
                        WHERE project_id=:project_id AND user_id=:user_id"""
                    ),
                    {"project_id": alpha_id, "user_id": str(users["invitee"])},
                )
            ).scalar_one()
            redeemed_invitation = (
                await connection.execute(
                    text(
                        """SELECT status,version FROM project_invitations
                        WHERE id=:invitation_id"""
                    ),
                    {"invitation_id": redeem_invitation.invitation.id},
                )
            ).one()
        assert invitee_membership_count == 1
        assert tuple(redeemed_invitation) == ("redeemed", 2)

        cross_project_reads = sum(response.status_code != 404 for response in hidden_reads)
        cross_project_mutations = int(beta_membership_after != beta_membership_before) + int(beta_invitation_after != beta_invitation_before)
        last_admin_violations = int(active_admin_count != 1)
        return GovernanceMatrixResult(
            cross_project_reads=cross_project_reads,
            cross_project_mutations=cross_project_mutations,
            concurrent_invitation_successes=invitation_successes,
            last_admin_violations=last_admin_violations,
        )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m2_cross_project_and_last_admin_matrix(
    migrated_postgres_database_url: str,
) -> None:
    result = await _exercise_matrix(migrated_postgres_database_url)

    assert result.cross_project_reads == 0
    assert result.cross_project_mutations == 0
    assert result.concurrent_invitation_successes == 1
    assert result.last_admin_violations == 0
