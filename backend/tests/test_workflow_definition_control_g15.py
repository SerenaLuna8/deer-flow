from __future__ import annotations

import copy
import uuid
from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.audit.models import AuditAction
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.system_runtime_settings.workflow_defaults import default_workflow_runtime_policy
from app.system_runtime_settings.workflow_runtime import create_workflow_runtime_facet_readiness
from app.workflows.authorization import WorkflowAction
from app.workflows.definition_contracts import (
    WorkflowCredentialGrantMutationRequestV1,
    WorkflowDefinitionPageV1,
    WorkflowDraftCanvasV1,
    WorkflowDraftSaveRequestV1,
    WorkflowDraftSpecV1,
    WorkflowDraftValidateRequestV1,
    WorkflowPublishRequestV1,
    workflow_definition_response_public_projection_v1,
)
from app.workflows.definition_domain import (
    WorkflowDefinitionAuthoritySnapshot,
    canonical_workflow_draft_checksum_v1,
    canonical_workflow_slot_schema_checksum_v1,
)
from app.workflows.definition_service import WorkflowDefinitionControlService
from app.workflows.errors import (
    WorkflowDraftConflict,
    WorkflowDraftInvalid,
    WorkflowForbidden,
)
from app.workflows.repository import (
    WorkflowControlIdempotencyConflict,
    WorkflowControlOperationCreate,
    WorkflowControlOperationRecord,
    WorkflowCredentialGrantRecord,
    WorkflowDefinitionPage,
    WorkflowDefinitionRecord,
    WorkflowDraftCASConflict,
    WorkflowDraftCredentialGrantIntentRecord,
    WorkflowDraftRecord,
    WorkflowPublishIdempotencyConflict,
    WorkflowVersionCodeRequirementRecord,
    WorkflowVersionCredentialSlotRecord,
    WorkflowVersionHttpRequirementRecord,
    WorkflowVersionPage,
    WorkflowVersionPublishResult,
    WorkflowVersionRecord,
    canonical_workflow_control_scope_key,
    hash_workflow_publish_idempotency_key,
)
from app.workflows.runtime_policy import WorkflowRuntimePolicyV1, workflow_runtime_policy_checksum
from deerflow.workflows.compiler import freeze_json

START_ID = "10000000-0000-4000-8000-000000000001"
LLM_ID = "10000000-0000-4000-8000-000000000002"
HTTP_ID = "10000000-0000-4000-8000-000000000003"
CODE_ID = "10000000-0000-4000-8000-000000000004"
END_ID = "10000000-0000-4000-8000-000000000005"
WORKFLOW_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
PROJECT_ID = uuid.UUID("20000000-0000-4000-8000-000000000002")
USER_ID = uuid.UUID("20000000-0000-4000-8000-000000000003")
MEMBERSHIP_ID = uuid.UUID("20000000-0000-4000-8000-000000000004")
POLICY_ID = uuid.UUID("20000000-0000-4000-8000-000000000005")
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _execution_policy() -> dict[str, object]:
    return {
        "retry": {"mode": "none"},
        "on_error": {"mode": "fail_workflow"},
    }


def _node(node_id: str, node_type: str, config: dict[str, object]) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "type_version": 1,
        "scope": {"kind": "root"},
        "custom_label": None,
        "description": None,
        "input_bindings": {},
        "execution_policy": _execution_policy(),
        "config": config,
    }


def _transition(index: int, source: str, source_port: str, target: str) -> dict[str, object]:
    return {
        "id": f"edge-{index}",
        "source": {"node_id": source, "port_id": source_port},
        "target": {"node_id": target, "port_id": "in"},
    }


def _spec_payload(*, middle: list[dict[str, object]] | None = None) -> dict[str, object]:
    middle = middle or []
    nodes = [_node(START_ID, "start", {}), *middle, _node(END_ID, "end", {})]
    transitions: list[dict[str, object]] = []
    previous = START_ID
    source_port = "next"
    for index, item in enumerate([*middle, {"id": END_ID}], start=1):
        target = str(item["id"])
        transitions.append(_transition(index, previous, source_port, target))
        previous = target
        source_port = "next"
    return {
        "schema_version": 1,
        "entry_node_id": START_ID,
        "nodes": nodes,
        "transitions": transitions,
        "workflow_inputs": [],
        "workflow_outputs": [],
        "credential_slots": [],
    }


def _canvas_payload(spec: dict[str, object]) -> dict[str, object]:
    nodes = spec.get("nodes", [])
    transitions = spec.get("transitions", [])
    return {
        "schema_version": 1,
        "node_layouts": [
            {
                "node_id": item["id"],
                "position": {"x": index * 100, "y": 0},
            }
            for index, item in enumerate(nodes)
        ],
        "edge_layouts": [{"edge_id": item["id"], "routing": "bezier"} for item in transitions],
    }


def _enabled_policy(*, code: bool = False, http: bool = False) -> WorkflowRuntimePolicyV1:
    payload = default_workflow_runtime_policy().model_dump(mode="json")
    payload["enabled"] = True
    payload["admission_enabled"] = True
    payload["execution_limits"]["max_code_activations"] = 100
    payload["execution_limits"]["max_http_calls"] = 100
    if code:
        payload["code"].update(
            {
                "enabled": True,
                "provider_adapter_key": "aio_isolated_code_v1",
                "execution_profile_id": "python312",
                "image_digest": "sha256:" + "1" * 64,
                "isolation_profile": "strict",
            }
        )
    if http:
        payload["http"].update(
            {
                "enabled": True,
                "write_enabled": True,
                "egress_profile_id": "controlled-egress",
                "egress_profile_digest": "2" * 64,
                "injection_profiles": [
                    {
                        "id": "bearer-v1",
                        "location": "header",
                        "scheme": "bearer",
                        "target_header": "authorization",
                        "credential_payload_contract": "bearer_token_v1",
                    }
                ],
                "endpoint_policies": [
                    {
                        "id": "partner-api",
                        "origin": "https://api.example.com",
                        "allowed_methods": ["GET", "POST"],
                        "injection_profile_ids": ["bearer-v1"],
                        "write_idempotency": "server_derived_key",
                        "idempotency_header": "x-idempotency-key",
                    }
                ],
            }
        )
    return WorkflowRuntimePolicyV1.model_validate(payload)


