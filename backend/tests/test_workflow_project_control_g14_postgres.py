from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings import workflow_runtime as workflow_runtime_module
from app.system_runtime_settings.models import RuntimePolicySection, default_policy_value
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.workflow_runtime import (
    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
    WorkflowRuntimeConvergence,
)
from app.workflows.catalog_contracts import WorkflowCatalogCapabilityProjectionV1
from app.workflows.project_control_service import WorkflowProjectControlService
from app.workflows.runtime_policy import WorkflowRuntimePolicyUpdateRequestV1

pytestmark = pytest.mark.postgres

_CODE_PROFILE_DIGEST = "c" * 64
_HTTP_PROFILE_DIGEST = "d" * 64


def _policy_payload(*, admission_enabled: bool) -> dict[str, object]:
    payload = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="json")
    payload["enabled"] = True
    payload["admission_enabled"] = admission_enabled
    payload["execution_limits"]["max_code_activations"] = 16  # type: ignore[index]
    payload["execution_limits"]["max_http_calls"] = 16  # type: ignore[index]
    payload["code"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "provider_adapter_key": "aio_isolated_code_v1",
            "execution_profile_id": "isolated-python-312",
            "image_digest": f"sha256:{'c' * 64}",
            "isolation_profile": "deny-all-v1",
        }
    )
    payload["http"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "write_enabled": False,
            "egress_profile_id": "controlled-egress-v1",
            "egress_profile_digest": _HTTP_PROFILE_DIGEST,
            "injection_profiles": [
                {
                    "id": "api-key-v1",
                    "location": "header",
                    "scheme": "api_key",
                    "target_header": "x-api-key",
                    "credential_payload_contract": "api_key_v1",
                }
            ],
            "endpoint_policies": [
                {
                    "id": "public-api",
                    "origin": "https://api.example.com",
                    "allowed_methods": ["GET", "POST"],
                    "injection_profile_ids": ["api-key-v1"],
                    "write_idempotency": "server_derived_key",
                    "idempotency_header": "x-workflow-idempotency-key",
                }
            ],
        }
    )
    return payload


def _entry(catalog, node_type: str):
    return next(entry for entry in catalog.entries if entry.definition.type == node_type)


async def _insert_worker(
    session: AsyncSession,
    *,
    worker_id: uuid.UUID,
    policy,
    profiles: list[str],
    capabilities: list[str] | None = None,
    draining: bool = False,
) -> None:
    await session.execute(
        text(
            """INSERT INTO worker_nodes
               (id,version,capabilities_json,runtime_profile_digests_json,
                workflow_runtime_policy_section,
                workflow_runtime_policy_version_id,
                workflow_runtime_policy_revision,
                workflow_runtime_policy_schema_version,
                workflow_runtime_policy_checksum,
                max_concurrent_jobs,heartbeat_at,draining)
               VALUES (:id,'g14',CAST(:capabilities AS json),CAST(:profiles AS jsonb),
                       'workflow_runtime',:policy_version,:revision,:schema_version,
                       :checksum,1,statement_timestamp(),:draining)"""
        ),
        {
            "id": worker_id,
            "capabilities": json.dumps(["workflow_run"] if capabilities is None else capabilities),
            "profiles": json.dumps(profiles),
            "policy_version": policy.stored.policy_version_id,
            "revision": policy.stored.revision,
            "schema_version": policy.stored.schema_version,
            "checksum": policy.stored.payload_checksum,
            "draining": draining,
        },
    )


