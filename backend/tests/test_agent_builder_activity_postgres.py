from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_activity import (
    MAX_AGENT_DESIGN_ACTIVITY_BYTES_PER_OPERATION,
    AgentDesignActivityKind,
    AgentDesignActivityLimitExceeded,
    AgentDesignActivityRepository,
    activity_view,
)
from app.shared_assets.agent_design_control import AgentDesignGenerationControl
from app.shared_assets.agent_design_service import (
    AgentDesignMessageTurn,
    AgentDesignService,
    CancelAgentDesignSession,
    SubmitAgentDesignTurn,
)
from app.shared_assets.errors import AssetNotFound
from deerflow.persistence.shared_assets import (
    AgentDesignActivityRow,
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)


async def _seed_activity_scope(database_url: str):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    session_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO users
                   (id,email,system_role,created_at,needs_setup,token_version)
                   VALUES (:user_id,:email,'user',:now,false,0)""",
            ),
            {
                "user_id": user_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
        await session.execute(
            text(
                """INSERT INTO projects
                   (id,slug,display_name,created_by_user_id)
                   VALUES (:project_id,:slug,'Activity Test',:user_id)""",
            ),
            {
                "project_id": project_id,
                "slug": f"activity-{project_id.hex[:12]}",
                "user_id": user_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO project_memberships
                   (id,project_id,user_id,role)
                   VALUES (:membership_id,:project_id,:user_id,'admin')""",
            ),
            {
                "membership_id": membership_id,
                "project_id": project_id,
                "user_id": user_id,
            },
        )
        session.add(
            AgentDesignSessionRow(
                id=session_id,
                project_id=project_id,
                owner_user_id=user_id,
                thread_id=uuid.uuid4(),
                slug="activity-test",
                display_name="Activity Test",
                status="interviewing",
                revision=1,
                messages_json=[],
                progress_json=[],
                create_idempotency_key_hash="a" * 64,
                create_request_checksum="b" * 64,
            ),
        )
        await session.flush()
        session.add(
            AgentDesignOperationRow(
                id=operation_id,
                project_id=project_id,
                owner_user_id=user_id,
                session_id=session_id,
                operation_kind="turn",
                idempotency_key_hash="c" * 64,
                request_checksum="d" * 64,
                status="in_progress",
            ),
        )
    context = ProjectContext(
        user_id=uuid.UUID(user_id),
        project_id=project_id,
        membership_id=membership_id,
        role=ProjectRole.ADMIN,
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            },
        ),
        membership_version=1,
        request_id="activity-postgres-test",
    )
    return engine, sessions, context, session_id, operation_id