def _authority(*, code: bool = False, http: bool = False, code_ready: bool | None = None, http_ready: bool | None = None) -> WorkflowDefinitionAuthoritySnapshot:
    policy = _enabled_policy(code=code, http=http)
    locked = LockedWorkflowRuntimePolicy.create(
        policy_version_id=POLICY_ID,
        revision=7,
        schema_version=1,
        payload_checksum=workflow_runtime_policy_checksum(policy),
        value=policy,
    )
    return WorkflowDefinitionAuthoritySnapshot(
        locked_policy=locked,
        facets=create_workflow_runtime_facet_readiness(
            generic_ready=True,
            code_ready=code if code_ready is None else code_ready,
            http_ready=http if http_ready is None else http_ready,
        ),
    )


def _project_context(*, capabilities: frozenset[Capability] | None = None) -> ProjectContext:
    return ProjectContext(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        membership_id=MEMBERSHIP_ID,
        role=ProjectRole.ADMIN,
        capabilities=capabilities if capabilities is not None else capabilities_for(ProjectRole.ADMIN),
        membership_version=3,
        request_id="req-g15",
    )


def _private_context(*, capabilities: frozenset[Capability] | None = None) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(_project_context(capabilities=capabilities))


class _Authorizer:
    def __init__(self, *, denied: frozenset[WorkflowAction] = frozenset()) -> None:
        self.denied = denied
        self.calls: list[tuple[WorkflowAction, bool]] = []

    async def require(self, _session, context, action: WorkflowAction, *, lock: bool) -> ProjectContext:
        self.calls.append((action, lock))
        if action in self.denied:
            raise WorkflowForbidden(context.request_id)
        return _project_context(capabilities=context.capabilities)


class _AuthorityReader:
    def __init__(self, authority: WorkflowDefinitionAuthoritySnapshot) -> None:
        self.authority = authority
        self.calls = 0
        self.for_update: list[bool] = []

    async def read_current(
        self,
        _session,
        *,
        for_update: bool,
    ) -> WorkflowDefinitionAuthoritySnapshot:
        self.calls += 1
        self.for_update.append(for_update)
        return self.authority


