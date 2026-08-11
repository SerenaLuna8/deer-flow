from __future__ import annotations

import inspect
import uuid
from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

import deerflow.persistence.models  # noqa: F401 -- register final metadata
from app.gateway.routers import project_workflows
from app.workflows.definition_domain import canonical_workflow_control_request_digest_v1
from deerflow.persistence.base import Base
from deerflow.persistence.workflows.sql import (
    WORKFLOW_CONTROL_OPERATIONS,
    WorkflowControlOperationCreate,
    WorkflowControlOperationRecord,
    WorkflowRepository,
    canonical_workflow_control_scope_key,
    hash_workflow_control_idempotency_key,
)


def test_control_operation_identity_is_strict_canonical_and_scope_isolated() -> None:
    project_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    workflow_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    version_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    other_project_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    other_workflow_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    other_version_id = uuid.UUID("66666666-6666-4666-8666-666666666666")

    assert WORKFLOW_CONTROL_OPERATIONS == frozenset(
        {
            "create",
            "update",
            "save_draft",
            "archive",
            "publish",
            "draft_grant_put",
            "draft_grant_delete",
            "version_grant_put",
            "version_grant_delete",
        }
    )
    assert (
        canonical_workflow_control_scope_key(
            project_id=project_id,
            operation="create",
        )
        == f"project:{project_id}"
    )
    assert (
        canonical_workflow_control_scope_key(
            project_id=project_id,
            operation="save_draft",
            workflow_id=workflow_id,
        )
        == f"definition:{workflow_id}"
    )
    assert (
        canonical_workflow_control_scope_key(
            project_id=project_id,
            operation="version_grant_put",
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id="http.auth",
        )
        == f"version-slot:{workflow_id}:{version_id}:http.auth"
    )
    assert canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="create",
    ) != canonical_workflow_control_scope_key(
        project_id=other_project_id,
        operation="create",
    )
    assert canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="save_draft",
        workflow_id=workflow_id,
    ) != canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="save_draft",
        workflow_id=other_workflow_id,
    )
    assert canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="draft_grant_put",
        workflow_id=workflow_id,
        slot_id="http.auth",
    ) != canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="draft_grant_put",
        workflow_id=workflow_id,
        slot_id="http.token",
    )
    assert canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="version_grant_put",
        workflow_id=workflow_id,
        version_id=version_id,
        slot_id="http.auth",
    ) != canonical_workflow_control_scope_key(
        project_id=project_id,
        operation="version_grant_put",
        workflow_id=workflow_id,
        version_id=other_version_id,
        slot_id="http.auth",
    )

    assert hash_workflow_control_idempotency_key("retry-key") == hash_workflow_control_idempotency_key("retry-key")
    for invalid_key in ("", "contains space", "non-ascii-非", "x" * 256):
        with pytest.raises(ValueError):
            hash_workflow_control_idempotency_key(invalid_key)

    digest = canonical_workflow_control_request_digest_v1(
        operation="save_draft",
        project_id=project_id,
        workflow_id=workflow_id,
        request={
            "expected_revision": 7,
            "spec": {"schema_version": 1},
            "canvas": {"schema_version": 1},
        },
    )
    assert digest == canonical_workflow_control_request_digest_v1(
        operation="save_draft",
        project_id=project_id,
        workflow_id=workflow_id,
        request={
            "canvas": {"schema_version": 1},
            "spec": {"schema_version": 1},
            "expected_revision": 7,
        },
    )
    assert digest != canonical_workflow_control_request_digest_v1(
        operation="save_draft",
        project_id=project_id,
        workflow_id=workflow_id,
        request={
            "expected_revision": 8,
            "spec": {"schema_version": 1},
            "canvas": {"schema_version": 1},
        },
    )


def test_control_request_digest_accepts_database_uuid_subclasses_only() -> None:
    class DriverUuid(uuid.UUID):
        pass

    project_id = uuid.uuid4()
    expected = canonical_workflow_control_request_digest_v1(
        operation="create",
        project_id=project_id,
        request={"name": "One", "description": ""},
    )
    assert (
        canonical_workflow_control_request_digest_v1(
            operation="create",
            project_id=DriverUuid(str(project_id)),
            request={"name": "One", "description": ""},
        )
        == expected
    )
    with pytest.raises(TypeError):
        canonical_workflow_control_request_digest_v1(
            operation="create",
            project_id=str(project_id),  # type: ignore[arg-type]
            request={"name": "One", "description": ""},
        )