@pytest.mark.asyncio
async def test_activity_replay_is_monotonic_private_and_clearable(
    migrated_postgres_database_url: str,
) -> None:
    engine, sessions, context, session_id, operation_id = await _seed_activity_scope(
        migrated_postgres_database_url,
    )
    try:
        async with sessions() as session, session.begin():
            repository = AgentDesignActivityRepository(session)
            first = await repository.append(
                context,
                session_id=session_id,
                operation_id=operation_id,
                kind=AgentDesignActivityKind.TURN_ACCEPTED,
            )
            second = await repository.append(
                context,
                session_id=session_id,
                operation_id=operation_id,
                kind=AgentDesignActivityKind.REASONING,
                attempt=1,
                payload={"text": "真实思考"},
            )
        assert int(second.seq) > int(first.seq)

        async with sessions() as session, session.begin():
            replay = await AgentDesignActivityRepository(session).list_after(
                context,
                session_id=session_id,
                after_seq=0,
                limit=500,
            )
            assert [row.kind for row in replay] == [
                "turn_accepted",
                "reasoning",
            ]
            assert [
                row.kind
                for row in await AgentDesignActivityRepository(session).list_after(
                    context,
                    session_id=session_id,
                    after_seq=int(first.seq),
                    limit=500,
                )
            ] == ["reasoning"]

        outsider = ProjectContext(
            user_id=uuid.uuid4(),
            project_id=context.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=context.capabilities,
            membership_version=1,
            request_id="activity-outsider-test",
        )
        async with sessions() as session, session.begin():
            with pytest.raises(AssetNotFound):
                await AgentDesignActivityRepository(session).list_after(
                    outsider,
                    session_id=session_id,
                    after_seq=0,
                    limit=500,
                )

        async with sessions() as session, session.begin():
            await AgentDesignActivityRepository(session).clear_session(
                context,
                session_id=session_id,
            )
        async with sessions() as session, session.begin():
            assert (
                await AgentDesignActivityRepository(session).list_after(
                    context,
                    session_id=session_id,
                    after_seq=0,
                    limit=500,
                )
                == ()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_activity_replay_keeps_all_legacy_reasoning_content(
    migrated_postgres_database_url: str,
) -> None:
    engine, sessions, context, session_id, operation_id = await _seed_activity_scope(
        migrated_postgres_database_url,
    )
    try:
        async with sessions() as session, session.begin():
            common = {
                "project_id": context.project_id,
                "owner_user_id": str(context.user_id),
                "session_id": session_id,
                "operation_id": operation_id,
            }
            session.add_all(
                [
                    AgentDesignActivityRow(
                        **common,
                        attempt=1,
                        kind=AgentDesignActivityKind.REASONING.value,
                        payload_json={"text": "system pro"},
                    ),
                    AgentDesignActivityRow(
                        **common,
                        attempt=1,
                        kind=AgentDesignActivityKind.REASONING.value,
                        payload_json={"text": "mpt: P0_LEGACY_AGENT_SPLIT_CANARY"},
                    ),
                    AgentDesignActivityRow(
                        **common,
                        attempt=1,
                        kind=AgentDesignActivityKind.REASONING.value,
                        payload_json={
                            "text": '{"phase":"composition","plan":"P0_LEGACY_AGENT_JSON_CANARY"}',
                        },
                    ),
                    AgentDesignActivityRow(
                        **common,
                        attempt=1,
                        kind=AgentDesignActivityKind.VALIDATION_STARTED.value,
                        payload_json={},
                    ),
                    AgentDesignActivityRow(
                        **common,
                        attempt=2,
                        kind=AgentDesignActivityKind.REASONING.value,
                        payload_json={"text": "普通修复思考"},
                    ),
                ]
            )

        async with sessions() as session, session.begin():
            rows = await AgentDesignActivityRepository(session).list_after(
                context,
                session_id=session_id,
                after_seq=0,
                limit=5,
            )
            replay = tuple(activity_view(row) for row in rows)

        assert [activity.kind for activity in replay] == [
            AgentDesignActivityKind.REASONING,
            AgentDesignActivityKind.REASONING,
            AgentDesignActivityKind.REASONING,
            AgentDesignActivityKind.VALIDATION_STARTED,
            AgentDesignActivityKind.REASONING,
        ]
        assert [activity.payload for activity in replay] == [
            {"text": "system pro"},
            {"text": "mpt: P0_LEGACY_AGENT_SPLIT_CANARY"},
            {
                "text": '{"phase":"composition","plan":"P0_LEGACY_AGENT_JSON_CANARY"}',
            },
            {},
            {"text": "普通修复思考"},
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_activity_has_one_terminal_and_reserves_space_for_it(
    migrated_postgres_database_url: str,
) -> None:
    engine, sessions, context, session_id, operation_id = await _seed_activity_scope(
        migrated_postgres_database_url,
    )
    try:
        async with sessions() as session, session.begin():
            await AgentDesignActivityRepository(session).append(
                context,
                session_id=session_id,
                operation_id=operation_id,
                kind=AgentDesignActivityKind.REASONING,
                attempt=1,
                payload={
                    "text": "x" * (MAX_AGENT_DESIGN_ACTIVITY_BYTES_PER_OPERATION - 2_048),
                },
            )
        async with sessions() as session, session.begin():
            await AgentDesignActivityRepository(session).append(
                context,
                session_id=session_id,
                operation_id=operation_id,
                kind=AgentDesignActivityKind.TURN_TERMINAL,
                payload={"status": "completed"},
            )
        with pytest.raises(IntegrityError):
            async with sessions() as session, session.begin():
                await AgentDesignActivityRepository(session).append(
                    context,
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=AgentDesignActivityKind.TURN_TERMINAL,
                    payload={"status": "failed"},
                )
        with pytest.raises(AgentDesignActivityLimitExceeded):
            async with sessions() as session, session.begin():
                await AgentDesignActivityRepository(session).append(
                    context,
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=AgentDesignActivityKind.REASONING,
                    attempt=1,
                    payload={"text": "x" * 2_048},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stop_then_cancel_converges_and_clears_private_activity(
    migrated_postgres_database_url: str,
) -> None:
    engine, sessions, context, session_id, seeded_operation_id = await _seed_activity_scope(migrated_postgres_database_url)

    class BlockingGenerator:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def generate(self, *_args: object, **kwargs: object):
            activity_callback = kwargs["activity_callback"]
            abort_event = kwargs["abort_event"]
            await activity_callback("attempt_started", 1, {})
            await activity_callback(
                "reasoning",
                1,
                {"text": "api_key=p0ReasoningContentMustRemainVisible12345"},
            )
            self.started.set()
            await abort_event.wait()
            raise asyncio.CancelledError

    generator = BlockingGenerator()
    service = AgentDesignService(
        sessions,
        generator=generator,  # type: ignore[arg-type]
        generation_control=AgentDesignGenerationControl(),
    )
    stop_service = AgentDesignService(
        sessions,
        generation_control=AgentDesignGenerationControl(),
    )
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(AgentDesignOperationRow).where(
                    AgentDesignOperationRow.id == seeded_operation_id,
                ),
            )

        first_task = asyncio.create_task(
            service.submit_turn(
                context,
                session_id,
                SubmitAgentDesignTurn(
                    input=AgentDesignMessageTurn(
                        kind="message",
                        message="设计一个审查助手",
                    ),
                    expected_revision=1,
                    idempotency_key="stoppable-turn-1",
                ),
            ),
        )
        await asyncio.wait_for(generator.started.wait(), timeout=2)
        stopped = await stop_service.stop_turn(context, session_id)
        settled = await first_task
        assert stopped.status.value == "interviewing"
        assert settled.status.value == "interviewing"
        activities = await service.list_activities(context, session_id)
        assert activities[-1].kind is AgentDesignActivityKind.TURN_TERMINAL
        assert activities[-1].payload["status"] == "stopped"
        assert any(activity.payload.get("text") == "api_key=p0ReasoningContentMustRemainVisible12345" for activity in activities)

        generator.started.clear()
        second_task = asyncio.create_task(
            service.submit_turn(
                context,
                session_id,
                SubmitAgentDesignTurn(
                    input=AgentDesignMessageTurn(
                        kind="message",
                        message="继续补充安全审查要求",
                    ),
                    expected_revision=settled.revision,
                    idempotency_key="stoppable-turn-2",
                ),
            ),
        )
        await asyncio.wait_for(generator.started.wait(), timeout=2)
        generating = await service.get(context, session_id)
        cancelled = await service.cancel(
            context,
            session_id,
            CancelAgentDesignSession(
                expected_revision=generating.revision,
                idempotency_key="cancel-active-design",
            ),
        )
        await second_task
        assert cancelled.status.value == "cancelled"
        assert cancelled.messages == ()
        assert cancelled.blueprint is None
        assert cancelled.generation_preference is None
        assert await service.list_activities(context, session_id) == ()
    finally:
        await engine.dispose()