class _Repository:
    def __init__(
        self,
        spec: dict[str, object],
        canvas: dict[str, object],
        *,
        active_slot_ids: tuple[str, ...] = (),
    ) -> None:
        self.draft = WorkflowDraftRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            revision=4,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec=copy.deepcopy(spec),
            canvas=copy.deepcopy(canvas),
            draft_checksum=canonical_workflow_draft_checksum_v1(spec=spec, canvas=canvas),
            updated_at=NOW,
        )
        self.definition = WorkflowDefinitionRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            name="Workflow",
            description="",
            status="active",
            current_published_version_id=None,
            revision=4,
            created_at=NOW,
            updated_at=NOW,
            current_published_version_number=None,
            draft_revision=4,
            draft_checksum=self.draft.draft_checksum,
        )
        self.active_slot_ids = frozenset(active_slot_ids)
        self.saved_commands: list[object] = []
        self.publish_commands: list[object] = []
        self.publish_records: dict[str, tuple[str, WorkflowVersionRecord]] = {}
        self.lock_calls: list[str] = []
        self.last_version: WorkflowVersionRecord | None = None
        self.draft_intents: dict[str, WorkflowDraftCredentialGrantIntentRecord] = {}
        self.version_grants: dict[str, WorkflowCredentialGrantRecord] = {}
        self.control_operations: dict[tuple[str, str, str], WorkflowControlOperationRecord] = {}

    async def get_control_operation(
        self,
        *,
        project_id,
        operation,
        idempotency_hash,
        request_digest,
        workflow_id=None,
        version_id=None,
        slot_id=None,
        lock_identity=True,
    ):
        assert project_id == PROJECT_ID
        assert lock_identity is True
        scope_key = canonical_workflow_control_scope_key(
            project_id=project_id,
            operation=operation,
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
        )
        record = self.control_operations.get((operation, scope_key, idempotency_hash))
        if record is not None and record.request_digest != request_digest:
            raise WorkflowControlIdempotencyConflict
        return record

    async def record_control_operation(self, command):
        assert type(command) is WorkflowControlOperationCreate
        payload = {item.name: getattr(command, item.name) for item in fields(WorkflowControlOperationCreate)}
        record = WorkflowControlOperationRecord(
            **payload,
            scope_key=command.scope_key,
            created_at=NOW,
        )
        self.control_operations[(record.operation, record.scope_key, record.idempotency_hash)] = record
        return record

    async def create_definition(self, *, project_id, actor_user_id, command):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        self.saved_commands.append(command)
        self.definition = WorkflowDefinitionRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            name=command.name,
            description=command.description,
            status="active",
            current_published_version_id=None,
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            draft_revision=1,
            draft_checksum=command.draft_checksum,
        )
        self.draft = WorkflowDraftRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec=command.materialize_spec(),
            canvas=command.materialize_canvas(),
            draft_checksum=command.draft_checksum,
            updated_at=NOW,
        )
        return self.definition, self.draft

    async def get_definition(self, project_id, workflow_id, *, lock: bool = False):
        assert project_id == PROJECT_ID
        assert workflow_id == WORKFLOW_ID
        if lock:
            self.lock_calls.append("definition")
        return self.definition

    async def list_definitions(self, project_id, query):
        assert project_id == PROJECT_ID
        return WorkflowDefinitionPage(
            items=(self.definition,),
            next_cursor=None,
        )

    async def update_definition(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        command,
    ):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        self.definition = replace(
            self.definition,
            name=command.name or self.definition.name,
            description=(command.description if command.description is not None else self.definition.description),
            revision=self.definition.revision + 1,
        )
        return self.definition

    async def archive_definition(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        command,
    ):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        self.definition = replace(
            self.definition,
            status="archived",
            revision=self.definition.revision + 1,
        )
        return self.definition

    async def get_draft(self, project_id, workflow_id, *, lock: bool = False):
        assert project_id == PROJECT_ID
        assert workflow_id == WORKFLOW_ID
        if lock:
            self.lock_calls.append("draft")
        return self.draft

    async def save_draft(self, *, project_id, actor_user_id, workflow_id, command):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        self.saved_commands.append(command)
        if command.expected_revision != self.draft.revision:
            raise WorkflowDraftCASConflict
        self.draft = WorkflowDraftRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            revision=self.draft.revision + 1,
            spec_schema_version=command.spec_schema_version,
            canvas_schema_version=command.canvas_schema_version,
            spec=command.materialize_spec(),
            canvas=command.materialize_canvas(),
            draft_checksum=command.draft_checksum,
            updated_at=NOW,
        )
        self.definition = replace(
            self.definition,
            revision=self.definition.revision + 1,
            draft_revision=self.draft.revision,
            draft_checksum=self.draft.draft_checksum,
        )
        return self.draft

    async def get_publish_replay(
        self,
        project_id,
        workflow_id,
        idempotency_hash,
        request_digest,
    ):
        assert project_id == PROJECT_ID
        assert workflow_id == WORKFLOW_ID
        existing = self.publish_records.get(idempotency_hash)
        if existing is None:
            return None
        previous_digest, record = existing
        if previous_digest != request_digest:
            raise WorkflowPublishIdempotencyConflict
        return record

    async def publish_version(self, *, project_id, actor_user_id, workflow_id, command):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        assert command.expected_draft_revision == self.draft.revision
        assert command.expected_draft_checksum == self.draft.draft_checksum
        existing = self.publish_records.get(command.idempotency_hash)
        if existing is not None:
            previous_digest, record = existing
            if previous_digest != command.request_digest:
                raise WorkflowPublishIdempotencyConflict
            return WorkflowVersionPublishResult(record=record, created=False)
        self.publish_commands.append(command)
        credential_slots = tuple(
            WorkflowVersionCredentialSlotRecord(
                slot_id=slot.slot_id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema=freeze_json(slot.materialize_payload_schema()),
                payload_schema_checksum=slot.payload_schema_checksum,
                required=True,
            )
            for slot in command.credential_slots
        )
        code_requirements = tuple(
            WorkflowVersionCodeRequirementRecord(
                node_id=item.node_id,
                runtime_contract=item.runtime_contract,
            )
            for item in command.code_requirements
        )
        http_requirements = tuple(
            WorkflowVersionHttpRequirementRecord(
                node_id=item.node_id,
                method=item.method,
                endpoint_policy_id=item.endpoint_policy_id,
                injection_profile_id=item.injection_profile_id,
                credential_slot_id=item.credential_slot_id,
            )
            for item in command.http_requirements
        )
        missing = tuple(slot.slot_id for slot in credential_slots if slot.slot_id not in self.active_slot_ids)
        record = WorkflowVersionRecord(
            version_id=uuid.UUID("30000000-0000-4000-8000-000000000001"),
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            version_number=1,
            graph_schema_version=command.graph_schema_version,
            canvas_schema_version=command.canvas_schema_version,
            compiler_contract_version=command.compiler_contract_version,
            semantic_checksum=command.semantic_checksum,
            published_at=NOW,
            spec=freeze_json(self.draft.spec),
            canvas=freeze_json(self.draft.canvas),
            credential_slots=credential_slots,
            code_requirements=code_requirements,
            http_requirements=http_requirements,
            missing_required_slot_ids=missing,
            executable=not missing,
        )
        self.publish_records[command.idempotency_hash] = (
            command.request_digest,
            record,
        )
        self.last_version = record
        return WorkflowVersionPublishResult(record=record, created=True)

    async def list_version_history(
        self,
        project_id,
        workflow_id,
        *,
        cursor,
        limit,
    ):
        assert project_id == PROJECT_ID
        assert workflow_id == WORKFLOW_ID
        return WorkflowVersionPage(
            items=() if self.last_version is None else (self.last_version,),
            next_cursor=None,
        )

    async def get_version(
        self,
        project_id,
        workflow_id,
        version_id,
        *,
        lock: bool = False,
    ):
        assert project_id == PROJECT_ID
        assert workflow_id == WORKFLOW_ID
        if self.last_version is None or self.last_version.version_id != version_id:
            return None
        return self.last_version

    async def put_draft_grant_intent(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        slot_id,
        resolved_draft_revision,
        command,
    ):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        assert resolved_draft_revision == self.draft.revision
        record = WorkflowDraftCredentialGrantIntentRecord(
            workflow_id=workflow_id,
            project_id=project_id,
            slot_id=slot_id,
            slot_schema_checksum=command.resolved_slot_schema_checksum,
            credential_id=command.credential_id,
            expected_credential_version_id=command.expected_credential_version_id,
            updated_at=NOW,
        )
        self.draft_intents[slot_id] = record
        return record

    async def delete_draft_grant_intent(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        slot_id,
        resolved_draft_revision,
    ):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        assert workflow_id == WORKFLOW_ID
        assert resolved_draft_revision == self.draft.revision
        return self.draft_intents.pop(slot_id, None)

    async def put_version_grant(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        version_id,
        slot_id,
        command,
    ):
        assert project_id == PROJECT_ID
        assert actor_user_id == str(USER_ID)
        record = WorkflowCredentialGrantRecord(
            grant_id=uuid.uuid4(),
            workflow_id=workflow_id,
            project_id=project_id,
            workflow_version_id=version_id,
            slot_id=slot_id,
            payload_schema_checksum=command.resolved_slot_schema_checksum,
            credential_id=command.credential_id,
            credential_version_id=command.expected_credential_version_id,
            status="active",
            revision=1,
            granted_by=actor_user_id,
            revoked_by=None,
            created_at=NOW,
            revoked_at=None,
        )
        self.version_grants[slot_id] = record
        return record

    async def revoke_version_grant(
        self,
        *,
        project_id,
        actor_user_id,
        workflow_id,
        version_id,
        slot_id,
    ):
        record = self.version_grants.get(slot_id)
        if record is None:
            return None
        record = replace(
            record,
            status="revoked",
            revision=record.revision + 1,
            revoked_by=actor_user_id,
            revoked_at=NOW,
        )
        self.version_grants[slot_id] = record
        return record


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[AuditAction, uuid.UUID]] = []

    async def record(self, _session, _context, *, action: AuditAction, target_id: uuid.UUID) -> None:
        self.events.append((action, target_id))


