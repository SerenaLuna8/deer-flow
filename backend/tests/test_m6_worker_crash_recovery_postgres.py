from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from support.m4_private_threads import seed_m4_thread_database

from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.jobs import AdmittedJobRecord, private_run_idempotency_key
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.models import AssetKind, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.events.models import StreamFrame
from deerflow.runtime.events.stream import PostgresStreamBridge

_PROCESS_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class _ForeignScopeSentinel:
    project_id: uuid.UUID


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="test-audit-v1",
        _keys={"test-audit-v1": b"a" * 32},
    )


def _project_context(context: PrivateWorkContext) -> ProjectContext:
    return ProjectContext(
        user_id=context.user_id,
        project_id=context.project_id,
        membership_id=context.membership_id,
        role=context.role,
        capabilities=context.capabilities,
        membership_version=context.membership_version,
        request_id=context.request_id,
    )


def _config(database_url: str) -> str:
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
  shutdown_grace_seconds: 2
  default_max_attempts: 3
  retry_initial_seconds: 1
  retry_max_seconds: 2
scheduler:
  enabled: false
"""


def _child_environment(
    tmp_path: Path,
    database_url: str,
    barrier: Path,
    release: Path,
) -> dict[str, str]:
    config = tmp_path / "config.yaml"
    config.write_text(_config(database_url), encoding="utf-8")
    environment = dict(os.environ)
    backend_root = Path(__file__).resolve().parents[1]
    environment.update(
        {
            "DEER_FLOW_CONFIG_PATH": str(config),
            "DEER_FLOW_HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (str(backend_root), environment.get("PYTHONPATH", "")),
                )
            ),
            "M6_PROCESS_BARRIER": str(barrier),
            "M6_PROCESS_RELEASE": str(release),
            "DEER_FLOW_AUDIT_ACTIVE_KEY_ID": "test-audit-v1",
            "DEER_FLOW_AUDIT_KEYRING_JSON": ('{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}'),
        }
    )
    return environment


def _start_worker(
    tmp_path: Path,
    database_url: str,
    barrier: Path,
    release: Path,
    name: str,
) -> tuple[subprocess.Popen[bytes], object]:
    log = (tmp_path / f"{name}.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "tests/support/m6_process_child.py", "worker"],
        cwd=Path(__file__).resolve().parents[1],
        env=_child_environment(tmp_path, database_url, barrier, release),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return process, log


def _stop_process(process: subprocess.Popen[bytes], log: object) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        else:
            process.wait(timeout=1)
    finally:
        log.close()  # type: ignore[attr-defined]


def _barrier_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _wait_for_event(
    path: Path,
    event: str,
    *,
    process: subprocess.Popen[bytes] | None = None,
    pid: int | None = None,
    timeout: float = _PROCESS_TIMEOUT,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"child pid={process.pid} exited with {process.returncode} before {event!r}")
        for item in _barrier_events(path):
            if item.get("event") == event and (pid is None or item.get("pid") == pid):
                return item
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {event!r} from pid={pid}")


async def _enqueue_private_job(
    seed,
    *,
    context: PrivateWorkContext,
    agent_id: uuid.UUID,
    retry_safety: str,
    available_at: datetime | None = None,
):
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(agent_id, "project"),
        )

    resolved = await ProjectAssetResolver(seed.factory).resolve_project_asset_snapshot(
        _project_context(context),
        AssetSelection(AssetKind.AGENT, agent_id),
    )
    run = await RunSnapshotRepository(seed.factory).create_run_with_snapshot(
        context,
        thread_id,
        PrivateRunCreate(
            run_id=run_id,
            kwargs={
                "input": {"messages": [{"role": "user", "content": "gate"}]},
                "config": {"configurable": {"thread_id": thread_id}},
                "stream_mode": ["values"],
            },
        ),
        resolved,
    )

    keyring = _keyring()
    quota = ProjectQuotaEnforcer(
        QuotaService(
            seed.factory,
            QuotaConfig(),
            source_ref_hasher=keyring,
        )
    )
    audit_service = AuditService(seed.factory, keyring)
    audit = OperationalAuditSink(
        audit_service,
        process_context=_bind_gateway_audit_process(audit_service),
    )
    async with seed.factory() as session, session.begin():
        job_id = await JobRepository(
            session,
            owner_ref_hasher=keyring.job_owner_ref,
        ).enqueue(
            EnqueueJob(
                job_type="private_run",
                scope=JobScope(context.project_id, str(context.user_id)),
                idempotency_key=private_run_idempotency_key(run_id),
                run_id=run_id,
                occurrence_id=None,
                max_attempts=3,
                retry_safety=retry_safety,
                available_at=available_at,
            )
        )
        row = await session.get(JobRow, job_id)
        assert row is not None
        admitted = AdmittedJobRecord(
            job_id=row.id,
            job_type="private_run",
            project_id=row.project_id,
            owner_user_id=str(row.owner_user_id),
            run_id=str(row.run_id),
            idempotency_key=row.idempotency_key,
            status=row.status,
        )
        run = await PrivateRunRepository(session).attach_job(
            scope=context.resource_scope,
            run_id=run_id,
            job_id=job_id,
        )
        await quota.reserve_concurrent_run(session, context, run)
        await audit.run_admitted(session, context, run, admitted)
    return job_id, thread_id, run_id


async def _admit_private_job(seed, *, retry_safety: str = "safe"):
    return await _enqueue_private_job(
        seed,
        context=seed.owner_a,
        agent_id=seed.project_agent_id,
        retry_safety=retry_safety,
    )


async def _seed_foreign_scope_sentinel(seed) -> _ForeignScopeSentinel:
    context = seed.project_b_owner_a
    await _enqueue_private_job(
        seed,
        context=context,
        agent_id=seed.project_b_agent_id,
        retry_safety="safe",
        available_at=datetime.now(UTC) + timedelta(days=1),
    )

    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                assistant_id=str(seed.project_b_agent_id),
                status="success",
            ),
        )
    bridge = PostgresStreamBridge(seed.factory)
    await bridge.publish_frame(
        context.resource_scope,
        thread_id,
        run_id,
        StreamFrame(event="updates", data={"foreign": True}),
    )
    await bridge.publish_terminal(
        context.resource_scope,
        thread_id,
        run_id,
        status="success",
    )
    return _ForeignScopeSentinel(project_id=context.project_id)


async def _foreign_scope_snapshot(seed, sentinel: _ForeignScopeSentinel) -> dict[str, tuple[tuple[object, ...], ...]]:
    queries = {
        "project": """SELECT id,slug,display_name,status,is_suspended,membership_version,updated_at
            FROM projects WHERE id=:project_id""",
        "runs": """SELECT run_id,thread_id,owner_user_id,status,job_id,finalization_status,updated_at
            FROM runs WHERE project_id=:project_id ORDER BY run_id""",
        "jobs": """SELECT id,run_id,owner_user_id,status,available_at,attempt_count,retry_safety,
            lease_owner_id,lease_expires_at,public_error_code,updated_at
            FROM jobs WHERE project_id=:project_id ORDER BY id""",
        "frames": """SELECT id,thread_id,run_id,event_type,category,content,event_metadata,seq
            FROM run_events WHERE project_id=:project_id ORDER BY id""",
        "quota_counters": """SELECT dimension,bucket,used,reserved,version,updated_at
            FROM project_usage_counters WHERE project_id=:project_id ORDER BY dimension,bucket""",
        "quota_ledger": """SELECT id,dimension,delta,bucket,source_kind,source_ref_key_id,
            source_ref_hmac,idempotency_key,request_id,occurred_at
            FROM project_usage_ledger WHERE project_id=:project_id ORDER BY id""",
        "audit": """SELECT id,actor_user_id,actor_process,action,target_kind,target_ref_key_id,
            target_ref_hmac,outcome,request_id,job_id,metadata_json,occurred_at
            FROM audit_logs WHERE project_id=:project_id ORDER BY id""",
    }
    async with seed.factory() as session:
        snapshot = {name: tuple(tuple(row) for row in (await session.execute(text(statement), {"project_id": sentinel.project_id})).all()) for name, statement in queries.items()}
    assert all(snapshot[name] for name in queries)
    return snapshot


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_sigkill_is_taken_over_without_duplicate_terminal(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    barrier = tmp_path / "worker-events.jsonl"
    release = tmp_path / "release"
    first = second = None
    first_log = second_log = None
    try:
        foreign = await _seed_foreign_scope_sentinel(seed)
        foreign_before = await _foreign_scope_snapshot(seed, foreign)
        job_id, _thread_id, run_id = await _admit_private_job(seed)
        first, first_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            "worker-first",
        )
        first_lease = await _wait_for_event(
            barrier,
            "leased",
            process=first,
            pid=first.pid,
        )
        assert first_lease["job_id"] == str(job_id)

        os.kill(first.pid, signal.SIGKILL)
        assert first.wait(timeout=5) == -signal.SIGKILL

        second, second_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            "worker-second",
        )
        second_lease = await _wait_for_event(
            barrier,
            "leased",
            process=second,
            pid=second.pid,
            timeout=25,
        )
        assert second_lease["job_id"] == str(job_id)
        release.touch()
        await _wait_for_event(
            barrier,
            "settled",
            process=second,
            pid=second.pid,
        )

        async with seed.factory() as session:
            job = await session.get(JobRow, job_id)
            attempts = (await session.execute(select(JobAttemptRow).where(JobAttemptRow.job_id == job_id))).scalars().all()
            frames = (
                (
                    await session.execute(
                        select(RunEventRow).where(
                            RunEventRow.run_id == run_id,
                            RunEventRow.category == "stream",
                        )
                    )
                )
                .scalars()
                .all()
            )
            run = await session.get(RunRow, run_id)

        assert job is not None and job.status == "succeeded"
        assert run is not None and run.status == "success"
        assert [attempt.outcome for attempt in attempts].count("succeeded") == 1
        assert [attempt.outcome for attempt in attempts].count("lease_lost") == 1
        assert sum(frame.event_type == "stream.end" for frame in frames) == 1
        assert all(frame.project_id == seed.owner_a.project_id for frame in frames)
        assert all(frame.owner_user_id == str(seed.owner_a.user_id) for frame in frames)
        assert await _foreign_scope_snapshot(seed, foreign) == foreign_before
    finally:
        if first is not None and first_log is not None:
            _stop_process(first, first_log)
        if second is not None and second_log is not None:
            _stop_process(second, second_log)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("retry_safety", ["unsafe", "unknown"])
async def test_expired_ambiguous_job_becomes_dead_and_is_never_replayed(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    retry_safety: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    barrier = tmp_path / f"{retry_safety}-events.jsonl"
    release = tmp_path / f"{retry_safety}-release"
    first = second = None
    first_log = second_log = None
    state = None
    try:
        job_id, _thread_id, _run_id = await _admit_private_job(
            seed,
            retry_safety=retry_safety,
        )
        first, first_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            f"{retry_safety}-first",
        )
        await _wait_for_event(
            barrier,
            "leased",
            process=first,
            pid=first.pid,
        )
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=5)

        second, second_log = _start_worker(
            tmp_path,
            migrated_postgres_database_url,
            barrier,
            release,
            f"{retry_safety}-second",
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            async with seed.factory() as session:
                state = await session.scalar(select(JobRow.status).where(JobRow.id == job_id))
            if state == "dead":
                break
            await asyncio.sleep(0.1)
        assert state == "dead"
        assert len([item for item in _barrier_events(barrier) if item.get("event") == "leased" and item.get("job_id") == str(job_id)]) == 1
        async with seed.factory() as session:
            projection = (
                await session.execute(
                    text(
                        """SELECT j.status,j.attempt_count,j.public_error_code,r.status
                           FROM jobs j JOIN runs r ON r.job_id=j.id
                           WHERE j.id=:job_id"""
                    ),
                    {"job_id": job_id},
                )
            ).one()
        assert tuple(projection) == (
            "dead",
            1,
            "SIDE_EFFECT_STATE_UNKNOWN",
            "error",
        )
    finally:
        if first is not None and first_log is not None:
            _stop_process(first, first_log)
        if second is not None and second_log is not None:
            _stop_process(second, second_log)
        await seed.engine.dispose()
