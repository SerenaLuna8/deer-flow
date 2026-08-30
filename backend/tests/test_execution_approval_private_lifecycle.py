from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.execution_approval_lifecycle import (
    claimed_execution_absolute_deadline,
)
from app.private_work.privacy_center import PrivacyCenterService
from app.private_work.retention_purge import RetentionPurger
from app.private_work.run_skill_tree_orphan_reaper import RunSkillTreeOrphanReaper
from app.quotas.integration import ProjectQuotaEnforcer
from app.worker.retention import RetentionPurgeJobHandler


class _Stream:
    def __init__(self, rows=()) -> None:
        self._rows = tuple(rows)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for row in self._rows:
            yield row

    async def close(self) -> None:
        return None


class _ApprovalExportSession:
    def __init__(self, plan, result) -> None:
        self._plan = plan
        self._result = result
        self.approval_sql: list[str] = []

    async def stream_scalars(self, statement):
        del statement
        return _Stream()

    async def stream(self, statement):
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        if "execution_approval_requests" in sql:
            self.approval_sql.append(sql)
            return _Stream((self._plan,))
        if "execution_approval_result_receipts" in sql:
            self.approval_sql.append(sql)
            return _Stream((self._result,))
        return _Stream()


class _Transaction:
    def __init__(self) -> None:
        self.rolled_back = False

    @property
    def is_active(self) -> bool:
        return not self.rolled_back

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_privacy_export_v3_allowlists_approval_plan_and_result() -> None:
    now = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    approval_id = uuid.uuid4()
    session = _ApprovalExportSession(
        SimpleNamespace(
            id=approval_id,
            thread_id="thread-a",
            source_run_id="source-run",
            tool_call_id="call-a",
            kind="local_bash",
            status="finished",
            decision="allow_once",
            description="count characters",
            requested_command="python count.py",
            timeout_seconds=60,
            source_agent_path=["lead"],
            continuation_run_id="continuation-run",
            expires_at=now + timedelta(minutes=5),
            decided_at=now,
            terminal_at=now + timedelta(seconds=2),
            created_at=now,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            approval_id=approval_id,
            thread_id="thread-a",
            outcome="finished",
            exit_code=0,
            stdout="42",
            stderr=None,
            result_text=None,
            reason_code=None,
            stdout_truncated=False,
            stderr_truncated=False,
            result_text_truncated=None,
            created_at=now + timedelta(seconds=2),
        ),
    )
    service = PrivacyCenterService(session)
    transaction = _Transaction()

    lines = [
        json.loads(line)
        async for line in service._stream_export(
            transaction,
            project=SimpleNamespace(
                id=uuid.uuid4(),
                slug="former",
                display_name="Former",
                icon="folder",
            ),
            membership=SimpleNamespace(
                user_id=str(uuid.uuid4()),
                status="left",
                ended_at=now,
                retention_until=now + timedelta(days=1),
            ),
            generated_at=now,
        )
    ]

    assert transaction.rolled_back
    assert lines[0]["schema_version"] == 3
    assert [line["record_type"] for line in lines] == [
        "manifest",
        "execution_approval_plan",
        "execution_approval_result",
    ]
    assert lines[1]["data"]["requested_command"] == "python count.py"
    assert lines[2]["data"]["stdout"] == "42"
    assert lines[2]["data"]["stderr"] is None
    assert lines[2]["data"]["result_text"] is None

    rendered = b"".join(json.dumps(line, ensure_ascii=False).encode() for line in lines).decode()
    sql = "\n".join(session.approval_sql)
    for forbidden in (
        "effective_command",
        "provider_policy",
        "environment_keys",
        "decided_by_user_id",
        "command_digest",
        "result_digest",
    ):
        assert forbidden not in rendered
        assert forbidden not in sql