def _service(spec: dict[str, object], canvas: dict[str, object], *, authority: WorkflowDefinitionAuthoritySnapshot | None = None, active_slot_ids: tuple[str, ...] = (), denied: frozenset[WorkflowAction] = frozenset()):
    repository = _Repository(spec, canvas, active_slot_ids=active_slot_ids)
    authorizer = _Authorizer(denied=denied)
    authority_reader = _AuthorityReader(authority or _authority())
    audit = _Audit()
    service = WorkflowDefinitionControlService(
        authorizer=authorizer,
        repository_factory=lambda _session: repository,
        authority_reader=authority_reader,
        audit=audit,
    )
    return service, repository, authorizer, authority_reader, audit


def test_definition_transport_is_strict_but_allows_semantically_incomplete_drafts() -> None:
    partial = WorkflowDraftSaveRequestV1.model_validate(
        {
            "expected_revision": 4,
            "spec": {"schema_version": 1},
            "canvas": {"schema_version": 1},
        }
    )
    assert partial.spec.model_dump(mode="json", exclude_unset=True) == {"schema_version": 1}
    assert partial.canvas.model_dump(mode="json", exclude_unset=True) == {"schema_version": 1}

    forbidden_payloads = (
        {**partial.model_dump(mode="json"), "project_id": str(PROJECT_ID)},
        {**partial.model_dump(mode="json"), "owner_user_id": str(USER_ID)},
        {**partial.model_dump(mode="json"), "runtime": "python"},
        {**partial.model_dump(mode="json"), "executor": "host"},
        {**partial.model_dump(mode="json"), "secret": "plaintext"},
    )
    for payload in forbidden_payloads:
        with pytest.raises(ValidationError):
            WorkflowDraftSaveRequestV1.model_validate(payload)

    with pytest.raises(ValidationError):
        WorkflowDraftSpecV1.model_validate(
            {
                "schema_version": 1,
                "nodes": [
                    {
                        "id": CODE_ID,
                        "type": "python_code",
                        "type_version": 1,
                        "config": {"source": "return {}", "executor": "host"},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        WorkflowDraftCanvasV1.model_validate({"schema_version": 1, "viewport": {"x": 0, "y": 0}})

    for model in (WorkflowDraftValidateRequestV1, WorkflowPublishRequestV1):
        with pytest.raises(ValidationError):
            model.model_validate(
                {
                    "expected_revision": 4,
                    "expected_draft_checksum": "a" * 64,
                    "spec": {"schema_version": 1},
                }
            )

    for non_json_array in ((), set(), iter(()), ""):
        with pytest.raises(ValidationError):
            WorkflowDraftSpecV1.model_validate(
                {
                    "schema_version": 1,
                    "nodes": non_json_array,
                }
            )
    with pytest.raises(ValidationError):
        WorkflowDefinitionPageV1.model_validate({"items": (), "next_cursor": None})


@pytest.mark.asyncio
async def test_save_uses_server_canonical_checksum_and_maps_cas_without_accepting_checksum() -> None:
    original = _spec_payload()
    canvas = _canvas_payload(original)
    service, repository, _authorizer, _authority_reader, audit = _service(
        original,
        canvas,
    )
    reordered = copy.deepcopy(original)
    reordered = {key: reordered[key] for key in reversed(tuple(reordered))}
    request = WorkflowDraftSaveRequestV1.model_validate(
        {
            "expected_revision": 4,
            "spec": reordered,
            "canvas": canvas,
        }
    )

    response = await service.save_draft(object(), _private_context(), WORKFLOW_ID, request, idempotency_key="save-1")

    command = repository.saved_commands[0]
    assert command.draft_checksum == canonical_workflow_draft_checksum_v1(
        spec=original,
        canvas=canvas,
    )
    assert response.draft_checksum == command.draft_checksum
    assert response.revision == 5
    assert command.credential_slot_ids == ()
    assert audit.events == [(AuditAction.WORKFLOW_DRAFT_SAVED, WORKFLOW_ID)]
    assert "draft_checksum" not in WorkflowDraftSaveRequestV1.model_fields

    stale = request.model_copy(update={"expected_revision": 4})
    with pytest.raises(WorkflowDraftConflict):
        await service.save_draft(object(), _private_context(), WORKFLOW_ID, stale, idempotency_key="save-stale")


@pytest.mark.asyncio
async def test_validate_uses_exact_policy_catalog_and_g12_compiler_and_rejects_future_nodes() -> None:
    valid_spec = _spec_payload()
    valid_canvas = _canvas_payload(valid_spec)
    service, repository, _authorizer, authority_reader, _audit = _service(
        valid_spec,
        valid_canvas,
    )
    request = WorkflowDraftValidateRequestV1(
        expected_revision=repository.draft.revision,
        expected_draft_checksum=repository.draft.draft_checksum,
    )

    valid = await service.validate_draft(object(), _private_context(), WORKFLOW_ID, request)
    assert valid.valid is True
    assert valid.issues == ()
    assert valid.semantic_checksum is not None
    assert valid.requirements.node_types == ("start", "end")
    assert valid.requirements.requires_code is False
    assert valid.requirements.requires_http is False
    assert authority_reader.for_update == [False]

    future_spec = _spec_payload(middle=[_node(LLM_ID, "agent", {})])
    future_canvas = _canvas_payload(future_spec)
    service, repository, *_rest = _service(future_spec, future_canvas)
    invalid = await service.validate_draft(
        object(),
        _private_context(),
        WORKFLOW_ID,
        WorkflowDraftValidateRequestV1(
            expected_revision=repository.draft.revision,
            expected_draft_checksum=repository.draft.draft_checksum,
        ),
    )
    assert invalid.valid is False
    assert "WORKFLOW_NODE_TYPE_UNAVAILABLE" in {issue.code for issue in invalid.issues}
    assert invalid.semantic_checksum is None


@pytest.mark.asyncio
async def test_publish_uses_only_locked_draft_derives_closure_and_replays_idempotently() -> None:
    llm = _node(
        LLM_ID,
        "llm",
        {
            "model_ref": "reasoning-default",
            "mode": "chat",
            "context_input_ids": [],
            "messages": [
                {
                    "id": "prompt",
                    "role": "user",
                    "content": {
                        "version": 1,
                        "segments": [{"kind": "text", "value": "Hello"}],
                    },
                }
            ],
            "model_parameters": {},
            "stream": False,
            "reasoning_output": "omit",
            "structured_output": {"enabled": False, "schema": None},
        },
    )
    spec = _spec_payload(middle=[llm])
    canvas = _canvas_payload(spec)
    service, repository, authorizer, authority_reader, audit = _service(
        spec,
        canvas,
    )
    request = WorkflowPublishRequestV1(
        expected_revision=repository.draft.revision,
        expected_draft_checksum=repository.draft.draft_checksum,
    )

    first = await service.publish(
        object(),
        _private_context(),
        WORKFLOW_ID,
        request,
        idempotency_key="publish-once",
    )
    replay = await service.publish(
        object(),
        _private_context(),
        WORKFLOW_ID,
        request,
        idempotency_key="publish-once",
    )

    assert replay == first
    assert len(repository.publish_commands) == 1
    publish_command = repository.publish_commands[0]
    assert {field.name for field in fields(type(publish_command))} == {
        "expected_draft_revision",
        "expected_draft_checksum",
        "graph_schema_version",
        "canvas_schema_version",
        "compiler_contract_version",
        "semantic_checksum",
        "model_refs",
        "credential_slots",
        "code_requirements",
        "http_requirements",
        "idempotency_hash",
        "request_digest",
    }
    assert not hasattr(publish_command, "spec")
    assert not hasattr(publish_command, "canvas")
    assert [(str(ref.node_id), ref.purpose, ref.logical_model_name) for ref in publish_command.model_refs] == [(LLM_ID, "primary", "reasoning-default")]
    assert first.semantic_checksum == publish_command.semantic_checksum
    assert first.executable is True
    first_json = workflow_definition_response_public_projection_v1(first)
    assert first_json["spec"]["nodes"][1]["config"]["model_ref"] == ("reasoning-default")
    assert publish_command.idempotency_hash == (hash_workflow_publish_idempotency_key("publish-once"))
    assert audit.events == [(AuditAction.WORKFLOW_VERSION_PUBLISHED, WORKFLOW_ID)]
    assert (WorkflowAction.PUBLISH, True) in authorizer.calls
    assert all("publish-once" not in repr(event) for event in audit.events)
    assert authority_reader.for_update == [True]
    assert repository.lock_calls == ["definition", "draft"]


@pytest.mark.asyncio
async def test_missing_grant_allows_publish_but_version_is_not_executable_and_slot_checksum_is_server_derived() -> None:
    slot_schema = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    }
    http = _node(
        HTTP_ID,
        "http_request",
        {
            "method": "GET",
            "base_origin": "https://api.example.com",
            "path_template": {"version": 1, "segments": [{"kind": "text", "value": "/v1"}]},
            "query": [],
            "headers": [],
            "auth": {
                "mode": "endpoint_profile",
                "injection_profile_id": "bearer-v1",
                "credential_slot_id": "partner_api",
            },
            "body": {"kind": "none"},
            "timeout": {"connect_ms": None, "read_ms": 1000, "write_ms": None},
            "response": {
                "accepted_statuses": [{"from": 200, "to": 299}],
                "mode": "json",
                "schema": {"type": "object"},
            },
        },
    )
    spec = _spec_payload(middle=[http])
    spec["transitions"][1]["source"]["port_id"] = "success"
    spec["credential_slots"] = [
        {
            "id": "partner_api",
            "name": "Partner API",
            "purpose": "http_auth",
            "payload_schema": slot_schema,
            "required": True,
        }
    ]
    canvas = _canvas_payload(spec)
    service, repository, authorizer, _authority_reader, _audit = _service(
        spec,
        canvas,
        authority=_authority(http=True),
        active_slot_ids=(),
    )
    response = await service.publish(
        object(),
        _private_context(),
        WORKFLOW_ID,
        WorkflowPublishRequestV1(
            expected_revision=repository.draft.revision,
            expected_draft_checksum=repository.draft.draft_checksum,
        ),
        idempotency_key="publish-http",
    )

    assert response.executable is False
    expected = canonical_workflow_slot_schema_checksum_v1(slot_schema)
    assert response.missing_required_credential_slot_ids == ("partner_api",)
    assert response.credential_slots[0].payload_schema_checksum == expected
    publish_command = repository.publish_commands[0]
    assert publish_command.credential_slots[0].payload_schema_checksum == expected
    assert publish_command.http_requirements[0].node_id == uuid.UUID(HTTP_ID)
    assert publish_command.http_requirements[0].method == "GET"
    assert publish_command.http_requirements[0].endpoint_policy_id == "partner-api"
    assert publish_command.http_requirements[0].injection_profile_id == "bearer-v1"
    assert publish_command.http_requirements[0].credential_slot_id == "partner_api"
    assert (WorkflowAction.HTTP_USE, True) in authorizer.calls


@pytest.mark.asyncio
async def test_specialized_publish_requires_capability_and_matching_readiness_without_external_effects() -> None:
    code = _node(
        CODE_ID,
        "python_code",
        {
            "source": "def main(inputs):\n    return {}",
            "input_variables": [],
            "output_schema": {"type": "object"},
            "timeout_ms": 1000,
        },
    )
    spec = _spec_payload(middle=[code])
    canvas = _canvas_payload(spec)
    service, repository, *_rest = _service(
        spec,
        canvas,
        authority=_authority(code=True),
        denied=frozenset({WorkflowAction.CODE_USE}),
    )
    request = WorkflowPublishRequestV1(
        expected_revision=repository.draft.revision,
        expected_draft_checksum=repository.draft.draft_checksum,
    )
    with pytest.raises(WorkflowForbidden):
        await service.publish(
            object(),
            _private_context(),
            WORKFLOW_ID,
            request,
            idempotency_key="publish-code-denied",
        )

    service, repository, *_rest = _service(
        spec,
        canvas,
        authority=_authority(code=True, code_ready=False),
    )
    validation = await service.validate_draft(
        object(),
        _private_context(),
        WORKFLOW_ID,
        WorkflowDraftValidateRequestV1(
            expected_revision=repository.draft.revision,
            expected_draft_checksum=repository.draft.draft_checksum,
        ),
    )
    assert "WORKFLOW_CODE_PROFILE_UNAVAILABLE" in {issue.code for issue in validation.issues}
    with pytest.raises(WorkflowDraftInvalid):
        await service.publish(
            object(),
            _private_context(),
            WORKFLOW_ID,
            WorkflowPublishRequestV1(
                expected_revision=repository.draft.revision,
                expected_draft_checksum=repository.draft.draft_checksum,
            ),
            idempotency_key="publish-code-offline",
        )
    assert not hasattr(service, "model_client")
    assert not hasattr(service, "http_client")
    assert not hasattr(service, "sandbox_provider")

    service, repository, *_rest = _service(
        spec,
        canvas,
        authority=_authority(code=True),
    )
    await service.publish(
        object(),
        _private_context(),
        WORKFLOW_ID,
        WorkflowPublishRequestV1(
            expected_revision=repository.draft.revision,
            expected_draft_checksum=repository.draft.draft_checksum,
        ),
        idempotency_key="publish-code-ready",
    )
    assert repository.publish_commands[0].code_requirements[0].node_id == uuid.UUID(CODE_ID)
    assert repository.publish_commands[0].code_requirements[0].runtime_contract == "python3.12-v1"


def test_idempotency_and_audit_contracts_are_closed_and_publish_request_has_no_spec() -> None:
    assert tuple(WorkflowPublishRequestV1.model_fields) == (
        "expected_revision",
        "expected_draft_checksum",
    )
    assert tuple(WorkflowDraftValidateRequestV1.model_fields) == (
        "expected_revision",
        "expected_draft_checksum",
    )
    with pytest.raises(TypeError):
        WorkflowDefinitionControlService(
            authorizer=_Authorizer(),
            repository_factory=None,  # type: ignore[arg-type]
            authority_reader=_AuthorityReader(_authority()),
            audit=_Audit(),
        )


@pytest.mark.asyncio
async def test_complete_control_facade_and_create_use_server_default_partial_draft() -> None:
    expected_methods = {
        "list_definitions",
        "create_definition",
        "get_definition",
        "update_definition",
        "archive_definition",
        "get_draft",
        "save_draft",
        "validate_draft",
        "publish_draft",
        "list_versions",
        "get_version",
        "put_draft_grant_intent",
        "delete_draft_grant_intent",
        "put_version_grant",
        "revoke_version_grant",
    }
    assert expected_methods <= set(dir(WorkflowDefinitionControlService))

    spec = _spec_payload()
    canvas = _canvas_payload(spec)
    service, repository, _authorizer, _authority_reader, audit = _service(
        spec,
        canvas,
    )
    created = await service.create_definition(
        object(),
        _private_context(),
        name="Blank Workflow",
        description="",
        idempotency_key="create-blank",
    )
    command = repository.saved_commands[0]
    assert command.materialize_spec() == {"schema_version": 1}
    assert command.materialize_canvas() == {"schema_version": 1}
    assert command.draft_checksum == canonical_workflow_draft_checksum_v1(
        spec={"schema_version": 1},
        canvas={"schema_version": 1},
    )
    assert created.id == WORKFLOW_ID
    assert created.draft_revision == 1
    assert created.draft_checksum == command.draft_checksum
    replayed = await service.create_definition(
        object(),
        _private_context(),
        name="Blank Workflow",
        description="",
        idempotency_key="create-blank",
    )
    assert replayed == created
    assert len(repository.saved_commands) == 1
    assert audit.events == [(AuditAction.WORKFLOW_DEFINITION_CREATED, WORKFLOW_ID)]


@pytest.mark.asyncio
async def test_definition_receipts_replay_original_scalar_projection_after_later_mutations() -> None:
    spec = _spec_payload()
    canvas = _canvas_payload(spec)
    service, repository, _authorizer, _authority_reader, audit = _service(spec, canvas)

    created = await service.create_definition(
        object(),
        _private_context(),
        name="Original",
        description="Original description",
        idempotency_key="stable-create",
    )
    first_update = await service.update_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
        expected_revision=created.revision,
        name="First update",
        description="First description",
        idempotency_key="stable-update-one",
    )
    second_update = await service.update_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
        expected_revision=first_update.revision,
        name="Second update",
        description="Second description",
        idempotency_key="stable-update-two",
    )
    assert second_update.revision > first_update.revision

    create_replay = await service.create_definition(
        object(),
        _private_context(),
        name="Original",
        description="Original description",
        idempotency_key="stable-create",
    )
    update_replay = await service.update_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
        expected_revision=created.revision,
        name="First update",
        description="First description",
        idempotency_key="stable-update-one",
    )

    assert create_replay == created
    assert update_replay == first_update
    assert repository.definition.name == "Second update"
    assert audit.events == [
        (AuditAction.WORKFLOW_DEFINITION_CREATED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_DEFINITION_UPDATED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_DEFINITION_UPDATED, WORKFLOW_ID),
    ]


