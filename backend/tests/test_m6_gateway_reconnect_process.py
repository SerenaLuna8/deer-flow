from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from support.project_agent_factory import create_project_agent_from_design

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.shared_assets.models import AgentPayload
from deerflow.runtime.events.models import StreamFrame
from deerflow.runtime.events.stream import PostgresStreamBridge
from deerflow.runtime.private_scope import PrivateResourceScope

_PROCESS_TIMEOUT = 60.0


def _config(database_url: str) -> str:
    return f"""\
log_level: warning
models:
  - name: release-model
    use: langchain_openai.ChatOpenAI
    model: release-model
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
database:
  url: {database_url}
memory:
  token_counting: char
worker:
  enabled: true
  poll_interval_seconds: 0.1
  lease_seconds: 15
  heartbeat_seconds: 4
  max_concurrent_jobs: 1
scheduler:
  enabled: false
"""


def _reserve_port() -> tuple[socket.socket, int]:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reservation.bind(("127.0.0.1", 0))
    reservation.listen(socket.SOMAXCONN)
    return reservation, int(reservation.getsockname()[1])


def _gateway_environment(tmp_path: Path, database_url: str) -> dict[str, str]:
    config = tmp_path / "gateway-config.yaml"
    config.write_text(_config(database_url), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "DEER_FLOW_CONFIG_PATH": str(config),
            "DEER_FLOW_HOME": str(tmp_path / "gateway-home"),
            "GATEWAY_WORKERS": "1",
            "AUTH_JWT_SECRET": "m6-release-gateway-process-secret",
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID": "test-audit-v1",
            "DEER_FLOW_AUDIT_KEYRING_JSON": ('{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}'),
        }
    )
    return environment


def _start_gateway(
    tmp_path: Path,
    database_url: str,
    name: str,
) -> tuple[subprocess.Popen[bytes], object, str]:
    reservation, port = _reserve_port()
    log = (tmp_path / f"{name}.log").open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.gateway.app:app",
                "--fd",
                str(reservation.fileno()),
                "--log-level",
                "warning",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=_gateway_environment(tmp_path, database_url),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            pass_fds=(reservation.fileno(),),
        )
    finally:
        reservation.close()
    return process, log, f"http://127.0.0.1:{port}"


def _stop_process(process: subprocess.Popen[bytes], log: object) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        else:
            process.wait(timeout=1)
    finally:
        log.close()  # type: ignore[attr-defined]


async def _wait_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT
    last_observation = "no response"
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Gateway pid={process.pid} exited with {process.returncode}")
            try:
                response = await client.get(f"{base_url}/health")
                last_observation = f"HTTP {response.status_code}"
                if response.status_code == 200:
                    return
            except httpx.HTTPError as error:
                last_observation = type(error).__name__
            await asyncio.sleep(0.05)
    raise AssertionError(f"Gateway pid={process.pid} did not become ready; last observation: {last_observation}")