def test_claimed_retention_deadline_is_frozen_not_renewable_lease_time() -> None:
    claimed_at = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    row = SimpleNamespace(
        claimed_at=claimed_at,
        command_private_json={"plan": {"timeout_seconds": 60}},
        expires_at=claimed_at + timedelta(minutes=5),
        updated_at=claimed_at,
    )

    assert claimed_execution_absolute_deadline(row) == (claimed_at + timedelta(seconds=90))


class _DeferralJobs:
    def __init__(self, row) -> None:
        self.row = row

    async def retry_or_dead_result(self, *args, now, **kwargs):
        del args, kwargs
        self.row.status = "retry_wait"
        self.row.available_at = now + timedelta(seconds=2)
        return SimpleNamespace(changed=True)


class _MountOwnerReconciler:
    def __init__(self) -> None:
        self.called = False

    async def reconcile_once(self) -> None:
        self.called = True


@pytest.mark.asyncio
async def test_retention_worker_reconciles_mount_owners_before_phase_b() -> None:
    reconciler = _MountOwnerReconciler()
    handler = object.__new__(RetentionPurgeJobHandler)
    handler._mount_owner_reconciler = reconciler

    async def not_a_project_purge(_claim) -> bool:  # noqa: ANN001
        return False

    handler._knowledge_purge_admitted = not_a_project_purge

    await handler(
        SimpleNamespace(job_type="retention_purge"),
        object(),
    )

    assert reconciler.called


def test_retention_worker_constructor_rejects_mount_proof_bypasses() -> None:
    common = {
        "audit": object.__new__(TrustedOperationAuditSink),
        "approval_audit": object(),
        "quota": object.__new__(ProjectQuotaEnforcer),
    }

    with pytest.raises(TypeError, match="mount-owner reconciler"):
        RetentionPurgeJobHandler(
            None,
            mount_owner_reconciler=_MountOwnerReconciler(),  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="purge repository"):
        RetentionPurgeJobHandler(
            None,
            mount_owner_reconciler=object.__new__(RunSkillTreeOrphanReaper),
            repository=object(),  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="purge repository"):
        RetentionPurger(
            None,
            repository=object(),  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_retention_external_authority_deferral_never_spends_failure_budget() -> None:
    now = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    absolute_deadline = now + timedelta(minutes=10)
    row = SimpleNamespace(
        max_attempts=5,
        attempt_count=0,
        status="queued",
        available_at=now,
        updated_at=now,
    )
    handler = object.__new__(RetentionPurgeJobHandler)
    handler._retry_initial_seconds = 2
    handler._retry_max_seconds = 300
    jobs = _DeferralJobs(row)
    claim = SimpleNamespace(job_id=uuid.uuid4(), lease_token="lease")

    for claim_number in range(1, 13):
        row.attempt_count += 1
        row.status = "running"
        await handler._defer_for_execution_approval(
            jobs,
            row,
            claim,
            now=now + timedelta(seconds=claim_number),
            retry_after=absolute_deadline,
        )
        assert row.status == "retry_wait"
        assert row.max_attempts - row.attempt_count == 5
        assert row.available_at == absolute_deadline + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_retention_blocker_without_deadline_uses_finite_retry_budget() -> None:
    now = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    row = SimpleNamespace(
        max_attempts=1,
        attempt_count=1,
        status="running",
        available_at=now,
        updated_at=now,
    )

    class _DeadJobs(_DeferralJobs):
        async def retry_or_dead_result(self, *args, now, **kwargs):
            del args, now, kwargs
            self.row.status = "dead"
            return SimpleNamespace(changed=True)

    handler = object.__new__(RetentionPurgeJobHandler)
    handler._retry_initial_seconds = 2
    handler._retry_max_seconds = 300
    await handler._defer_for_execution_approval(
        _DeadJobs(row),
        row,
        SimpleNamespace(job_id=uuid.uuid4(), lease_token="lease"),
        now=now,
        retry_after=None,
    )

    assert row.status == "dead"
    assert row.max_attempts == 1