@pytest.mark.asyncio
async def test_definition_crud_and_version_history_return_gateway_safe_projections() -> None:
    spec = _spec_payload()
    canvas = _canvas_payload(spec)
    service, repository, _authorizer, _authority_reader, audit = _service(
        spec,
        canvas,
    )

    page = await service.list_definitions(
        object(),
        _private_context(),
        query=None,
        lifecycle="active",
        publication="all",
        sort="updated_desc",
        cursor=None,
        limit=50,
    )
    assert page.items[0].id == WORKFLOW_ID
    assert isinstance(
        workflow_definition_response_public_projection_v1(page)["items"],
        list,
    )
    fetched = await service.get_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
    )
    assert fetched.draft_checksum == repository.draft.draft_checksum
    updated = await service.update_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
        expected_revision=repository.definition.revision,
        name="Renamed",
        description=None,
        idempotency_key="update-name",
    )
    assert updated.name == "Renamed"
    assert (
        await service.update_definition(
            object(),
            _private_context(),
            WORKFLOW_ID,
            expected_revision=updated.revision - 1,
            name="Renamed",
            description=None,
            idempotency_key="update-name",
        )
        == updated
    )
    archived = await service.archive_definition(
        object(),
        _private_context(),
        WORKFLOW_ID,
        expected_revision=repository.definition.revision,
        idempotency_key="archive-definition",
    )
    assert archived.lifecycle == "archived"
    repository.definition = replace(
        repository.definition,
        name="Later projection",
        revision=repository.definition.revision + 1,
    )
    assert (
        await service.archive_definition(
            object(),
            _private_context(),
            WORKFLOW_ID,
            expected_revision=archived.revision - 1,
            idempotency_key="archive-definition",
        )
        == archived
    )
    assert audit.events == [
        (AuditAction.WORKFLOW_DEFINITION_UPDATED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_DEFINITION_ARCHIVED, WORKFLOW_ID),
    ]

    repository.definition = replace(repository.definition, status="active")
    published = await service.publish(
        object(),
        _private_context(),
        WORKFLOW_ID,
        WorkflowPublishRequestV1(
            expected_revision=repository.draft.revision,
            expected_draft_checksum=repository.draft.draft_checksum,
        ),
        idempotency_key="history-version",
    )
    history = await service.list_versions(
        object(),
        _private_context(),
        WORKFLOW_ID,
        cursor=None,
        limit=50,
    )
    version = await service.get_version(
        object(),
        _private_context(),
        WORKFLOW_ID,
        published.version_id,
    )
    assert history.items == (version,)