@pytest.mark.anyio
async def test_project_control_postgres_matrix_and_independent_runtime_facets(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    generic_worker = uuid.uuid4()
    code_worker = uuid.uuid4()
    http_worker = uuid.uuid4()
    convergence = WorkflowRuntimeConvergence(
        code_profile_digest_resolver=lambda _policy: _CODE_PROFILE_DIGEST,
        http_profile_digest_resolver=lambda policy: policy.http.egress_profile_digest,
    )
    project_control = WorkflowProjectControlService(convergence=convergence)
    capabilities = WorkflowCatalogCapabilityProjectionV1(
        code_use=True,
        http_use=True,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                       (id,email,system_role,created_at,needs_setup,token_version)
                       VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"workflow-g14-{admin_id}@example.com",
                },
            )
        audit = AuditService(
            factory,
            AuditHmacKeyring(
                active_key_id="workflow-g14-v1",
                _keys={"workflow-g14-v1": b"g" * 32},
            ),
        )
        settings = SystemRuntimePolicyService(
            factory,
            audit,
            workflow_runtime_convergence=convergence,
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id="workflow-g14-admin",
        )

        async with factory() as session:
            disabled = await project_control.read_readiness(
                session,
                request_id="workflow-g14-disabled",
            )
        assert disabled.code == "WORKFLOW_DISABLED"

        builder_only = await settings.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1.model_validate(
                {
                    "expected_revision": 1,
                    "value": _policy_payload(admission_enabled=False),
                }
            ),
        )
        async with factory() as session:
            builder = await project_control.read_readiness(
                session,
                request_id="workflow-g14-builder",
            )
        assert builder.code == "WORKFLOW_CONTROL_PLANE_READY"
        assert builder.admission_ready is False

        enabled = await settings.update_workflow_runtime_policy(
            context,
            WorkflowRuntimePolicyUpdateRequestV1.model_validate(
                {
                    "expected_revision": builder_only.stored.revision,
                    "value": _policy_payload(admission_enabled=True),
                }
            ),
        )
        async with factory() as session, session.begin():
            # Even an exact-policy row carrying every guessed digest cannot
            # bypass the G32/G40/G43 code-level handler gates.
            await _insert_worker(
                session,
                worker_id=generic_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _CODE_PROFILE_DIGEST,
                    _HTTP_PROFILE_DIGEST,
                ],
            )
        async with factory() as session:
            forged = await project_control.read_readiness(
                session,
                request_id="workflow-g14-forged",
            )
            forged_catalog = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-forged-catalog",
                capabilities=capabilities,
            )
        assert forged.admission_ready is False
        assert _entry(forged_catalog, "python_code").availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
        assert _entry(forged_catalog, "http_request").availability.reason_code == "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"

        monkeypatch.setattr(
            workflow_runtime_module,
            "WORKFLOW_RUN_HANDLER_INSTALLED",
            True,
        )
        monkeypatch.setattr(
            workflow_runtime_module,
            "WORKFLOW_CODE_EXECUTION_HANDLER_INSTALLED",
            True,
        )
        monkeypatch.setattr(
            workflow_runtime_module,
            "WORKFLOW_HTTP_EXECUTION_HANDLER_INSTALLED",
            True,
        )
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM worker_nodes WHERE id=:id"),
                {"id": generic_worker},
            )
            await _insert_worker(
                session,
                worker_id=generic_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _CODE_PROFILE_DIGEST,
                    _HTTP_PROFILE_DIGEST,
                ],
                capabilities=[],
            )
        async with factory() as session:
            wrong_capability = await project_control.read_readiness(
                session,
                request_id="workflow-g14-wrong-capability",
            )
        assert wrong_capability.admission_ready is False

        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM worker_nodes WHERE id=:id"),
                {"id": generic_worker},
            )
            # A valid older policy identity with perfect profiles cannot
            # satisfy the current exact-policy facet query.
            await _insert_worker(
                session,
                worker_id=generic_worker,
                policy=builder_only,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _CODE_PROFILE_DIGEST,
                    _HTTP_PROFILE_DIGEST,
                ],
            )
            await _insert_worker(
                session,
                worker_id=code_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    "e" * 64,
                ],
            )
            await _insert_worker(
                session,
                worker_id=http_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    "f" * 64,
                ],
            )
        async with factory() as session:
            wrong_profiles = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-wrong-profiles",
                capabilities=capabilities,
            )
        assert _entry(wrong_profiles, "python_code").availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
        assert _entry(wrong_profiles, "http_request").availability.reason_code == "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"

        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM worker_nodes WHERE id IN (:code_id,:http_id)"),
                {
                    "code_id": code_worker,
                    "http_id": http_worker,
                },
            )
            await _insert_worker(
                session,
                worker_id=code_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _CODE_PROFILE_DIGEST,
                ],
                draining=True,
            )
            await _insert_worker(
                session,
                worker_id=http_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _HTTP_PROFILE_DIGEST,
                ],
                draining=True,
            )
        async with factory() as session:
            draining = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-draining",
                capabilities=capabilities,
            )
        assert _entry(draining, "python_code").availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
        assert _entry(draining, "http_request").availability.reason_code == "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"

        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM worker_nodes WHERE id IN (:generic_id,:code_id,:http_id)"),
                {
                    "generic_id": generic_worker,
                    "code_id": code_worker,
                    "http_id": http_worker,
                },
            )
            await _insert_worker(
                session,
                worker_id=generic_worker,
                policy=enabled,
                profiles=[WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1],
            )
            await _insert_worker(
                session,
                worker_id=code_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _CODE_PROFILE_DIGEST,
                ],
            )
            await _insert_worker(
                session,
                worker_id=http_worker,
                policy=enabled,
                profiles=[
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    _HTTP_PROFILE_DIGEST,
                ],
            )

        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
        try:
            async with factory() as session:
                ready = await project_control.read_readiness(
                    session,
                    request_id="workflow-g14-ready",
                )
                catalog = await project_control.read_node_catalog(
                    session,
                    request_id="workflow-g14-catalog",
                    capabilities=capabilities,
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)
        assert ready.admission_ready is True
        assert _entry(catalog, "python_code").availability.state == "enabled"
        assert _entry(catalog, "http_request").availability.state == "enabled"
        assert _entry(catalog, "http_request").http_authoring.model_dump(mode="json") == {
            "endpoints": [
                {
                    "id": "public-api",
                    "origin": "https://api.example.com",
                    "allowed_methods": ["GET"],
                    "write_idempotency": "none",
                    "injection_profiles": [
                        {
                            "id": "api-key-v1",
                            "scheme": "api_key",
                            "target_header": "x-api-key",
                            "credential_payload_contract": "api_key_v1",
                        }
                    ],
                }
            ]
        }
        # One independent facet query per service operation; never one query
        # or graph walk per node/profile.
        assert len([statement for statement in statements if "worker_nodes" in statement]) == 2

        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE worker_nodes
                          SET heartbeat_at=statement_timestamp() - interval '10 minutes'
                        WHERE id=:id"""
                ),
                {"id": code_worker},
            )
        async with factory() as session:
            code_revoked = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-code-revoked",
                capabilities=capabilities,
            )
            still_ready = await project_control.read_readiness(
                session,
                request_id="workflow-g14-generic-still-ready",
            )
        assert still_ready.admission_ready is True
        assert _entry(code_revoked, "python_code").availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
        assert _entry(code_revoked, "http_request").availability.state == "enabled"

        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE worker_nodes SET heartbeat_at=statement_timestamp()
                        WHERE id=:code_id"""
                ),
                {"code_id": code_worker},
            )
            await session.execute(
                text(
                    """UPDATE worker_nodes
                          SET heartbeat_at=statement_timestamp() - interval '10 minutes'
                        WHERE id=:http_id"""
                ),
                {"http_id": http_worker},
            )
        async with factory() as session:
            http_revoked = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-http-revoked",
                capabilities=capabilities,
            )
        assert _entry(http_revoked, "python_code").availability.state == "enabled"
        assert _entry(http_revoked, "http_request").availability.reason_code == "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"

        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE worker_nodes SET heartbeat_at=statement_timestamp()
                        WHERE id=:id"""
                ),
                {"id": http_worker},
            )
        async with factory() as session:
            restored = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-restored",
                capabilities=capabilities,
            )
            capability_revoked = await project_control.read_node_catalog(
                session,
                request_id="workflow-g14-capability-revoked",
                capabilities=WorkflowCatalogCapabilityProjectionV1(
                    code_use=False,
                    http_use=True,
                ),
            )
        assert _entry(restored, "python_code").availability.state == "enabled"
        assert _entry(restored, "http_request").availability.state == "enabled"
        assert _entry(capability_revoked, "python_code").availability.reason_code == "WORKFLOW_NODE_CAPABILITY_REQUIRED"
        assert _entry(capability_revoked, "http_request").availability.state == "enabled"
    finally:
        await engine.dispose()


class _NeverPolicyReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_current(self, _session: AsyncSession):
        self.calls += 1
        raise AssertionError("schema failure must win before policy materialization")


@pytest.mark.anyio
async def test_project_readiness_postgres_schema_failure_wins_before_policy(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    policy_reader = _NeverPolicyReader()
    service = WorkflowProjectControlService(policy_reader=policy_reader)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        await connection.execute(text("UPDATE alembic_version SET version_num='full_schema_v9'"))
        readiness = await service.read_readiness(
            session,
            request_id="workflow-g14-schema-first-postgres",
        )
        assert readiness.code == "WORKFLOW_SCHEMA_UNAVAILABLE"
        assert policy_reader.calls == 0
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()
