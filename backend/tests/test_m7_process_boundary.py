"""Final M7 process-boundary release gate."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_m6_gateway_reconnect_process import (
    _create_project_thread,
    _start_gateway,
    _stop_process,
    _wait_ready,
)
from test_m6_worker_crash_recovery_postgres import (
    _barrier_events,
    _start_worker,
    _wait_for_event,
)

from app.automations.ownership import AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY

_PROCESS_TIMEOUT = 60.0


def test_process_probe_records_all_authority_boundaries() -> None:
    from support import m6_process_child

    source = inspect.getsource(m6_process_child)
    for event in (
        '"role": "worker"',
        '"event": "claim"',
        '"event": "graph_execution"',
        '"event": "stream_append"',
        '"event": "terminal_append"',
    ):
        assert event in source


def test_gateway_and_scheduler_cannot_import_worker_graph_execution() -> None:
    from app.gateway import deps as gateway_deps
    from app.scheduler import app as scheduler_app
    from app.scheduler import service as scheduler_service

    source = "\n".join(
        (
            inspect.getsource(gateway_deps),
            inspect.getsource(scheduler_app),
            inspect.getsource(scheduler_service),
        )
    )
    assert "RunAgentPrivateExecutor" not in source
    assert "PrivateRunJobHandler" not in source
    assert "run_agent" not in source


def _scheduler_config(database_url: str) -> str:
    return f"""\
log_level: warning
models: []
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
database:
  url: {database_url}
memory:
  token_counting: char
worker:
  enabled: true
  poll_interval_seconds: 0.05
  lease_seconds: 15
  heartbeat_seconds: 4
  max_concurrent_jobs: 1
scheduler:
  enabled: true
  poll_interval_seconds: 1
  max_concurrent_runs: 1