async def _create_project_thread(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str]:
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"gateway-{uuid.uuid4().hex[:8]}@example.com",
            "password": "very-strong-password-123",
        },
    )
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["id"]
    csrf = client.cookies.get("csrf_token")
    assert csrf
    headers = {"X-CSRF-Token": csrf}
    suffix = uuid.uuid4().hex[:10]
    project = await client.post(
        "/api/projects",
        json={"slug": f"gateway-{suffix}", "display_name": "Gateway release"},
        headers=headers,
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    created = await create_project_agent_from_design(
        session_factory,
        user_id=uuid.UUID(user_id),
        project_id=uuid.UUID(project_id),
        slug=f"agent-{suffix}",
        display_name="Gateway Agent",
        payload=AgentPayload(
            description="Gateway reconnect process gate",
            soul="Release gate",
            model_ref="release-model",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        request_id="gateway-reconnect-agent-setup",
    )
    agent_id = str(created.asset.id)
    activated = await client.post(
        f"/api/projects/{project_id}/agents/{agent_id}/activate",
        json={"expected_asset_version": created.asset.version},
        headers=headers,
    )
    assert activated.status_code == 200, activated.text
    thread_id = str(uuid.uuid4())
    thread = await client.post(
        f"/api/projects/{project_id}/private-work/threads",
        json={
            "thread_id": thread_id,
            "agent_asset_id": agent_id,
            "agent_scope": "project",
            "metadata": {},
        },
        headers=headers,
    )
    assert thread.status_code == 201, thread.text
    return user_id, project_id, thread_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_last_event_id_replays_after_formal_gateway_process_replacement(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    first = second = None
    first_log = second_log = None
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first, first_log, first_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "gateway-first",
        )
        await _wait_ready(first, first_url)
        async with httpx.AsyncClient(
            base_url=first_url,
            timeout=10,
            trust_env=False,
        ) as client:
            user_id, project_id, thread_id = await _create_project_thread(
                client,
                factory,
            )
            cookies = httpx.Cookies(client.cookies)

        scope = PrivateResourceScope(
            project_id=project_id,
            owner_user_id=user_id,
            membership_version=1,
        )
        run_id = str(uuid.uuid4())
        async with factory() as session, session.begin():
            await PrivateRunRepository(session).create(
                scope=scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=run_id, status="running"),
            )
        bridge = PostgresStreamBridge(factory)
        first_frame = await bridge.publish_frame(
            scope,
            thread_id,
            run_id,
            StreamFrame(event="updates", data={"delta": "first"}),
        )
        path = f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/stream"
        async with httpx.AsyncClient(
            base_url=first_url,
            cookies=cookies,
            timeout=10,
            trust_env=False,
        ) as first_owner:
            async with asyncio.timeout(10):
                async with first_owner.stream("GET", path) as live:
                    assert live.status_code == 200
                    async for line in live.aiter_lines():
                        if line == f"id: {first_frame.id}":
                            break
                    else:  # pragma: no cover - an open SSE stream does not end
                        raise AssertionError("first Gateway closed before the first frame")

        _stop_process(first, first_log)
        first = first_log = None

        second_frame = await bridge.publish_frame(
            scope,
            thread_id,
            run_id,
            StreamFrame(event="updates", data={"delta": "second"}),
        )
        terminal = await bridge.publish_terminal(
            scope,
            thread_id,
            run_id,
            status="success",
        )
        async with factory() as session, session.begin():
            assert await PrivateRunRepository(session).update_status(
                scope=scope,
                run_id=run_id,
                status="success",
            )

        second, second_log, second_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "gateway-second",
        )
        await _wait_ready(second, second_url)
        async with httpx.AsyncClient(
            base_url=second_url,
            cookies=cookies,
            timeout=10,
            trust_env=False,
        ) as owner:
            replay = await owner.get(
                path,
                headers={"Last-Event-ID": first_frame.id},
            )
        assert replay.status_code == 200, replay.text
        assert f"id: {first_frame.id}\n" not in replay.text
        assert replay.text.count(f"id: {second_frame.id}\n") == 1
        assert replay.text.count(f"id: {terminal.id}\n") == 1
        replay_ids = [line.removeprefix("id: ") for line in replay.text.splitlines() if line.startswith("id: ")]
        assert replay_ids == [second_frame.id, terminal.id]

        async with httpx.AsyncClient(
            base_url=second_url,
            timeout=10,
            trust_env=False,
        ) as foreign:
            foreign_registration = await foreign.post(
                "/api/v1/auth/register",
                json={
                    "email": f"foreign-{uuid.uuid4().hex[:8]}@example.com",
                    "password": "very-strong-password-123",
                },
            )
            assert foreign_registration.status_code == 201
            denied = await foreign.get(path)
        assert denied.status_code == 404
        async with factory() as session:
            persisted_frames = (
                await session.execute(
                    text(
                        """SELECT count(*),
                                  count(*) FILTER (WHERE event_type='stream.end')
                           FROM run_events
                           WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                             AND thread_id=:thread_id AND run_id=:run_id"""
                    ),
                    {
                        "project_id": project_id,
                        "owner_user_id": user_id,
                        "thread_id": thread_id,
                        "run_id": run_id,
                    },
                )
            ).one()
        assert tuple(persisted_frames) == (3, 1)
    finally:
        if first is not None and first_log is not None:
            _stop_process(first, first_log)
        if second is not None and second_log is not None:
            _stop_process(second, second_log)
        await engine.dispose()