@pytest.mark.asyncio
async def test_grant_mutations_recompute_slot_checksum_and_emit_content_free_audit() -> None:
    slot_schema = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    }
    spec = _spec_payload()
    spec["credential_slots"] = [
        {
            "id": "partner_api",
            "name": "Partner API",
            "purpose": "http_auth",
            "payload_schema": slot_schema,
            "required": True,
        }
    ]
    canvas = _canvas_payload(spec)
    service, repository, _authorizer, _authority_reader, audit = _service(
        spec,
        canvas,
    )
    checksum = canonical_workflow_slot_schema_checksum_v1(slot_schema)
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()

    intent = await service.put_draft_grant_intent(
        object(),
        _private_context(),
        WORKFLOW_ID,
        "partner_api",
        credential_id=credential_id,
        expected_credential_version_id=credential_version_id,
        expected_slot_schema_checksum=checksum,
        idempotency_key="draft-grant-put",
    )
    assert intent.slot_schema_checksum == checksum
    assert (
        await service.put_draft_grant_intent(
            object(),
            _private_context(),
            WORKFLOW_ID,
            "partner_api",
            credential_id=credential_id,
            expected_credential_version_id=credential_version_id,
            expected_slot_schema_checksum=checksum,
            idempotency_key="draft-grant-put",
        )
        == intent
    )
    deleted = await service.delete_draft_grant_intent(
        object(),
        _private_context(),
        WORKFLOW_ID,
        "partner_api",
        idempotency_key="draft-grant-delete",
    )
    assert deleted.deleted is True
    assert (
        await service.delete_draft_grant_intent(
            object(),
            _private_context(),
            WORKFLOW_ID,
            "partner_api",
            idempotency_key="draft-grant-delete",
        )
        == deleted
    )
    assert (
        await service.put_draft_grant_intent(
            object(),
            _private_context(),
            WORKFLOW_ID,
            "partner_api",
            credential_id=credential_id,
            expected_credential_version_id=credential_version_id,
            expected_slot_schema_checksum=checksum,
            idempotency_key="draft-grant-put",
        )
        == intent
    )

    version_id = uuid.uuid4()
    repository.last_version = WorkflowVersionRecord(
        version_id=version_id,
        workflow_id=WORKFLOW_ID,
        project_id=PROJECT_ID,
        version_number=1,
        graph_schema_version=1,
        canvas_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="a" * 64,
        published_at=NOW,
        spec=freeze_json(spec),
        canvas=freeze_json(canvas),
        credential_slots=(
            WorkflowVersionCredentialSlotRecord(
                slot_id="partner_api",
                name="Partner API",
                purpose="http_auth",
                payload_schema=freeze_json(slot_schema),
                payload_schema_checksum=checksum,
                required=True,
            ),
        ),
        missing_required_slot_ids=("partner_api",),
        executable=False,
    )
    grant = await service.put_version_grant(
        object(),
        _private_context(),
        WORKFLOW_ID,
        version_id,
        "partner_api",
        credential_id=credential_id,
        expected_credential_version_id=credential_version_id,
        expected_slot_schema_checksum=checksum,
        idempotency_key="version-grant-put",
    )
    assert grant.status == "active"
    assert (
        await service.put_version_grant(
            object(),
            _private_context(),
            WORKFLOW_ID,
            version_id,
            "partner_api",
            credential_id=credential_id,
            expected_credential_version_id=credential_version_id,
            expected_slot_schema_checksum=checksum,
            idempotency_key="version-grant-put",
        )
        == grant
    )
    revoked = await service.revoke_version_grant(
        object(),
        _private_context(),
        WORKFLOW_ID,
        version_id,
        "partner_api",
        idempotency_key="version-grant-delete",
    )
    assert revoked.status == "revoked"
    assert (
        await service.revoke_version_grant(
            object(),
            _private_context(),
            WORKFLOW_ID,
            version_id,
            "partner_api",
            idempotency_key="version-grant-delete",
        )
        == revoked
    )
    assert (
        await service.put_version_grant(
            object(),
            _private_context(),
            WORKFLOW_ID,
            version_id,
            "partner_api",
            credential_id=credential_id,
            expected_credential_version_id=credential_version_id,
            expected_slot_schema_checksum=checksum,
            idempotency_key="version-grant-put",
        )
        == grant
    )
    assert audit.events == [
        (AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_UPDATED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_DELETED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_VERSION_GRANT_UPDATED, WORKFLOW_ID),
        (AuditAction.WORKFLOW_VERSION_GRANT_REVOKED, WORKFLOW_ID),
    ]


