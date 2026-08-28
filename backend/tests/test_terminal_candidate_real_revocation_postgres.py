from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

import pytest
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database

from app.audit.models import resolve_system_audit_context
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import resolve_project_context
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole
from app.projects.system_lifecycle import SystemProjectLifecycleService
from app.reliability.run_execution.contracts import AgentExecutionResult
from app.reliability.run_execution.handler import PrivateRunJobHandler
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.events.models import (
    StreamLeaseProof,
    StreamTerminalCandidate,
    StreamWriteAuthorityRequired,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True, slots=True)
class _RunningScenario:
    seed: PrivateThreadSeed
    thread_id: str
    run_id: str
    claim: JobClaim


@dataclass(frozen=True, slots=True)
class _SystemAdmin:
    id: uuid.UUID
    system_role: str = "system_admin"


class _NoopSystemLifecycleAudit:
    async def project_suspended(self, _session, *, project_id: uuid.UUID) -> None:
        del project_id

    async def project_resumed(self, _session, *, project_id: uuid.UUID) -> None:
        del project_id


@pytest.mark.asyncio
async def test_settled_terminal_rejects_an_unissued_authority() -> None:
    store = DbRunEventStore(SimpleNamespace(), run_event_notify_enabled=False)
    with pytest.raises(
        StreamWriteAuthorityRequired,
        match="settlement authority is invalid",
    ):
        await store.ensure_settled_stream_terminal(
            SimpleNamespace(),  # type: ignore[arg-type]
            scope=PrivateResourceScope(
                project_id=str(uuid.uuid4()),
                owner_user_id=str(uuid.uuid4()),
                membership_version=1,
            ),
            thread_id="thread-forged-authority",
            run_id="run-forged-authority",
            status="interrupted",
            settlement_authority=object(),  # type: ignore[arg-type]
        )


async def _running_owner_b_scenario(database_url: str) -> _RunningScenario:
    seed = await seed_private_thread_database(database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_b_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="terminal-candidate-real-revocation",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )

    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_b,
        thread_id,
        PrivateRunCreate(
            run_id=run_id,
            kwargs={"input": {"messages": []}},
        ),
    )
    async with seed.factory() as session, session.begin():
        job = await session.get(JobRow, admitted.job.job_id)
        assert job is not None
        job.priority = 32_767
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=300,
        )
        assert claim is not None and claim.job_id == admitted.job.job_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_b_scope,
            run_id=run_id,
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
        )
    return _RunningScenario(
        seed=seed,
        thread_id=thread_id,
        run_id=run_id,
        claim=claim,
    )


async def _apply_real_revocation(
    scenario: _RunningScenario,
    revocation: Literal["role_downgrade", "member_removal", "project_pause"],
) -> None:
    if revocation in {"role_downgrade", "member_removal"}:
        async with scenario.seed.factory() as session:
            admin = await resolve_project_context(
                session,
                scenario.seed.owner_a.user_id,
                scenario.seed.owner_a.project_id,
                "terminal-candidate-membership-revocation",
            )
            service = MembershipService(MembershipRepository(session))
            if revocation == "role_downgrade":
                await service.change_role(
                    admin,
                    scenario.seed.owner_b.membership_id,
                    ProjectRole.VIEWER,
                    expected_version=scenario.seed.owner_b.membership_version,
                )
            else:
                await service.remove(
                    admin,
                    scenario.seed.owner_b.membership_id,
                    expected_version=scenario.seed.owner_b.membership_version,
                )
        return

    audit_context = resolve_system_audit_context(
        _SystemAdmin(uuid.uuid4()),
        request_id="terminal-candidate-project-pause",
    )
    async with scenario.seed.factory() as session, session.begin():
        await SystemProjectLifecycleService(
            session,
            audit=_NoopSystemLifecycleAudit(),  # type: ignore[arg-type]
        ).suspend(
            audit_context,
            scenario.seed.owner_b.project_id,
            now=datetime.now(UTC),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation",
    ["role_downgrade", "member_removal", "project_pause"],
)
async def test_real_governance_revocation_after_terminal_candidate_settles_interrupted(
    migrated_postgres_database_url: str,
    revocation: Literal["role_downgrade", "member_removal", "project_pause"],
) -> None:
    """A real governance revocation remains stronger than the candidate."""

    scenario = await _running_owner_b_scenario(migrated_postgres_database_url)
    events = DbRunEventStore(
        scenario.seed.factory,
        run_event_notify_enabled=False,
    )
    candidate = StreamTerminalCandidate(
        status="error",
        error_code="MODEL_OUTPUT_LIMIT",
    )
    try:
        async with scenario.seed.factory() as session, session.begin():
            stored = await events.append_stream_terminal_candidate(
                session,
                scope=scenario.seed.owner_b_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.run_id,
                candidate=candidate,
                lease=StreamLeaseProof(
                    job_id=scenario.claim.job_id,
                    lease_token=scenario.claim.lease_token,
                ),
            )
            assert stored == candidate

        await _apply_real_revocation(scenario, revocation)

        settlement = PrivateRunJobHandler(
            scenario.seed.factory,
            executor=SimpleNamespace(),
        )._settlement(
            scenario.claim,
            AgentExecutionResult.cancelled(),
            scope=scenario.seed.owner_b_scope,
        )
        await settlement.commit()

        async with scenario.seed.factory() as session:
            run = await session.get(RunRow, scenario.run_id)
            job = await session.get(JobRow, scenario.claim.job_id)
            attempt = await session.get(JobAttemptRow, scenario.claim.attempt_id)
            membership = await session.get(
                ProjectMembershipRow,
                scenario.seed.owner_b.membership_id,
            )
            project = await session.get(
                ProjectRow,
                scenario.seed.owner_b.project_id,
            )
            terminal = await events.get_stream_terminal(
                session,
                scope=scenario.seed.owner_b_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.run_id,
            )
            frames = await events.list_stream_frames(
                session,
                scope=scenario.seed.owner_b_scope,
                thread_id=scenario.thread_id,
                run_id=scenario.run_id,
                cursor=0,
                limit=100,
            )

            expected_membership = {
                "role_downgrade": ("viewer", "active", 2),
                "member_removal": ("runner", "removed", 2),
                "project_pause": ("runner", "active", 1),
            }[revocation]
            assert membership is not None and project is not None
            assert (
                membership.role,
                membership.status,
                membership.version,
            ) == expected_membership
            assert project.is_suspended is (revocation == "project_pause")
            assert run is not None and (run.status, run.error) == (
                "interrupted",
                "authorization_revoked",
            )
            assert job is not None and job.status == "cancelled"
            assert attempt is not None and attempt.outcome == "cancelled"
            assert terminal is not None and terminal.data == {
                "status": "interrupted",
            }
            assert sum(frame.terminal for frame in frames) == 1

        public_events = await events.list_events(
            scenario.thread_id,
            scenario.run_id,
            scope=scenario.seed.owner_b_scope,
        )
        assert all(event["event_type"] != "run.terminal_candidate" for event in public_events)
    finally:
        await scenario.seed.engine.dispose()
