from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from app.private_work.memory_source_admission import MemorySourceAdmissionService
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.execution import AgentExecutionResult, PrivateRunJobHandler
from app.reliability.workers import WorkerRegistry
from app.system_runtime_settings.models import RuntimePolicySection, default_policy_value
from app.system_runtime_settings.validation import canonical_policy_payload
from app.worker.service import JobLeaseAuthority
from deerflow.persistence.jobs.sql import JobRepository


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


async def _install_snapshots(
    seed,
    admitted,
    *,
    mode: str,
    model_purpose: str,
    make_policy_current: bool,
) -> None:
    owner_id = admitted.run.owner_user_id
    policy_value = default_policy_value(
        RuntimePolicySection.AGENT_RUNTIME,
    ).model_dump(mode="python")
    policy_value["memory"]["enabled"] = True
    policy_value["memory"]["pipeline_mode"] = mode
    policy_version_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    model_checksum = hashlib.sha256(model_version_id.bytes).hexdigest()
    model_name = f"memory-pr4-{model_id.hex}"
    if model_purpose == "memory":
        policy_value["memory"]["model_name"] = model_name
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        policy_value,
    )

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
        if make_policy_current:
            await session.execute(
                text(
                    """UPDATE system_runtime_policies
                    SET current_version_id=:version,revision=:revision
                    WHERE section='agent_runtime'"""
                ),
                {
                    "version": policy_version_id,
                    "revision": policy_revision,
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
                VALUES (:id,:name,'Memory PR4 model','','active',NULL,1,0,:owner,:owner)"""
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
                VALUES (:project,:owner,:thread,:run,:purpose,:name,
                        :model,:version,:checksum,NULL,NULL,NULL)"""
            ),
            {
                "project": admitted.run.project_id,
                "owner": owner_id,
                "thread": admitted.run.thread_id,
                "run": admitted.run.run_id,
                "purpose": model_purpose,
                "name": model_name,
                "model": model_id,
                "version": model_version_id,
                "checksum": model_checksum,
            },
        )


async def admit_memory_extraction_job(
    seed,
    *,
    messages: list[dict[str, object]],
    mode: str = "shadow",
    model_purpose: str = "lead",
    make_policy_current: bool = False,
):
    if model_purpose not in {"lead", "memory"}:
        raise ValueError("invalid test model purpose")
    thread_id = f"memory-pr4-{uuid.uuid4()}"
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
                "command": None,
                "config": {
                    "configurable": {"thread_id": thread_id},
                    "context": {},
                },
                "stream_mode": ["values"],
                "stream_subgraphs": False,
            }
        ),
    )
    await _install_snapshots(
        seed,
        admitted,
        mode=mode,
        model_purpose=model_purpose,
        make_policy_current=make_policy_current,
    )

    source_worker_id = uuid.uuid4()
    await WorkerRegistry(seed.factory, version="memory-pr4-source").register(
        source_worker_id,
        frozenset({"private_run"}),
        1,
    )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        source_claim = await jobs.claim_next(
            worker_id=source_worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=90,
        )
        assert source_claim is not None
        assert source_claim.job_id == admitted.job.job_id
        assert await jobs.mark_running(
            source_claim.job_id,
            lease_token=source_claim.lease_token,
        )
    settlement = await PrivateRunJobHandler(
        seed.factory,
        executor=_SuccessfulExecutor(),
        memory_source_admission=MemorySourceAdmissionService(
            source_hmac=_source_hmac,
        ),
    )(
        source_claim,
        JobLeaseAuthority(seed.factory, source_claim, lease_seconds=90),
    )
    await settlement.commit()

    extract_worker_id = uuid.uuid4()
    await WorkerRegistry(seed.factory, version="memory-pr4-extract").register(
        extract_worker_id,
        frozenset({"memory_extract"}),
        1,
    )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        extract_claim = await jobs.claim_next(
            worker_id=extract_worker_id,
            capabilities=frozenset({"memory_extract"}),
            lease_seconds=90,
        )
        assert extract_claim is not None
        assert await jobs.mark_running(
            extract_claim.job_id,
            lease_token=extract_claim.lease_token,
        )
    return admitted, extract_claim


__all__ = ["admit_memory_extraction_job"]
