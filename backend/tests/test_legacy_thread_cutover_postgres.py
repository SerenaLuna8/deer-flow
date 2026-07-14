from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text
from starlette.requests import Request
from support.m4_private_threads import seed_m4_thread_database

from app.gateway.authz import AuthContext, Permissions
from app.gateway.deps import langgraph_runtime
from app.gateway.routers import runs, threads
from app.gateway.routers.thread_runs import RunCreateRequest
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.runtime.user_context import reset_current_user, set_current_user


def _config(database_url: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "database": {"url": database_url},
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        }
    )


def _request(app: FastAPI, user, *, method: str, path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "app": app,
        }
    )
    request.state.user = user
    request.state.auth = AuthContext(
        user=user,
        permissions=[
            Permissions.THREADS_READ,
            Permissions.THREADS_WRITE,
            Permissions.THREADS_DELETE,
            Permissions.RUNS_CREATE,
        ],
    )
    return request


@contextmanager
def _user_context(user):
    token = set_current_user(user)
    try:
        yield
    finally:
        reset_current_user(token)


async def _put_root(raw, thread_id: str) -> None:
    await raw.aput(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        empty_checkpoint(),
        {
            "step": -1,
            "source": "input",
            "writes": None,
            "parents": {},
        },
        {},
    )


class _FailingDeleteProxy:
    def __init__(self, raw) -> None:
        self._raw = raw
        self.delete_calls: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._raw, name)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_calls.append(thread_id)
        raise RuntimeError("provider-secret raw delete failed")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_production_lifespan_rejects_legacy_create_and_stateless_run_before_raw_write(
    migrated_postgres_database_url: str,
) -> None:
    config = _config(migrated_postgres_database_url)
    app = FastAPI()
    user = SimpleNamespace(id="legacy-cutover-user", system_role="user")
    set_app_config(config)
    try:
        async with langgraph_runtime(app, config):
            assert app.state._raw_checkpointer.__class__.__name__ == "AsyncPostgresSaver"
            assert app.state.thread_store.__class__.__name__ == "TrustedUnscopedThreadMetaStore"

            create_request = _request(app, user, method="POST", path="/api/threads")
            with _user_context(user), pytest.raises(HTTPException) as create_error:
                await threads.create_thread(
                    threads.ThreadCreateRequest(thread_id="legacy-create-no-authority"),
                    create_request,
                )
            assert create_error.value.status_code == 409
            assert create_error.value.detail["code"] == "PRIVATE_WORK_CUTOVER"

            run_request = _request(app, user, method="POST", path="/api/runs/wait")
            run_body = RunCreateRequest(config={"configurable": {"thread_id": "stateless-no-authority"}})
            with _user_context(user), pytest.raises(HTTPException) as run_error:
                await runs.stateless_wait(run_body, run_request)
            assert run_error.value.status_code == 409
            assert run_error.value.detail["code"] == "PRIVATE_WORK_CUTOVER"

            for thread_id in ("legacy-create-no-authority", "stateless-no-authority"):
                assert await app.state._raw_checkpointer.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}) is None

            async with app.state.run_store._sf() as session:
                run_count = await session.scalar(text("SELECT count(*) FROM runs"))
            assert run_count == 0
    finally:
        reset_app_config()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_production_legacy_delete_uses_raw_saver_and_keeps_failed_cleanup_tombstone(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    config = _config(migrated_postgres_database_url)
    app = FastAPI()
    user = SimpleNamespace(id=str(seed.owner_a.user_id), system_role="user")
    set_app_config(config)
    try:
        from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

        repository = ThreadMetaRepository(seed.factory)
        for thread_id in ("legacy-delete-success", "legacy-delete-retry"):
            await repository.create(
                thread_id,
                scope=seed.owner_a_scope,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
            )

        async with langgraph_runtime(app, config):
            raw = app.state._raw_checkpointer
            await _put_root(raw, "legacy-delete-success")
            await _put_root(raw, "legacy-delete-retry")

            success_request = _request(
                app,
                user,
                method="DELETE",
                path="/api/threads/legacy-delete-success",
            )
            with _user_context(user):
                response = await threads.delete_thread_data(
                    thread_id="legacy-delete-success",
                    request=success_request,
                )
            assert response.success is True
            assert (
                await raw.aget_tuple(
                    {
                        "configurable": {
                            "thread_id": "legacy-delete-success",
                            "checkpoint_ns": "",
                        }
                    }
                )
                is None
            )

            proxy = _FailingDeleteProxy(raw)
            app.state._raw_checkpointer = proxy
            retry_request = _request(
                app,
                user,
                method="DELETE",
                path="/api/threads/legacy-delete-retry",
            )
            with _user_context(user), pytest.raises(HTTPException) as retry_error:
                await threads.delete_thread_data(
                    thread_id="legacy-delete-retry",
                    request=retry_request,
                )
            assert retry_error.value.status_code == 503
            assert retry_error.value.detail["code"] == "PRIVATE_WORK_UNAVAILABLE"
            assert proxy.delete_calls == ["legacy-delete-retry"]
            assert (
                await raw.aget_tuple(
                    {
                        "configurable": {
                            "thread_id": "legacy-delete-retry",
                            "checkpoint_ns": "",
                        }
                    }
                )
                is not None
            )

            async with seed.engine.connect() as connection:
                statuses = dict(
                    (
                        await connection.execute(
                            text(
                                """SELECT thread_id, checkpoint_delete_status
                                FROM threads_meta
                                WHERE thread_id IN
                                    ('legacy-delete-success','legacy-delete-retry')"""
                            )
                        )
                    ).all()
                )
            assert statuses == {
                "legacy-delete-success": "complete",
                "legacy-delete-retry": "retry_required",
            }
            assert (
                await app.state.thread_store.check_access(
                    "legacy-delete-retry",
                    str(seed.owner_b.user_id),
                )
                is False
            )
    finally:
        reset_app_config()
        await seed.engine.dispose()
