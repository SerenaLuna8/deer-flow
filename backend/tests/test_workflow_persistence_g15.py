from __future__ import annotations

import inspect
import uuid
from dataclasses import fields

import pytest

from deerflow.persistence.workflows.sql import (
    WorkflowCodeRequirementCreate,
    WorkflowCredentialGrantPut,
    WorkflowCredentialSlotCreate,
    WorkflowDefinitionArchive,
    WorkflowDefinitionListQuery,
    WorkflowDefinitionPage,
    WorkflowDefinitionRecord,
    WorkflowDraftCredentialGrantIntentRecord,
    WorkflowDraftUpdate,
    WorkflowHttpRequirementCreate,
    WorkflowRepository,
    WorkflowVersionCodeRequirementRecord,
    WorkflowVersionCredentialSlotRecord,
    WorkflowVersionHttpRequirementRecord,
    WorkflowVersionModelRefRecord,
    WorkflowVersionPublish,
    WorkflowVersionPublishResult,
    WorkflowVersionRecord,
    hash_workflow_publish_idempotency_key,
)


def test_definition_list_query_is_closed_bounded_and_normalizes_empty_search() -> None:
    query = WorkflowDefinitionListQuery(
        query="   ",
        lifecycle="active",
        publication="all",
        sort="updated_desc",
        cursor=None,
        limit=100,
    )
    assert query.query is None
    assert query.limit == 100

    for invalid_limit in (0, 101, True):
        with pytest.raises((TypeError, ValueError)):
            WorkflowDefinitionListQuery(limit=invalid_limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkflowDefinitionListQuery(lifecycle="deleted")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkflowDefinitionListQuery(publication="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkflowDefinitionListQuery(sort="random")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkflowDefinitionListQuery(cursor="x" * 1025)


def test_archive_and_grant_commands_are_strict_and_secret_free() -> None:
    archive = WorkflowDefinitionArchive(expected_revision=3)
    assert archive.expected_revision == 3
    with pytest.raises(ValueError):
        WorkflowDefinitionArchive(expected_revision=0)

    command = WorkflowCredentialGrantPut(
        credential_id=uuid.uuid4(),
        expected_credential_version_id=uuid.uuid4(),
        expected_slot_schema_checksum="a" * 64,
        resolved_slot_schema_checksum="a" * 64,
    )
    assert command.expected_slot_schema_checksum == command.resolved_slot_schema_checksum
    with pytest.raises(ValueError):
        WorkflowCredentialGrantPut(
            credential_id=command.credential_id,
            expected_credential_version_id=command.expected_credential_version_id,
            expected_slot_schema_checksum="a" * 64,
            resolved_slot_schema_checksum="b" * 64,
        )

    forbidden_fragments = ("secret", "envelope", "cipher", "nonce", "header", "scheme", "value")
    command_fields = {item.name.lower() for item in fields(WorkflowCredentialGrantPut)}
    intent_fields = {item.name.lower() for item in fields(WorkflowDraftCredentialGrantIntentRecord)}
    assert all(fragment not in field_name for field_name in command_fields | intent_fields for fragment in forbidden_fragments)
    raw_key = "publish-key-1"
    assert hash_workflow_publish_idempotency_key(raw_key) == ("f29730f722b1b0b5e42c966fb7c9837f6ce30a2e19712dde2d0974b7b9f638dc")
    for invalid_key in ("", "contains space", "非-ascii"):
        with pytest.raises(ValueError):
            hash_workflow_publish_idempotency_key(invalid_key)


def test_draft_retained_credential_slot_ids_are_exact_frozen_coordinates() -> None:
    command = WorkflowDraftUpdate(
        expected_revision=1,
        spec_schema_version=1,
        canvas_schema_version=1,
        spec={},
        canvas={},
        draft_checksum="c" * 64,
        credential_slot_ids=("http.auth", "model.api-key"),
    )
    assert command.credential_slot_ids == ("http.auth", "model.api-key")
    with pytest.raises(TypeError):
        WorkflowDraftUpdate(
            expected_revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec={},
            canvas={},
            draft_checksum="c" * 64,
            credential_slot_ids=["http.auth"],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        WorkflowDraftUpdate(
            expected_revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec={},
            canvas={},
            draft_checksum="c" * 64,
            credential_slot_ids=("http.auth", "http.auth"),
        )


def test_complete_version_projection_is_immutable_and_has_no_runtime_secret_carrier() -> None:
    version_fields = {item.name for item in fields(WorkflowVersionRecord)}
    assert {
        "spec",
        "canvas",
        "model_refs",
        "credential_slots",
        "code_requirements",
        "http_requirements",
        "published_by",
        "active_grants",
        "missing_required_slot_ids",
        "executable",
    } <= version_fields
    assert {
        "current_published_version_number",
        "draft_revision",
        "draft_checksum",
    } <= {item.name for item in fields(WorkflowDefinitionRecord)}
    assert {item.name for item in fields(WorkflowVersionModelRefRecord)} == {
        "node_id",
        "purpose",
        "logical_model_name",
    }
    assert {item.name for item in fields(WorkflowVersionCredentialSlotRecord)} == {
        "slot_id",
        "name",
        "purpose",
        "payload_schema",
        "payload_schema_checksum",
        "required",
    }
    assert {item.name for item in fields(WorkflowVersionCodeRequirementRecord)} == {
        "node_id",
        "runtime_contract",
    }
    assert {item.name for item in fields(WorkflowVersionHttpRequirementRecord)} == {
        "node_id",
        "method",
        "endpoint_policy_id",
        "injection_profile_id",
        "credential_slot_id",
    }
    for field_name in version_fields:
        assert "envelope" not in field_name.lower()
        assert "secret" not in field_name.lower()
    assert tuple(item.name for item in fields(WorkflowVersionPublishResult)) == (
        "record",
        "created",
    )


def test_published_code_and_http_requirements_are_closed_and_slot_bound() -> None:
    node_id = uuid.uuid4()
    code = WorkflowCodeRequirementCreate(
        node_id=node_id,
        runtime_contract="python3.12-v1",
    )
    assert code.runtime_contract == "python3.12-v1"
    with pytest.raises(ValueError):
        WorkflowCodeRequirementCreate(node_id=node_id, runtime_contract="Python 3.12")
    with pytest.raises(ValueError):
        WorkflowCodeRequirementCreate(node_id=node_id, runtime_contract="python3.13-v1")

    http = WorkflowHttpRequirementCreate(
        node_id=node_id,
        method="POST",
        endpoint_policy_id="approved.api",
        injection_profile_id="header.api-key",
        credential_slot_id="http.auth",
    )
    assert http.credential_slot_id == "http.auth"
    with pytest.raises(ValueError):
        WorkflowHttpRequirementCreate(
            node_id=node_id,
            method="CONNECT",
            endpoint_policy_id="approved.api",
        )
    with pytest.raises(ValueError):
        WorkflowHttpRequirementCreate(
            node_id=node_id,
            method="GET",
            endpoint_policy_id="approved.api",
            injection_profile_id="header.api-key",
        )
    slot = WorkflowCredentialSlotCreate(
        slot_id="http.auth",
        name="HTTP auth",
        purpose="http_auth",
        payload_schema={"type": "object"},
        payload_schema_checksum="a" * 64,
    )
    command = WorkflowVersionPublish(
        expected_draft_revision=1,
        expected_draft_checksum="b" * 64,
        graph_schema_version=1,
        canvas_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="c" * 64,
        credential_slots=(slot,),
        code_requirements=(code,),
        http_requirements=(http,),
    )
    assert command.code_requirements == (code,)
    assert command.http_requirements == (http,)
    with pytest.raises(ValueError):
        WorkflowVersionPublish(
            expected_draft_revision=1,
            expected_draft_checksum="b" * 64,
            graph_schema_version=1,
            canvas_schema_version=1,
            compiler_contract_version=1,
            semantic_checksum="c" * 64,
            http_requirements=(http,),
        )


def test_g15_repository_surface_is_session_bound_and_never_commits() -> None:
    source = inspect.getsource(WorkflowRepository)
    module_source = inspect.getsource(inspect.getmodule(WorkflowRepository))
    assert ".commit(" not in source
    assert "CredentialEnvelopeRow" not in module_source
    assert "credential_envelopes" not in module_source
    for method_name in (
        "list_definitions",
        "archive_definition",
        "update_definition",
        "get_version",
        "list_version_history",
        "get_publish_replay",
        "put_draft_grant_intent",
        "delete_draft_grant_intent",
        "put_version_grant",
        "revoke_version_grant",
    ):
        assert hasattr(WorkflowRepository, method_name)
    assert fields(WorkflowDefinitionPage)[1].name == "next_cursor"
