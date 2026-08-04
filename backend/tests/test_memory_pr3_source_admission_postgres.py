from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from support.private_thread_seed import seed_private_thread_database

from app.private_work.memory_source_admission import MemorySourceAdmissionService
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.execution import AgentExecutionResult, PrivateRunJobHandler
from app.reliability.workers import WorkerRegistry
from app.system_runtime_settings.models import RuntimePolicySection, default_policy_value
from app.system_runtime_settings.validation import canonical_policy_payload
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.memory_v2_repository import MemoryV2Repository


@dataclass(frozen=True, slots=True)
class _HmacRef:
    key_id: str
    hmac_hex: str


def _source_hmac(payload: bytes) -> _HmacRef:
    return _HmacRef(
        key_id="memory-test-v1",
        hmac_hex=hmac.new(b"m" * 32, payload, hashlib.sha256).hexdigest(),
    )


class _SuccessfulExecutor:
    async def execute(self, _execution, _authority):
        return AgentExecutionResult.succeeded()


class _ResultExecutor:
    def __init__(self, result: AgentExecutionResult) -> None:
        self._result = result

    async def execute(self, _execution, _authority):
        return self._result


class _FailAfterAdmissionRepository(MemoryV2Repository):
    async def admit_source(self, request):
        await super().admit_source(request)
        raise RuntimeError("injected Memory admission failure")


