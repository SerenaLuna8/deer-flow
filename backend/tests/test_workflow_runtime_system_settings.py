from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.models import (
    SystemSettingAuditMetadata,
    resolve_system_audit_context,
)
from app.audit.service import AuditService
from app.gateway.routers.admin_system_settings import AdminSystemSectionResponse
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings.bootstrap import (
    WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID,
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.errors import SystemRuntimePolicyConflict
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.models import (
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
    parse_policy_value,
    runtime_policy_schema_version,
)
from app.workflows.runtime_policy import (
    WorkflowRuntimePolicyUpdateRequestV1,
    WorkflowRuntimePolicyV1,
    workflow_runtime_policy_checksum,
)

_SHARED_RUNTIME_POLICY_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-runtime-policy-v1.json"


def test_workflow_runtime_system_setting_uses_the_frozen_disabled_v1_default() -> None:
    policy = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)

    assert type(policy) is WorkflowRuntimePolicyV1
    assert policy.schema_version == 1
    assert policy.enabled is False
    assert policy.admission_enabled is False
    assert policy.code.enabled is False
    assert policy.http.enabled is False
    assert policy.http.write_enabled is False
    assert all(value is False for value in policy.future.model_dump(mode="python").values())
    assert workflow_runtime_policy_checksum(policy) == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
    assert WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID == uuid.UUID("ddf23bac-aa2c-5d9a-aa41-f014a66914a0")
    fixture = json.loads(_SHARED_RUNTIME_POLICY_FIXTURE.read_text(encoding="utf-8"))
    assert policy.model_dump(mode="json") == fixture["policy"]
    assert workflow_runtime_policy_checksum(policy) == fixture["payload_checksum"]


def test_workflow_runtime_canonicalization_is_v1_while_existing_sections_remain_v2() -> None:
    policy = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)
    canonical = canonical_policy_payload(RuntimePolicySection.WORKFLOW_RUNTIME, policy)

    assert canonical.schema_version == 1
    assert canonical.checksum == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
    assert type(parse_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME, canonical.value)) is WorkflowRuntimePolicyV1
    assert runtime_policy_schema_version(RuntimePolicySection.WORKFLOW_RUNTIME) == 1
    assert {runtime_policy_schema_version(section) for section in RuntimePolicySection if section is not RuntimePolicySection.WORKFLOW_RUNTIME} == {2}


@pytest.mark.parametrize(
    ("path", "secret_value"),
    (
        (("api_key",), "sk-proj-12345678"),
        (("code", "storage_locator"), "docker://private-control-plane"),
        (("http", "proxy_authorization"), "Bearer abcdefgh"),
    ),
)
def test_workflow_runtime_system_setting_rejects_secret_or_locator_material(
    path: tuple[str, ...],
    secret_value: str,
) -> None:
    payload: dict[str, object] = copy.deepcopy(default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="python"))
    cursor = payload
    for segment in path[:-1]:
        nested = cursor[segment]
        assert isinstance(nested, dict)
        cursor = nested
    cursor[path[-1]] = secret_value

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.WORKFLOW_RUNTIME, payload)


def test_workflow_runtime_audit_metadata_is_closed_and_content_free() -> None:
    metadata = SystemSettingAuditMetadata.model_validate(
        {
            "section": "workflow_runtime",
            "previous_revision": 1,
            "revision": 2,
            "effect_scope": "new_workflow_runs",
        }
    )

    assert metadata.section == "workflow_runtime"
    assert metadata.effect_scope == "new_workflow_runs"
    with pytest.raises(ValueError):
        SystemSettingAuditMetadata.model_validate(
            {
                **metadata.model_dump(mode="python"),
                "value": {"enabled": True},
            }
        )


def test_generic_admin_system_settings_dto_rejects_the_workflow_section() -> None:
    policy = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)

    with pytest.raises(ValueError):
        AdminSystemSectionResponse(
            section="workflow_runtime",  # type: ignore[arg-type]
            revision=1,
            schema_version=1,
            value=policy.model_dump(mode="json"),
            effect_scope="new_workflow_runs",
            effective_revision=1,
            updated_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_workflow_runtime_postgres_cas_materialization_and_content_free_audit(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    try:
        assert await bootstrap_system_runtime_policies(factory) == 1
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-runtime-{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(
                factory,
                AuditHmacKeyring(
                    active_key_id="workflow-runtime-test-v1",
                    _keys={"workflow-runtime-test-v1": b"a" * 32},
                ),
            ),
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )
        value = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME)
        updated = await service.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1(
                expected_revision=1,
                value=value.model_dump(mode="json"),
            ),
        )
        assert updated.catalog_revision == 2
        assert updated.stored.revision == 2
        assert updated.stored.schema_version == 1
        assert updated.effect_scope == "new_workflow_runs"
        assert updated.stored.value == value

        with pytest.raises(SystemRuntimePolicyConflict):
            await service.update_workflow_runtime_policy(
                context,
                WorkflowRuntimePolicyUpdateRequestV1(
                    expected_revision=1,
                    value=value.model_dump(mode="json"),
                ),
            )

        materialized = await SystemRuntimePolicyMaterializer(factory).materialize_workflow_runtime_current()
        assert type(materialized) is WorkflowRuntimePolicyV1
        assert materialized == value
        async with factory() as session, session.begin():
            locked = await service.lock_workflow_runtime_policy(session)
        assert locked.revision == 2
        assert locked.schema_version == 1
        assert locked.policy_version_id != WORKFLOW_RUNTIME_DEFAULT_POLICY_VERSION_ID
        assert locked.payload_checksum == "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
        assert locked.value == value

        async with factory() as session:
            audit_metadata = await session.scalar(
                text(
                    """SELECT metadata_json
                         FROM audit_logs
                        WHERE action='system_setting.updated'
                          AND metadata_json->>'section'='workflow_runtime'"""
                )
            )
        assert audit_metadata == {
            "section": "workflow_runtime",
            "previous_revision": 1,
            "revision": 2,
            "effect_scope": "new_workflow_runs",
        }
        assert await bootstrap_system_runtime_policies(factory) == 2
    finally:
        await engine.dispose()