"""


def _scheduler_environment(tmp_path: Path, database_url: str) -> dict[str, str]:
    config = tmp_path / "scheduler-config.yaml"
    config.write_text(_scheduler_config(database_url), encoding="utf-8")
    backend_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "DEER_FLOW_CONFIG_PATH": str(config),
            "DEER_FLOW_HOME": str(tmp_path / "scheduler-home"),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(backend_root), environment.get("PYTHONPATH", "")))),
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID": "test-audit-v1",
            "DEER_FLOW_AUDIT_KEYRING_JSON": ('{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}'),
        }
    )
    return environment


def _start_scheduler(
    tmp_path: Path,
    database_url: str,
) -> tuple[subprocess.Popen[bytes], object]:
    log = (tmp_path / "scheduler.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.scheduler.app"],
        cwd=Path(__file__).resolve().parents[1],
        env=_scheduler_environment(tmp_path, database_url),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return process, log


async def _wait_scheduler_owned(
    process: subprocess.Popen[bytes],
    database_url: str,
) -> int:
    engine = create_async_engine(database_url)
    deadline = time.monotonic() + _PROCESS_TIMEOUT
    high = (AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY >> 32) & 0xFFFF_FFFF
    low = AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY & 0xFFFF_FFFF
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Scheduler pid={process.pid} exited with {process.returncode}")
            async with engine.connect() as connection:
                backend_pid = await connection.scalar(
                    text(
                        """SELECT pid FROM pg_locks
                        WHERE locktype='advisory' AND granted
                          AND classid=:classid AND objid=:objid AND objsubid=1"""
                    ),
                    {"classid": high, "objid": low},
                )
            if backend_pid is not None:
                return int(backend_pid)
            await asyncio.sleep(0.05)
    finally:
        await engine.dispose()
    raise AssertionError(f"Scheduler pid={process.pid} did not acquire ownership")


async def _job_snapshot(database_url: str, run_id: str) -> tuple[object, ...]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT j.id,j.status,j.lease_owner_id,w.id,w.draining,r.status
                        FROM jobs j
                        JOIN runs r ON r.job_id=j.id
                        LEFT JOIN worker_nodes w ON w.id=j.lease_owner_id
                        WHERE r.run_id=:run_id"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        return tuple(row)
    finally:
        await engine.dispose()


async def _stream_rows(
    database_url: str,
    *,
    project_id: str,
    owner_user_id: str,
    thread_id: str,
    run_id: str,
) -> tuple[tuple[int, str, str, str, str], ...]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    """SELECT seq,event_type,project_id::text,owner_user_id::text,thread_id
                    FROM run_events
                    WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                      AND thread_id=:thread_id AND run_id=:run_id AND category='stream'
                    ORDER BY seq"""
                ),
                {
                    "project_id": project_id,
                    "owner_user_id": owner_user_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                },
            )
        return tuple((int(seq), str(event), str(project), str(owner), str(thread)) for seq, event, project, owner, thread in rows)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_real_process_roles_keep_graph_worker_only_and_reconnect_isolated(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    """Run Gateway, Scheduler and Worker against one disposable M7 database."""

    scheduler = gateway = replacement = worker = None
    scheduler_log = gateway_log = replacement_log = worker_log = None
    barrier = tmp_path / "m7-process-events.jsonl"
    release = tmp_path / "m7-process-release"
    roles: list[dict[str, object]] = []
    try:
        scheduler, scheduler_log = _start_scheduler(
            tmp_path,
            migrated_postgres_database_url,
        )
        scheduler_backend_pid = await _wait_scheduler_owned(
            scheduler,
            migrated_postgres_database_url,
        )
        roles.append(
            {
                "event": "ownership",
                "role": "scheduler",
                "pid": scheduler.pid,
                "backend_pid": scheduler_backend_pid,
            }
        )

        gateway, gateway_log, gateway_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "m7-gateway-first",
        )
        await _wait_ready(gateway, gateway_url)
        async with httpx.AsyncClient(
            base_url=gateway_url,
            timeout=15,
            trust_env=False,
        ) as owner:
            owner_user_id, project_id, thread_id = await _create_project_thread(owner)
            cookies = httpx.Cookies(owner.cookies)
            csrf = owner.cookies.get("csrf_token")
            assert csrf
            admitted = await owner.post(
                f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs",
                headers={"X-CSRF-Token": csrf},
                json={
                    "input": {"messages": [{"role": "user", "content": "M7 process boundary"}]},
                    "config": {"configurable": {"thread_id": thread_id}},
                },
            )
        assert admitted.status_code == 200, admitted.text
        run_id = admitted.json()["run_id"]
        roles.append({"event": "admission", "role": "gateway", "pid": gateway.pid})

        worker, worker_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            "m7-worker",
        )
        claim = await _wait_for_event(barrier, "claim", process=worker, pid=worker.pid)
        graph = await _wait_for_event(
            barrier,
            "graph_execution",
            process=worker,
            pid=worker.pid,
        )
        assert claim["role"] == graph["role"] == "worker"
        assert claim["job_id"] == graph["job_id"]

        job_id, job_status, lease_owner_id, worker_id, draining, run_status = await _job_snapshot(migrated_postgres_database_url, run_id)
        assert str(job_id) == claim["job_id"]
        assert (job_status, lease_owner_id, worker_id, draining, run_status) == (
            "running",
            worker_id,
            worker_id,
            False,
            "running",
        )

        release.touch()
        await _wait_for_event(
            barrier,
            "terminal_append",
            process=worker,
            pid=worker.pid,
        )
        await _wait_for_event(barrier, "settled", process=worker, pid=worker.pid)
        roles.extend(_barrier_events(barrier))

        rows = await _stream_rows(
            migrated_postgres_database_url,
            project_id=project_id,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
        )
        assert [row[1] for row in rows] == ["updates", "stream.end"]
        assert all(row[2:] == (project_id, owner_user_id, thread_id) for row in rows)
        first_event_id, terminal_event_id = str(rows[0][0]), str(rows[1][0])

        path = f"/api/projects/{project_id}/private-work/threads/{thread_id}/runs/{run_id}/stream"
        async with httpx.AsyncClient(
            base_url=gateway_url,
            cookies=cookies,
            timeout=15,
            trust_env=False,
        ) as first_owner:
            initial = await first_owner.get(path)
        assert initial.status_code == 200, initial.text
        assert [line.removeprefix("id: ") for line in initial.text.splitlines() if line.startswith("id: ")] == [first_event_id, terminal_event_id]

        _stop_process(gateway, gateway_log)
        gateway = gateway_log = None
        replacement, replacement_log, replacement_url = _start_gateway(
            tmp_path,
            migrated_postgres_database_url,
            "m7-gateway-second",
        )
        await _wait_ready(replacement, replacement_url)
        async with httpx.AsyncClient(
            base_url=replacement_url,
            cookies=cookies,
            timeout=15,
            trust_env=False,
        ) as reconnected:
            replay = await reconnected.get(
                path,
                headers={"Last-Event-ID": first_event_id},
            )
        assert replay.status_code == 200, replay.text
        assert f"id: {first_event_id}\n" not in replay.text
        assert replay.text.count(f"id: {terminal_event_id}\n") == 1

        async with httpx.AsyncClient(
            base_url=replacement_url,
            timeout=15,
            trust_env=False,
        ) as foreign:
            registered = await foreign.post(
                "/api/v1/auth/register",
                json={
                    "email": f"m7-foreign-{os.getpid()}@example.com",
                    "password": "very-strong-password-123",
                },
            )
            assert registered.status_code == 201
            denied = await foreign.get(path)
        assert denied.status_code == 404

        graph_pids = {int(item["pid"]) for item in roles if item.get("event") == "graph_execution"}
        assert graph_pids == {worker.pid}
        assert scheduler.pid not in graph_pids
        assert replacement.pid not in graph_pids
        assert all(item.get("role") == "worker" for item in roles if item.get("event") in {"claim", "graph_execution", "stream_append", "terminal_append"})
    finally:
        for process, log in (
            (worker, worker_log),
            (gateway, gateway_log),
            (replacement, replacement_log),
            (scheduler, scheduler_log),
        ):
            if process is not None and log is not None:
                _stop_process(process, log)