def test_control_receipt_is_scalar_only_and_has_no_private_payload_field() -> None:
    table = Base.metadata.tables["workflow_control_operations"]
    assert set(table.c) >= {
        table.c.project_id,
        table.c.scope_key,
        table.c.operation,
        table.c.idempotency_hash,
        table.c.request_digest,
        table.c.workflow_id,
        table.c.result_version_id,
        table.c.result_revision,
        table.c.result_checksum,
        table.c.result_slot_id,
        table.c.result_credential_id,
        table.c.result_credential_version_id,
        table.c.result_status,
        table.c.result_deleted,
        table.c.result_name,
        table.c.result_description,
        table.c.result_lifecycle,
        table.c.result_published_version_id,
        table.c.result_published_version_number,
        table.c.result_draft_revision,
        table.c.result_draft_checksum,
        table.c.result_missing_slot_ids_csv,
    }
    forbidden = (
        "key",
        "secret",
        "spec",
        "canvas",
        "payload",
        "response",
        "json",
        "header",
        "envelope",
        "cipher",
        "nonce",
        "value",
    )
    public_fields = {item.name for item in fields(WorkflowControlOperationCreate)}
    column_names = set(table.c.keys())
    assert "idempotency_hash" in public_fields
    assert "scope_key" not in public_fields
    assert all(fragment not in field_name for field_name in public_fields for fragment in forbidden if fragment != "key")
    assert all(fragment not in column_name for column_name in column_names for fragment in forbidden if fragment != "key")


def test_control_receipt_scope_is_derived_and_result_shapes_are_exact() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    project_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    actor_id = str(uuid.uuid4())
    create = WorkflowControlOperationCreate(
        project_id=project_id,
        workflow_id=workflow_id,
        operation="create",
        idempotency_hash="a" * 64,
        request_digest="b" * 64,
        created_by=actor_id,
        result_revision=1,
        result_created_at=now,
        result_updated_at=now,
        result_name="Original",
        result_description="",
        result_lifecycle="active",
        result_draft_revision=1,
        result_draft_checksum="c" * 64,
    )
    assert create.scope_key == f"project:{project_id}"
    assert "scope_key" not in inspect.signature(WorkflowControlOperationCreate).parameters
    assert "scope_key" not in inspect.signature(WorkflowRepository.get_control_operation).parameters
    assert {
        "workflow_id",
        "version_id",
        "slot_id",
    } <= set(inspect.signature(WorkflowRepository.get_control_operation).parameters)

    with pytest.raises(TypeError):
        WorkflowControlOperationCreate(  # type: ignore[call-arg]
            project_id=project_id,
            workflow_id=workflow_id,
            operation="create",
            scope_key=f"definition:{workflow_id}",
            idempotency_hash="a" * 64,
            request_digest="b" * 64,
            created_by=actor_id,
        )
    with pytest.raises(ValueError):
        replace(create, result_credential_id=uuid.uuid4())

    credential = WorkflowControlOperationCreate(
        project_id=project_id,
        workflow_id=workflow_id,
        operation="draft_grant_put",
        idempotency_hash="d" * 64,
        request_digest="e" * 64,
        created_by=actor_id,
        result_checksum="f" * 64,
        result_slot_id="http.auth",
        result_credential_id=uuid.uuid4(),
        result_credential_version_id=uuid.uuid4(),
        result_updated_at=now,
    )
    assert credential.scope_key == f"draft-slot:{workflow_id}:http.auth"
    with pytest.raises(ValueError):
        replace(credential, result_checksum=None)
    with pytest.raises(ValueError):
        WorkflowControlOperationRecord(
            **{item.name: getattr(credential, item.name) for item in fields(WorkflowControlOperationCreate)},
            scope_key=f"draft-slot:{uuid.uuid4()}:http.auth",
            created_at=now,
        )


@pytest.mark.parametrize(
    "route_name",
    (
        "create_workflow_definition",
        "update_workflow_definition",
        "save_workflow_draft",
        "publish_workflow_draft",
        "put_workflow_draft_grant_intent",
        "delete_workflow_draft_grant_intent",
        "put_workflow_version_grant",
        "revoke_workflow_version_grant",
        "archive_workflow_definition",
    ),
)
def test_every_definition_state_mutation_requires_one_strict_idempotency_header(route_name: str) -> None:
    signature = inspect.signature(getattr(project_workflows, route_name))
    parameter = signature.parameters["idempotency_key"]
    assert parameter.default is inspect.Signature.empty
    assert "Idempotency-Key" in repr(parameter.annotation)