@pytest.mark.asyncio
async def test_draft_grant_intent_accepts_one_complete_slot_in_an_incomplete_draft() -> None:
    slot_schema = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    }
    partial_spec = {
        "schema_version": 1,
        "credential_slots": [
            {
                "id": "partner_api",
                "name": "Partner API",
                "purpose": "http_auth",
                "payload_schema": slot_schema,
                "required": True,
            }
        ],
    }
    partial_canvas = {"schema_version": 1}
    service, repository, _authorizer, _authority_reader, audit = _service(
        partial_spec,
        partial_canvas,
    )
    checksum = canonical_workflow_slot_schema_checksum_v1(slot_schema)

    intent = await service.put_draft_grant_intent(
        object(),
        _private_context(),
        WORKFLOW_ID,
        "partner_api",
        credential_id=uuid.uuid4(),
        expected_credential_version_id=uuid.uuid4(),
        expected_slot_schema_checksum=checksum,
        idempotency_key="partial-grant-put",
    )

    assert intent.slot_schema_checksum == checksum
    stored = repository.draft_intents["partner_api"]
    assert stored.slot_schema_checksum == intent.slot_schema_checksum
    assert stored.credential_id == intent.credential_id
    assert stored.expected_credential_version_id == intent.expected_credential_version_id
    assert audit.events == [
        (AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_UPDATED, WORKFLOW_ID),
    ]

    incomplete_slot_spec = copy.deepcopy(partial_spec)
    incomplete_slot_spec["credential_slots"][0].pop("payload_schema")
    incomplete_service, _repository, _authorizer, _authority_reader, _audit = _service(
        incomplete_slot_spec,
        partial_canvas,
    )
    with pytest.raises(WorkflowDraftInvalid):
        await incomplete_service.put_draft_grant_intent(
            object(),
            _private_context(),
            WORKFLOW_ID,
            "partner_api",
            credential_id=uuid.uuid4(),
            expected_credential_version_id=uuid.uuid4(),
            expected_slot_schema_checksum=checksum,
            idempotency_key="incomplete-grant-put",
        )


