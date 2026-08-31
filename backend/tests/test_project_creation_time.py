from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.routers.projects import CreateProjectRequest, PatchProjectRequest, _response
from app.projects.context import resolve_project_context
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.models import ProjectChanges
from app.projects.repository import ProjectRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


@pytest.mark.parametrize("request_model", [CreateProjectRequest, PatchProjectRequest])
def test_project_creation_time_is_not_client_authorable(request_model) -> None:
    payload = {"display_name": "Example", "created_at": "2024-01-02T03:04:05Z"}
    if request_model is CreateProjectRequest:
        payload["slug"] = "example"

    with pytest.raises(ValidationError) as caught:
        request_model.model_validate(payload)

    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [
        (("created_at",), "extra_forbidden"),
    ]


@pytest.mark.asyncio
async def test_project_responses_keep_persisted_creation_time_across_mutations(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner, project_id = uuid.uuid4(), uuid.uuid4()
    created_at = datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    entered_at = datetime(2025, 6, 7, 8, 9, 10, tzinfo=UTC)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("""INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'creation-time@example.com','user',:now,false,0)"""),
                    {"id": str(owner), "now": created_at},
                )
                session.add(
                    ProjectRow(
                        id=project_id,
                        slug="creation-time",
                        display_name="Creation Time",
                        created_by_user_id=str(owner),
                        created_at=created_at,
                    )
                )
                await session.flush()
                session.add(
                    ProjectMembershipRow(
                        project_id=project_id,
                        user_id=str(owner),
                        role="admin",
                        created_at=entered_at,
                        last_entered_at=entered_at,
                    )
                )

            context = await resolve_project_context(session, owner, project_id, "creation-time")
            repository = ProjectRepository(session)
            views = [await repository.get(context)]
            views.append(await repository.update(context, ProjectChanges(display_name="Renamed")))
            views.append(await repository.pin(context, True))
            views.append(await repository.enter(context, entered_at + timedelta(days=1)))
            page = await repository.list_for_user(owner, None, None, None, 20, "creation-time")
            views.extend(page.items)

            lifecycle = ProjectLifecycleRepository(session)
            now = datetime.now(UTC)
            views.append(await lifecycle.mark_pending(context, requested_at=now, effective_at=now + timedelta(days=1)))
            recoverable = await repository.list_for_user(owner, None, None, None, 20, "creation-time", include_recoverable=True)
            views.extend(recoverable.items)
            views.append(await lifecycle.restore(owner, project_id, request_id="creation-time", now=now))

            assert len(views) == 8
            for view in views:
                assert view.created_at == created_at
                assert view.created_at != view.last_entered_at
                assert _response(view).model_dump(mode="json")["created_at"] == "2024-01-02T03:04:05.123456Z"
            async with session.begin():
                assert await session.scalar(select(ProjectRow.created_at).where(ProjectRow.id == project_id)) == created_at
    finally:
        await engine.dispose()