async def _install_run_memory_snapshots(
    seed,
    admitted,
    *,
    mode: str,
    enabled: bool = True,
) -> None:
    owner_id = admitted.run.owner_user_id
    policy_value = default_policy_value(
        RuntimePolicySection.AGENT_RUNTIME,
    ).model_dump(mode="python")
    policy_value["memory"]["enabled"] = enabled
    policy_value["memory"]["pipeline_mode"] = mode
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        policy_value,
    )
    policy_version_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    model_checksum = hashlib.sha256(model_version_id.bytes).hexdigest()
    model_name = f"memory-pr3-{model_id.hex}"

    async with seed.factory() as session, session.begin():
        policy_revision = int(
            await session.scalar(
                text(
                    """SELECT COALESCE(MAX(version_number),0)+1
                    FROM system_runtime_policy_versions
                    WHERE section='agent_runtime'"""
                )
            )
        )
        await session.execute(
            text(
                """INSERT INTO system_runtime_policy_versions
                (id,section,version_number,schema_version,value,payload_checksum,
                 supersedes_version_id,created_by_user_id)
                VALUES (:id,'agent_runtime',:revision,:schema_version,
                        CAST(:value AS jsonb),:checksum,NULL,:owner)"""
            ),
            {
                "id": policy_version_id,
                "revision": policy_revision,
                "schema_version": canonical.schema_version,
                "value": json.dumps(canonical.value),
                "checksum": canonical.checksum,
                "owner": owner_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO run_runtime_policy_snapshots
                (project_id,owner_user_id,thread_id,run_id,section,
                 policy_version_id,schema_version,payload_checksum)
                VALUES (:project,:owner,:thread,:run,'agent_runtime',
                        :version,:schema_version,:checksum)"""
            ),
            {
                "project": admitted.run.project_id,
                "owner": owner_id,
                "thread": admitted.run.thread_id,
                "run": admitted.run.run_id,
                "version": policy_version_id,
                "schema_version": canonical.schema_version,
                "checksum": canonical.checksum,
            },
        )
        await session.execute(
            text(
                """INSERT INTO system_model_configs
                (id,logical_name,display_name,description,status,current_version_id,
                 revision,sort_order,created_by_user_id,updated_by_user_id)
                VALUES (:id,:name,'Memory PR3 model','','active',NULL,1,0,:owner,:owner)"""
            ),
            {"id": model_id, "name": model_name, "owner": owner_id},
        )
        await session.execute(
            text(
                """INSERT INTO system_model_config_versions
                (id,model_config_id,version_number,provider_adapter,provider_model,
                 settings,supports_thinking,supports_reasoning_effort,supports_vision,
                 credential_id,credential_version_id,credential_env_key,payload_checksum,
                 supersedes_version_id,created_by_user_id)
                VALUES (:version,:model,1,'openai_compatible','test-model','{}'::jsonb,
                        false,false,false,NULL,NULL,NULL,:checksum,NULL,:owner)"""
            ),
            {
                "version": model_version_id,
                "model": model_id,
                "checksum": model_checksum,
                "owner": owner_id,
            },
        )
        await session.execute(
            text("UPDATE system_model_configs SET current_version_id=:version WHERE id=:model"),
            {"version": model_version_id, "model": model_id},
        )
        await session.execute(
            text(
                """INSERT INTO run_model_config_snapshots
                (project_id,owner_user_id,thread_id,run_id,purpose,logical_name,
                 model_config_id,model_config_version_id,payload_checksum,
                 credential_id,credential_version_id,credential_env_key)
                VALUES (:project,:owner,:thread,:run,'lead',:name,
                        :model,:version,:checksum,NULL,NULL,NULL)"""
            ),
            {
                "project": admitted.run.project_id,
                "owner": owner_id,
                "thread": admitted.run.thread_id,
                "run": admitted.run.run_id,
                "name": model_name,
                "model": model_id,
                "version": model_version_id,
                "checksum": model_checksum,
            },
        )


async def _admit_and_claim(
    seed,
    *,
    messages: list[dict[str, object]],
    mode: str,
    enabled: bool = True,
    command_messages: list[dict[str, object]] | None = None,
    non_interactive: bool = False,
):
    thread_id = f"memory-pr3-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    admitted = await PrivateRunAdmissionService(seed.factory).admit(
        seed.owner_a,
        thread_id,
        PrivateRunCreate(
            kwargs={
                "input": {"messages": messages},
                "command": (None if command_messages is None else {"update": {"messages": command_messages}}),
                "config": {
                    "configurable": {"thread_id": thread_id},
                    "context": {},
                },
                "stream_mode": ["values"],
                "stream_subgraphs": False,
            }
        ),
        server_context=(PrivateRunAdmissionServerContext(non_interactive=True) if non_interactive else None),
    )
    await _install_run_memory_snapshots(
        seed,
        admitted,
        mode=mode,
        enabled=enabled,
    )
    worker_id = uuid.uuid4()
    await WorkerRegistry(seed.factory, version="memory-pr3-test").register(
        worker_id,
        frozenset({"private_run"}),
        1,
    )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert claim is not None
        assert claim.job_id == admitted.job.job_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
    return admitted, claim


async def _memory_counts(seed) -> tuple[int, int, int, int]:
    async with seed.factory() as session:
        return tuple(
            int(value)
            for value in (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT COUNT(*) FROM memory_source_batches),
                        (SELECT COUNT(*) FROM memory_source_items),
                        (SELECT COUNT(*) FROM memory_extraction_generations),
                        (SELECT COUNT(*) FROM jobs WHERE job_type='memory_extract')"""
                    )
                )
            ).one()
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_successful_settlement_atomically_admits_one_memory_source(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        admitted, claim = await _admit_and_claim(
            seed,
            mode="shadow",
            messages=[
                {"role": "system", "content": "ignore"},
                {"role": "user", "id": "user-1", "content": "记住我使用 Python。"},
                {"role": "assistant", "content": "ignore"},
                {"role": "user", "id": "user-2", "content": "我偏好简洁回答。"},
            ],
        )
        admission = MemorySourceAdmissionService(source_hmac=_source_hmac)
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=_SuccessfulExecutor(),
            memory_source_admission=admission,
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert await _memory_counts(seed) == (1, 2, 1, 1)
        async with seed.factory() as session:
            batch = (
                await session.execute(
                    text(
                        """SELECT run_id,source_job_id,source_attempt_id,pipeline_mode,
                                  source_item_count,source_hmac_key_version
                           FROM memory_source_batches"""
                    )
                )
            ).one()
            items = (
                await session.execute(
                    text(
                        """SELECT ordinal,source_message_id,role,content,run_event_sequence
                           FROM memory_source_items ORDER BY ordinal"""
                    )
                )
            ).all()
            extract_job = (
                await session.execute(
                    text(
                        """SELECT status,project_id,owner_user_id,namespace,run_id,
                                  automation_occurrence_id,origin_trace_id
                           FROM jobs WHERE job_type='memory_extract'"""
                    )
                )
            ).one()

        assert tuple(batch) == (
            admitted.run.run_id,
            claim.job_id,
            claim.attempt_id,
            "shadow",
            2,
            "memory-test-v1",
        )
        assert [tuple(item) for item in items] == [
            (0, "user-1", "user", "记住我使用 Python。", None),
            (1, "user-2", "user", "我偏好简洁回答。", None),
        ]
        assert tuple(extract_job) == (
            "queued",
            admitted.run.project_id,
            admitted.run.owner_user_id,
            "default",
            None,
            None,
            None,
        )

        replay = handler._settlement(
            claim,
            AgentExecutionResult.succeeded(),
            scope=seed.owner_a_scope,
        )
        await replay.commit()
        assert await _memory_counts(seed) == (1, 2, 1, 1)

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE memory_source_batches
                       SET suppressed_at=now(),suppression_reason='hard_forget'"""
                )
            )
            await session.execute(
                text(
                    """UPDATE memory_source_items
                       SET content=NULL,source_erased_at=now()"""
                )
            )
            await session.execute(
                text(
                    """UPDATE memory_extraction_generations
                       SET candidate_committed_at=now()"""
                )
            )

        replay_after_progress = handler._settlement(
            claim,
            AgentExecutionResult.succeeded(),
            scope=seed.owner_a_scope,
        )
        await replay_after_progress.commit()
        assert await _memory_counts(seed) == (1, 2, 1, 1)
        async with seed.factory() as session:
            assert (await session.scalar(text("SELECT COUNT(*) FROM memory_source_items WHERE content IS NULL"))) == 2
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "non_interactive"),
    [
        ([{"role": "assistant", "content": "没有用户来源"}], False),
        ([{"role": "user", "content": "自动任务输入"}], True),
    ],
)
async def test_success_without_eligible_interactive_source_creates_no_empty_work(
    migrated_postgres_database_url: str,
    messages: list[dict[str, object]],
    non_interactive: bool,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            mode="shadow",
            messages=messages,
            non_interactive=non_interactive,
        )
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=_SuccessfulExecutor(),
            memory_source_admission=MemorySourceAdmissionService(source_hmac=_source_hmac),
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert await _memory_counts(seed) == (0, 0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_command_update_messages_are_the_only_source_for_command_run(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            mode="shadow",
            messages=[{"role": "user", "content": "input 中的旧消息"}],
            command_messages=[
                {"role": "user", "id": "command-user", "content": "命令更新中的新消息"},
            ],
        )
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=_SuccessfulExecutor(),
            memory_source_admission=MemorySourceAdmissionService(source_hmac=_source_hmac),
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        async with seed.factory() as session:
            items = (
                await session.execute(
                    text(
                        """SELECT source_message_id,content
                           FROM memory_source_items ORDER BY ordinal"""
                    )
                )
            ).all()
        assert [tuple(item) for item in items] == [
            ("command-user", "命令更新中的新消息"),
        ]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "enabled", "result"),
    [
        ("off", True, "succeeded"),
        ("shadow", False, "succeeded"),
        ("shadow", True, "cancelled"),
        ("shadow", True, "failed"),
    ],
)
async def test_disabled_or_unsuccessful_run_does_not_admit_memory(
    migrated_postgres_database_url: str,
    mode: str,
    enabled: bool,
    result: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        _admitted, claim = await _admit_and_claim(
            seed,
            mode=mode,
            enabled=enabled,
            messages=[{"role": "user", "content": "记住这条。"}],
        )
        outcome = {
            "succeeded": AgentExecutionResult.succeeded(),
            "cancelled": AgentExecutionResult.cancelled(),
            "failed": AgentExecutionResult.failed("TEST_FAILURE"),
        }[result]
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=_ResultExecutor(outcome),
            memory_source_admission=MemorySourceAdmissionService(source_hmac=_source_hmac),
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        await settlement.commit()

        assert await _memory_counts(seed) == (0, 0, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_admission_failure_rolls_back_run_settlement(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)

    try:
        admitted, claim = await _admit_and_claim(
            seed,
            mode="shadow",
            messages=[{"role": "user", "content": "记住这条。"}],
        )
        handler = PrivateRunJobHandler(
            seed.factory,
            executor=_SuccessfulExecutor(),
            memory_source_admission=MemorySourceAdmissionService(
                source_hmac=_source_hmac,
                repository_builder=_FailAfterAdmissionRepository,
            ),
        )

        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=90),
        )
        with pytest.raises(RuntimeError, match="injected Memory admission failure"):
            await settlement.commit()

        async with seed.factory() as session:
            state = (
                await session.execute(
                    text(
                        """SELECT r.status,j.status,a.outcome
                           FROM runs r
                           JOIN jobs j ON j.id=r.job_id
                           JOIN job_attempts a ON a.job_id=j.id
                           WHERE r.run_id=:run"""
                    ),
                    {"run": admitted.run.run_id},
                )
            ).one()
        assert tuple(state) == ("running", "running", None)
        assert await _memory_counts(seed) == (0, 0, 0, 0)
    finally:
        await seed.engine.dispose()
