from __future__ import annotations

import asyncio
import dataclasses
import uuid

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import AdmittedPrivateRun, PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import Capability
from app.shared_assets.model_refs import ConfiguredModelRefResolver
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection
from deerflow.config.app_config import AppConfig


def _model_config(*model_names: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "models": [{"name": name, "use": "pkg:Model", "model": f"provider/{name}"} for name in model_names],
        }
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_persists_pending_snapshot_before_runtime_calls(migrated_postgres_database_url: str) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-{uuid.uuid4()}"
    calls: list[str] = []

    from app.shared_assets.resolver import ProjectAssetResolver

    class Resolver(ProjectAssetResolver):
        async def resolve_project_asset_snapshot_in_session(self, session, context, selection):
            calls.append("resolve")
            assert selection == AssetSelection(AssetKind.AGENT, seed.project_agent_id)
            return await super().resolve_project_asset_snapshot_in_session(session, context, selection)

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        service = PrivateRunAdmissionService(seed.factory, resolver=Resolver(seed.factory))
        admitted = await service.admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(metadata={"source": "task5"}),
        )

        assert isinstance(admitted, AdmittedPrivateRun)
        assert admitted.run.status == "pending"
        assert admitted.snapshot.assets[0].asset_kind == "agent"
        assert admitted.snapshot.catalog_generation >= 0
        assert calls == ["resolve"]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_resolves_default_to_exact_configured_model_name(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-default-model-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await session.execute(
                text(
                    """UPDATE agent_versions SET model_ref='default'
                    WHERE agent_id=:agent_id"""
                ),
                {"agent_id": seed.project_agent_id},
            )

        admitted = await PrivateRunAdmissionService(
            seed.factory,
            model_ref_resolver=ConfiguredModelRefResolver(_model_config("primary-logical", "secondary-logical")),
        ).admit(seed.owner_a, thread_id, PrivateRunCreate())

        assert admitted.run.model_name == "primary-logical"
        async with seed.factory() as session:
            persisted = await session.scalar(
                text("SELECT model_name FROM runs WHERE run_id=:run_id"),
                {"run_id": admitted.run.run_id},
            )
        assert persisted == "primary-logical"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_rejects_unknown_model_ref_without_partial_run_or_job(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-missing-model-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await session.execute(
                text(
                    """UPDATE agent_versions SET model_ref='missing-logical'
                    WHERE agent_id=:agent_id"""
                ),
                {"agent_id": seed.project_agent_id},
            )

        with pytest.raises(PrivateWorkAssetStale):
            await PrivateRunAdmissionService(
                seed.factory,
                model_ref_resolver=ConfiguredModelRefResolver(_model_config("primary-logical")),
            ).admit(seed.owner_a, thread_id, PrivateRunCreate())

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM jobs WHERE run_id IN
                            (SELECT run_id FROM runs WHERE thread_id=:thread_id)),
                        (SELECT count(*) FROM run_asset_versions
                            WHERE thread_id=:thread_id)"""
                    ),
                    {"thread_id": thread_id},
                )
            ).one()
        assert tuple(counts) == (0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_overwrites_forged_run_asset_authority_and_rejects_strategy(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-forged-{uuid.uuid4()}"
    invalid_thread_id = f"admit-invalid-strategy-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            for current_thread_id in (thread_id, invalid_thread_id):
                await PrivateThreadRepository(session).create(
                    scope=seed.owner_a_scope,
                    thread_id=current_thread_id,
                    agent=ThreadAgentRef(seed.project_agent_id, "project"),
                )

        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                assistant_id="forged-assistant",
                status="success",
                metadata={
                    "safe": "value",
                    "nested": {"agent_id": "forged-agent"},
                },
                kwargs={
                    "input": "safe",
                    "context": {"project_id": "forged-project"},
                },
                model_name="forged-model",
            ),
        )
        assert admitted.run.status == "pending"
        assert admitted.run.assistant_id == str(seed.project_agent_id)
        assert admitted.run.model_name == "test-model"
        assert admitted.run.metadata == {"safe": "value", "nested": {}}
        assert admitted.run.kwargs == {"input": "safe", "context": {}}

        with pytest.raises(PrivateWorkConflict):
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_a,
                invalid_thread_id,
                PrivateRunCreate(multitask_strategy="rollback"),
            )
        async with seed.factory() as session:
            invalid_runs = await session.scalar(
                text("SELECT count(*) FROM runs WHERE thread_id=:thread_id"),
                {"thread_id": invalid_thread_id},
            )
            invalid_snapshots = await session.scalar(
                text(
                    """SELECT count(*) FROM run_asset_versions
                    WHERE thread_id=:thread_id"""
                ),
                {"thread_id": invalid_thread_id},
            )
        assert invalid_runs == 0
        assert invalid_snapshots == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_requires_both_capabilities_and_hides_invisible_threads(migrated_postgres_database_url: str) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-cap-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        viewer_thread_id = f"admit-viewer-{uuid.uuid4()}"
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.viewer.resource_scope,
                thread_id=viewer_thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        with pytest.raises(PrivateWorkForbidden) as forbidden:
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.viewer,
                viewer_thread_id,
                PrivateRunCreate(),
            )
        assert forbidden.value.request_id == seed.viewer.request_id

        required_calls: list[tuple[Capability, ...]] = []

        class DenyingRevalidator:
            def __init__(self, denied: Capability) -> None:
                self.denied = denied

            async def require(self, session, context, *capabilities, lock=False):
                del session, lock
                required_calls.append(capabilities)
                assert self.denied in capabilities
                raise PrivateWorkForbidden(context.request_id)

        for denied in (
            Capability.PRIVATE_WORK_CREATE,
            Capability.SHARED_ASSETS_EXECUTE,
        ):
            with pytest.raises(PrivateWorkForbidden):
                await PrivateRunAdmissionService(
                    seed.factory,
                    revalidator=DenyingRevalidator(denied),
                ).admit(seed.owner_a, thread_id, PrivateRunCreate())
        assert required_calls == [
            (Capability.PRIVATE_WORK_CREATE, Capability.SHARED_ASSETS_EXECUTE),
            (Capability.PRIVATE_WORK_CREATE, Capability.SHARED_ASSETS_EXECUTE),
        ]

        with pytest.raises(PrivateWorkNotFound):
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_b,
                thread_id,
                PrivateRunCreate(),
            )
        with pytest.raises(PrivateWorkNotFound):
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.project_b_owner_a,
                thread_id,
                PrivateRunCreate(),
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_rejects_busy_deleted_and_frozen_threads(migrated_postgres_database_url: str) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-busy-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            thread = await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        service = PrivateRunAdmissionService(seed.factory)
        await service.admit(seed.owner_a, thread_id, PrivateRunCreate())
        with pytest.raises(PrivateWorkConflict):
            await service.admit(seed.owner_a, thread_id, PrivateRunCreate())

        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).mark_deleted(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                expected_version=thread.version,
            )
        with pytest.raises(PrivateWorkNotFound):
            await service.admit(seed.owner_a, thread_id, PrivateRunCreate())

        frozen_thread_id = f"admit-frozen-{uuid.uuid4()}"
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=frozen_thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await session.execute(
                text("UPDATE threads_meta SET frozen_at=now() WHERE thread_id=:thread_id"),
                {"thread_id": frozen_thread_id},
            )
        with pytest.raises(PrivateWorkNotFound):
            await service.admit(seed.owner_a, frozen_thread_id, PrivateRunCreate())
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("drift", "expected_error"),
    [
        pytest.param("membership_stale", PrivateWorkNotFound, id="membership-stale"),
        pytest.param("project_suspended", PrivateWorkNotFound, id="project-suspended"),
        pytest.param("agent_scope_mismatch", PrivateWorkAssetStale, id="thread-agent-scope-mismatch"),
    ],
)
async def test_admission_fails_closed_on_scope_drift_without_partial_run_or_snapshot(
    migrated_postgres_database_url: str,
    drift: str,
    expected_error: type[Exception],
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-scope-drift-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            if drift == "membership_stale":
                await session.execute(
                    text(
                        """UPDATE project_memberships
                        SET version=version+1
                        WHERE id=:membership_id"""
                    ),
                    {"membership_id": seed.owner_a.membership_id},
                )
            elif drift == "project_suspended":
                await session.execute(
                    text("UPDATE projects SET is_suspended=true WHERE id=:project_id"),
                    {"project_id": seed.owner_a.project_id},
                )
        service = PrivateRunAdmissionService(seed.factory)
        if drift == "agent_scope_mismatch":
            from app.shared_assets.resolver import ProjectAssetResolver

            class ScopeMismatchResolver(ProjectAssetResolver):
                async def resolve_project_asset_snapshot_in_session(
                    self,
                    session,
                    context,
                    selection,
                ):
                    snapshot = await super().resolve_project_asset_snapshot_in_session(
                        session,
                        context,
                        selection,
                    )
                    return dataclasses.replace(snapshot, scope=AssetScope.SYSTEM)

            service = PrivateRunAdmissionService(
                seed.factory,
                resolver=ScopeMismatchResolver(seed.factory),
            )

        with pytest.raises(expected_error):
            await service.admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(),
            )

        async with seed.factory() as session:
            run_count = await session.scalar(
                text("SELECT count(*) FROM runs WHERE thread_id=:thread_id"),
                {"thread_id": thread_id},
            )
            snapshot_count = await session.scalar(
                text(
                    """SELECT count(*) FROM run_asset_versions
                    WHERE thread_id=:thread_id"""
                ),
                {"thread_id": thread_id},
            )
        assert run_count == 0
        assert snapshot_count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_admission_rejects_non_executable_agent_but_ignores_unrelated_generation_race(
    migrated_postgres_database_url: str,
) -> None:
    from sqlalchemy import func, select

    from app.private_work.errors import PrivateWorkAssetStale
    from deerflow.persistence.run.model import RunRow

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    suspended_thread_id = f"admit-suspended-{uuid.uuid4()}"
    racing_thread_id = f"admit-race-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            for thread_id in (suspended_thread_id, racing_thread_id):
                await PrivateThreadRepository(session).create(
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    agent=ThreadAgentRef(seed.project_agent_id, "project"),
                )
            await session.execute(
                text("UPDATE agents SET status='suspended' WHERE id=:agent_id"),
                {"agent_id": seed.project_agent_id},
            )
        with pytest.raises(PrivateWorkAssetStale):
            await PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_a,
                suspended_thread_id,
                PrivateRunCreate(),
            )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE agents SET status='active' WHERE id=:agent_id"),
                {"agent_id": seed.project_agent_id},
            )

        from app.shared_assets.resolver import ProjectAssetResolver

        class RacingResolver(ProjectAssetResolver):
            async def resolve_project_asset_snapshot_in_session(self, session, context, selection):
                snapshot = await super().resolve_project_asset_snapshot_in_session(session, context, selection)
                await session.execute(text("UPDATE asset_catalog_state SET generation=generation+1 WHERE id=1"))
                return snapshot

        admitted = await PrivateRunAdmissionService(
            seed.factory,
            resolver=RacingResolver(seed.factory),
        ).admit(seed.owner_a, racing_thread_id, PrivateRunCreate())
        assert admitted.thread_id == racing_thread_id

        async with seed.factory() as session:
            count = await session.scalar(select(func.count()).select_from(RunRow).where(RunRow.thread_id.in_((suspended_thread_id, racing_thread_id))))
        assert count == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_concurrent_same_thread_admission_serializes_the_empty_active_run_gap(
    migrated_postgres_database_url: str,
) -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"admit-concurrent-{uuid.uuid4()}"
    resolver_entered = asyncio.Event()
    release_resolver = asyncio.Event()
    second_revalidated = asyncio.Event()
    session_pids: list[int] = []
    first = None
    second = None

    class NonLockingObservedRevalidator:
        async def require(self, session, context, *capabilities, lock=False):
            assert lock is True
            current = await PrivateWorkRevalidator().require(
                session,
                context,
                *capabilities,
                lock=False,
            )
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            session_pids.append(pid)
            if len(session_pids) == 2:
                second_revalidated.set()
            return current

    class BarrierResolver(ProjectAssetResolver):
        async def resolve_project_asset_snapshot_in_session(
            self,
            session,
            context,
            selection,
        ):
            resolver_entered.set()
            await asyncio.wait_for(release_resolver.wait(), timeout=5)
            return await super().resolve_project_asset_snapshot_in_session(
                session,
                context,
                selection,
            )

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        service = PrivateRunAdmissionService(
            seed.factory,
            resolver=BarrierResolver(seed.factory),
            revalidator=NonLockingObservedRevalidator(),
        )
        first = asyncio.create_task(service.admit(seed.owner_a, thread_id, PrivateRunCreate()))
        await asyncio.wait_for(resolver_entered.wait(), timeout=5)
        second = asyncio.create_task(service.admit(seed.owner_a, thread_id, PrivateRunCreate()))
        await asyncio.wait_for(second_revalidated.wait(), timeout=5)

        blockers = ()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            async with seed.factory() as session, session.begin():
                blockers = tuple(
                    await asyncio.wait_for(
                        session.scalar(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": session_pids[1]},
                        ),
                        timeout=1,
                    )
                    or ()
                )
            if blockers:
                break
        assert session_pids[0] in blockers
        assert second.done() is False

        release_resolver.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True),
            timeout=10,
        )
        assert sum(isinstance(outcome, AdmittedPrivateRun) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, PrivateWorkConflict) for outcome in outcomes) == 1
        async with seed.factory() as session:
            run_count = await session.scalar(
                text("SELECT count(*) FROM runs WHERE thread_id=:thread_id"),
                {"thread_id": thread_id},
            )
            snapshot_count = await session.scalar(
                text(
                    """SELECT count(*) FROM run_asset_versions
                    WHERE thread_id=:thread_id"""
                ),
                {"thread_id": thread_id},
            )
        assert run_count == 1
        assert snapshot_count == 1
    finally:
        release_resolver.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=10,
            )
        await seed.engine.dispose()