@pytest.mark.asyncio
async def test_draft_save_replays_exactly_once_conflicts_on_changed_body_and_rejects_advanced_state() -> None:
    spec = _spec_payload()
    canvas = _canvas_payload(spec)
    service, repository, _authorizer, _authority_reader, audit = _service(spec, canvas)
    request = WorkflowDraftSaveRequestV1.model_validate(
        {
            "expected_revision": repository.draft.revision,
            "spec": spec,
            "canvas": canvas,
        }
    )

    first = await service.save_draft(
        object(),
        _private_context(),
        WORKFLOW_ID,
        request,
        idempotency_key="save-replay",
    )
    replay = await service.save_draft(
        object(),
        _private_context(),
        WORKFLOW_ID,
        request,
        idempotency_key="save-replay",
    )
    assert replay == first
    assert len(repository.saved_commands) == 1
    assert audit.events == [(AuditAction.WORKFLOW_DRAFT_SAVED, WORKFLOW_ID)]

    changed = request.model_copy(update={"expected_revision": request.expected_revision + 1})
    with pytest.raises(WorkflowDraftConflict):
        await service.save_draft(
            object(),
            _private_context(),
            WORKFLOW_ID,
            changed,
            idempotency_key="save-replay",
        )

    repository.draft = replace(repository.draft, revision=repository.draft.revision + 1)
    with pytest.raises(WorkflowDraftConflict):
        await service.save_draft(
            object(),
            _private_context(),
            WORKFLOW_ID,
            request,
            idempotency_key="save-replay",
        )
    assert len(repository.saved_commands) == 1
    assert audit.events == [(AuditAction.WORKFLOW_DRAFT_SAVED, WORKFLOW_ID)]


def test_grant_mutation_body_is_strict_and_cannot_carry_secret_material() -> None:
    valid = {
        "credential_id": str(uuid.uuid4()),
        "expected_credential_version_id": str(uuid.uuid4()),
        "expected_slot_schema_checksum": "a" * 64,
    }
    parsed = WorkflowCredentialGrantMutationRequestV1.model_validate_json(__import__("json").dumps(valid))
    assert parsed.expected_slot_schema_checksum == "a" * 64
    for field, value in (
        ("secret", "plaintext"),
        ("scheme", "bearer"),
        ("header_value", "token"),
        ("envelope_id", str(uuid.uuid4())),
        ("project_id", str(PROJECT_ID)),
        ("owner_user_id", str(USER_ID)),
    ):
        with pytest.raises(ValidationError):
            WorkflowCredentialGrantMutationRequestV1.model_validate_json(__import__("json").dumps({**valid, field: value}))
