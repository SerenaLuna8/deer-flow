from __future__ import annotations

import asyncio
import hashlib
import json as json_module
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from support.private_thread_seed import seed_private_thread_database

from app.audit.models import AuditAction
from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.gateway import deps
from app.gateway.deps import (
    get_current_user_from_request,
    get_workflow_definition_service,
    project_session,
    workflow_project_context,
)
from app.gateway.routers import project_workflows
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings.models import LockedWorkflowRuntimePolicy
from app.system_runtime_settings.workflow_defaults import (
    default_workflow_runtime_policy,
)
from app.system_runtime_settings.workflow_runtime import (
    create_workflow_runtime_facet_readiness,
)
from app.workflows.authorization import (
    ProjectWorkflowCapabilityPolicy,
    WorkflowAuthorizationService,
)
from app.workflows.definition_contracts import (
    WorkflowCredentialGrantResponseV1,
    WorkflowDefinitionPageV1,
    WorkflowDefinitionResponseV1,
    WorkflowDraftGrantIntentDeleteResponseV1,
    WorkflowDraftGrantIntentResponseV1,
    WorkflowDraftResponseV1,
    WorkflowDraftSaveRequestV1,
    WorkflowDraftValidateRequestV1,
    WorkflowDraftValidationResponseV1,
    WorkflowPublishRequestV1,
    WorkflowPublishResponseV1,
    WorkflowVersionPageV1,
    WorkflowVersionResponseV1,
)
from app.workflows.definition_domain import WorkflowDefinitionAuthoritySnapshot
from app.workflows.definition_service import WorkflowDefinitionControlService, workflow_definition_repository_factory
from app.workflows.errors import (
    WorkflowDraftConflict,
    WorkflowDraftInvalid,
    WorkflowForbidden,
    WorkflowNotFound,
    WorkflowUnavailable,
)
from app.workflows.repository import (
    WorkflowDefinitionPage,
    WorkflowDefinitionRecord,
    WorkflowDraftRecord,
)
from app.workflows.runtime_policy import (
    WorkflowRuntimePolicyV1,
    workflow_runtime_policy_checksum,
)

PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000201")
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000202")
MEMBERSHIP_ID = uuid.UUID("00000000-0000-4000-8000-000000000203")
WORKFLOW_ID = uuid.UUID("00000000-0000-4000-8000-000000000204")
VERSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000205")
CREDENTIAL_ID = uuid.UUID("00000000-0000-4000-8000-000000000206")
CREDENTIAL_VERSION_ID = uuid.UUID("00000000-0000-4000-8000-000000000207")
NOW = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)


class _SpyTransaction:
    def __init__(self, session: _SpySession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        assert not self._session.active
        self._session.active = True
        self._session.begin_count += 1

    async def __aexit__(self, error_type, error, traceback) -> bool:
        del error, traceback
        assert self._session.active
        self._session.active = False
        if error_type is None:
            self._session.commit_count += 1
        else:
            self._session.rollback_count += 1
        return False


class _SpySession:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.active = False

    def begin(self) -> _SpyTransaction:
        return _SpyTransaction(self)


SESSION = _SpySession()


def _context(*capabilities: Capability, request_id: str = "req-g15") -> ProjectContext:
    return ProjectContext(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        membership_id=MEMBERSHIP_ID,
        role=ProjectRole.EDITOR,
        capabilities=frozenset(capabilities),
        membership_version=3,
        request_id=request_id,
    )


def _private_context(*capabilities: Capability, request_id: str = "req-g15") -> PrivateWorkContext:
    return PrivateWorkContext.from_project(_context(*capabilities, request_id=request_id))


def _definition() -> dict[str, object]:
    return {
        "id": WORKFLOW_ID,
        "name": "Order review",
        "description": "",
        "lifecycle": "active",
        "publication": "draft_only",
        "revision": 1,
        "current_published_version_id": None,
        "current_published_version_number": None,
        "draft_revision": 1,
        "draft_checksum": "a" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _draft() -> dict[str, object]:
    return {
        "workflow_id": WORKFLOW_ID,
        "revision": 1,
        "draft_checksum": "a" * 64,
        "spec": {"schema_version": 1},
        "canvas": {"schema_version": 1},
        "updated_at": NOW,
    }


def _version() -> dict[str, object]:
    return {
        "id": VERSION_ID,
        "workflow_id": WORKFLOW_ID,
        "version_number": 1,
        "graph_schema_version": 1,
        "canvas_schema_version": 1,
        "compiler_contract_version": 1,
        "semantic_checksum": "b" * 64,
        "spec": {"schema_version": 1},
        "canvas": {"schema_version": 1},
        "credential_slots": [],
        "missing_required_credential_slot_ids": [],
        "executable": True,
        "published_at": NOW,
    }


def _requirements() -> dict[str, object]:
    return {
        "node_types": ["start", "end"],
        "model_refs": [],
        "code": [],
        "http": [],
        "credential_slots": [],
        "requires_code": False,
        "requires_http": False,
        "requires_http_write": False,
    }


def _publish_receipt() -> dict[str, object]:
    return {
        "request_id": "req-g15",
        "workflow_id": WORKFLOW_ID,
        "version_id": VERSION_ID,
        "version_number": 1,
        "graph_schema_version": 1,
        "canvas_schema_version": 1,
        "compiler_contract_version": 1,
        "semantic_checksum": "b" * 64,
        "spec": {"schema_version": 1},
        "canvas": {"schema_version": 1},
        "credential_slots": [],
        "missing_required_credential_slot_ids": [],
        "executable": True,
        "published_at": NOW,
    }


def _minimal_workflow_documents() -> tuple[dict[str, object], dict[str, object]]:
    start_id = "10000000-0000-4000-8000-000000000001"
    end_id = "10000000-0000-4000-8000-000000000002"
    execution_policy = {
        "retry": {"mode": "none"},
        "on_error": {"mode": "fail_workflow"},
    }
    spec = {
        "schema_version": 1,
        "entry_node_id": start_id,
        "nodes": [
            {
                "id": start_id,
                "type": "start",
                "type_version": 1,
                "scope": {"kind": "root"},
                "custom_label": None,
                "description": None,
                "input_bindings": {},
                "execution_policy": execution_policy,
                "config": {},
            },
            {
                "id": end_id,
                "type": "end",
                "type_version": 1,
                "scope": {"kind": "root"},
                "custom_label": None,
                "description": None,
                "input_bindings": {},
                "execution_policy": execution_policy,
                "config": {},
            },
        ],
        "transitions": [
            {
                "id": "edge-1",
                "source": {"node_id": start_id, "port_id": "next"},
                "target": {"node_id": end_id, "port_id": "in"},
            }
        ],
        "workflow_inputs": [],
        "workflow_outputs": [],
        "credential_slots": [],
    }
    canvas = {
        "schema_version": 1,
        "node_layouts": [
            {"node_id": start_id, "position": {"x": 0, "y": 0}},
            {"node_id": end_id, "position": {"x": 100, "y": 0}},
        ],
        "edge_layouts": [{"edge_id": "edge-1", "routing": "bezier"}],
    }
    return spec, canvas


class _EnabledWorkflowAuthorityReader:
    def __init__(self) -> None:
        payload = default_workflow_runtime_policy().model_dump(mode="json")
        payload["enabled"] = True
        payload["admission_enabled"] = True
        policy = WorkflowRuntimePolicyV1.model_validate(payload)
        self.snapshot = WorkflowDefinitionAuthoritySnapshot(
            locked_policy=LockedWorkflowRuntimePolicy.create(
                policy_version_id=uuid.uuid4(),
                revision=1,
                schema_version=1,
                payload_checksum=workflow_runtime_policy_checksum(policy),
                value=policy,
            ),
            facets=create_workflow_runtime_facet_readiness(
                generic_ready=True,
                code_ready=False,
                http_ready=False,
            ),
        )

    async def read_current(
        self,
        session,
        *,
        for_update: bool,
    ) -> WorkflowDefinitionAuthoritySnapshot:
        del session, for_update
        return self.snapshot


class _FailPublishAudit:
    def __init__(self, delegate: OperationalAuditSink) -> None:
        self.delegate = delegate

    async def record(self, session, context, *, action, target_id) -> None:
        if action is AuditAction.WORKFLOW_VERSION_PUBLISHED:
            raise WorkflowUnavailable(context.request_id)
        await self.delegate.record(
            session,
            context,
            action=action,
            target_id=target_id,
        )


class _DefinitionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.invalid_validation = False

    def _call(self, operation: str, session, **kwargs):
        assert session is SESSION
        assert SESSION.active
        if self.error is not None:
            raise self.error
        self.calls.append((operation, kwargs))

    async def list_definitions(self, session, **kwargs):
        self._call("list_definitions", session, **kwargs)
        return WorkflowDefinitionPageV1.model_validate({"items": [_definition()], "next_cursor": None})

    async def create_definition(self, session, **kwargs):
        self._call("create_definition", session, **kwargs)
        return WorkflowDefinitionResponseV1.model_validate(_definition())

    async def get_definition(self, session, **kwargs):
        self._call("get_definition", session, **kwargs)
        return WorkflowDefinitionResponseV1.model_validate(_definition())

    async def update_definition(self, session, **kwargs):
        self._call("update_definition", session, **kwargs)
        return WorkflowDefinitionResponseV1.model_validate({**_definition(), "name": kwargs["name"], "revision": 2})

    async def get_draft(self, session, **kwargs):
        self._call("get_draft", session, **kwargs)
        return WorkflowDraftResponseV1.model_validate(_draft())

    async def save_draft(self, session, context, workflow_id, request, *, idempotency_key):
        self._call(
            "save_draft",
            session,
            context=context,
            workflow_id=workflow_id,
            request=request,
            idempotency_key=idempotency_key,
        )
        return WorkflowDraftResponseV1.model_validate({**_draft(), "revision": 2, "draft_checksum": "c" * 64})

    async def validate_draft(self, session, context, workflow_id, request):
        self._call(
            "validate_draft",
            session,
            context=context,
            workflow_id=workflow_id,
            request=request,
        )
        if self.invalid_validation:
            return WorkflowDraftValidationResponseV1.model_validate(
                {
                    "request_id": context.request_id,
                    "workflow_id": WORKFLOW_ID,
                    "draft_revision": request.expected_revision,
                    "draft_checksum": request.expected_draft_checksum,
                    "valid": False,
                    "issues": [
                        {
                            "severity": "error",
                            "code": "WORKFLOW_DRAFT_TRANSPORT_INCOMPLETE",
                            "message": "Workflow Draft is incomplete or invalid.",
                            "path": ("transport",),
                            "node_id": None,
                            "edge_id": None,
                            "port_id": None,
                        }
                    ],
                    "semantic_checksum": None,
                    "requirements": None,
                    "catalog_generation": None,
                    "policy_revision": None,
                }
            )
        return WorkflowDraftValidationResponseV1.model_validate(
            {
                "request_id": context.request_id,
                "workflow_id": WORKFLOW_ID,
                "draft_revision": request.expected_revision,
                "draft_checksum": request.expected_draft_checksum,
                "valid": True,
                "issues": [],
                "semantic_checksum": "b" * 64,
                "requirements": _requirements(),
                "catalog_generation": "e" * 64,
                "policy_revision": 1,
            }
        )

    async def publish(self, session, context, workflow_id, request, *, idempotency_key):
        self._call(
            "publish",
            session,
            context=context,
            workflow_id=workflow_id,
            request=request,
            idempotency_key=idempotency_key,
        )
        return WorkflowPublishResponseV1.model_validate(_publish_receipt())

    async def list_versions(self, session, **kwargs):
        self._call("list_versions", session, **kwargs)
        return WorkflowVersionPageV1.model_validate({"items": [_version()], "next_cursor": None})

    async def get_version(self, session, **kwargs):
        self._call("get_version", session, **kwargs)
        return WorkflowVersionResponseV1.model_validate(_version())

    async def put_draft_grant_intent(self, session, **kwargs):
        self._call("put_draft_grant_intent", session, **kwargs)
        return WorkflowDraftGrantIntentResponseV1.model_validate(
            {
                "workflow_id": WORKFLOW_ID,
                "slot_id": kwargs["slot_id"],
                "slot_schema_checksum": kwargs["expected_slot_schema_checksum"],
                "credential_id": kwargs["credential_id"],
                "expected_credential_version_id": kwargs["expected_credential_version_id"],
                "updated_at": NOW,
            }
        )

    async def delete_draft_grant_intent(self, session, **kwargs):
        self._call("delete_draft_grant_intent", session, **kwargs)
        return WorkflowDraftGrantIntentDeleteResponseV1.model_validate({"workflow_id": WORKFLOW_ID, "slot_id": kwargs["slot_id"], "deleted": True})

    async def put_version_grant(self, session, **kwargs):
        self._call("put_version_grant", session, **kwargs)
        return WorkflowCredentialGrantResponseV1.model_validate(
            {
                "workflow_id": WORKFLOW_ID,
                "workflow_version_id": VERSION_ID,
                "slot_id": kwargs["slot_id"],
                "payload_schema_checksum": kwargs["expected_slot_schema_checksum"],
                "credential_id": kwargs["credential_id"],
                "credential_version_id": kwargs["expected_credential_version_id"],
                "status": "active",
                "revision": 1,
                "created_at": NOW,
                "revoked_at": None,
            }
        )

    async def revoke_version_grant(self, session, **kwargs):
        self._call("revoke_version_grant", session, **kwargs)
        return WorkflowCredentialGrantResponseV1.model_validate(
            {
                "workflow_id": WORKFLOW_ID,
                "workflow_version_id": VERSION_ID,
                "slot_id": kwargs["slot_id"],
                "payload_schema_checksum": "d" * 64,
                "credential_id": CREDENTIAL_ID,
                "credential_version_id": CREDENTIAL_VERSION_ID,
                "status": "revoked",
                "revision": 2,
                "created_at": NOW,
                "revoked_at": NOW,
            }
        )

    async def archive_definition(self, session, **kwargs):
        self._call("archive_definition", session, **kwargs)
        return WorkflowDefinitionResponseV1.model_validate({**_definition(), "lifecycle": "archived", "revision": 2})


class _RealServiceAuthorizer:
    def __init__(self, current: ProjectContext) -> None:
        self.current = current

    async def require(self, session, context, action, *, lock):
        del action, lock
        assert session is SESSION and SESSION.active
        assert context == PrivateWorkContext.from_project(self.current)
        return self.current


class _RealServiceRepository:
    def __init__(self) -> None:
        self.definition = WorkflowDefinitionRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            name="Order review",
            description="",
            status="active",
            current_published_version_id=None,
            revision=1,
            created_at=NOW,
            updated_at=NOW,
            current_published_version_number=None,
            draft_revision=1,
            draft_checksum="a" * 64,
        )
        self.draft = WorkflowDraftRecord(
            workflow_id=WORKFLOW_ID,
            project_id=PROJECT_ID,
            revision=1,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec={"schema_version": 1},
            canvas={"schema_version": 1},
            draft_checksum="a" * 64,
            updated_at=NOW,
        )

    async def list_definitions(self, project_id, query):
        assert SESSION.active and project_id == PROJECT_ID
        assert query.lifecycle == "active"
        return WorkflowDefinitionPage((self.definition,), None)

    async def get_definition(self, project_id, workflow_id, *, lock=False):
        assert SESSION.active and not lock
        if (project_id, workflow_id) != (PROJECT_ID, WORKFLOW_ID):
            return None
        return self.definition


class _RealServiceAudit:
    async def record(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("read-only Definition operations must not audit")


def _app(service: _DefinitionService | None, context: ProjectContext) -> FastAPI:
    SESSION.reset()
    app = FastAPI()
    app.include_router(project_workflows.router)

    async def session_override():
        yield SESSION

    app.dependency_overrides[project_session] = session_override
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(id=USER_ID)
    app.dependency_overrides[workflow_project_context] = lambda: context
    if service is not None:
        app.dependency_overrides[get_workflow_definition_service] = lambda: service
    return app


def _postgres_app(
    factory: async_sessionmaker,
    service: WorkflowDefinitionControlService,
    context: ProjectContext,
) -> FastAPI:
    app = FastAPI()
    app.include_router(project_workflows.router)

    async def session_override():
        async with factory() as session:
            yield session

    app.dependency_overrides[project_session] = session_override
    app.dependency_overrides[workflow_project_context] = lambda: context
    app.dependency_overrides[get_workflow_definition_service] = lambda: service
    return app


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if headers is None and method in {"POST", "PUT", "PATCH", "DELETE"}:
        material = json_module.dumps(
            {"method": method, "path": path, "body": json},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        headers = {"Idempotency-Key": f"gateway-test-{hashlib.sha256(material).hexdigest()}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=json, headers=headers)


@pytest.mark.asyncio
async def test_definition_collection_is_cursor_strict_and_preserves_required_nulls() -> None:
    service = _DefinitionService()
    response = await _request(
        _app(service, _context(Capability.WORKFLOW_READ)),
        "GET",
        (f"/api/projects/{PROJECT_ID}/workflows?query=&lifecycle=archived&publication=published&sort=name_desc&cursor=opaque&limit=17"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["next_cursor"] is None
    assert payload["items"][0]["current_published_version_id"] is None
    assert service.calls == [
        (
            "list_definitions",
            {
                "context": _private_context(Capability.WORKFLOW_READ),
                "query": None,
                "lifecycle": "archived",
                "publication": "published",
                "sort": "name_desc",
                "cursor": "opaque",
                "limit": 17,
            },
        )
    ]


@pytest.mark.asyncio
async def test_real_definition_service_runs_through_asgi_with_request_session_repository() -> None:
    current = _context(Capability.WORKFLOW_READ)
    repository = _RealServiceRepository()
    service = WorkflowDefinitionControlService(
        authorizer=_RealServiceAuthorizer(current),
        repository_factory=lambda session: repository,
        audit=_RealServiceAudit(),
    )
    app = _app(service, current)  # type: ignore[arg-type]

    listed = await _request(
        app,
        "GET",
        f"/api/projects/{PROJECT_ID}/workflows",
    )
    fetched = await _request(
        app,
        "GET",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}",
    )

    assert listed.status_code == fetched.status_code == 200
    assert listed.json()["items"][0]["id"] == str(WORKFLOW_ID)
    assert fetched.json()["draft_checksum"] == "a" * 64
    assert SESSION.begin_count == SESSION.commit_count == 2
    assert SESSION.rollback_count == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_definition_gateway_commits_replays_and_rolls_back_publish_atomically(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    private = seed.owner_a
    context = ProjectContext(
        user_id=private.user_id,
        project_id=private.project_id,
        membership_id=private.membership_id,
        role=private.role,
        capabilities=private.capabilities,
        membership_version=private.membership_version,
        request_id="req-g15-pg-gateway",
    )
    audit_service = AuditService(
        seed.factory,
        AuditHmacKeyring(
            active_key_id="workflow-g15-v1",
            _keys={"workflow-g15-v1": b"g" * 32},
        ),
    )
    audit = OperationalAuditSink(
        audit_service,
        process_context=_bind_gateway_audit_process(audit_service),
    )
    authority_reader = _EnabledWorkflowAuthorityReader()
    authorizer = WorkflowAuthorizationService(
        policy=ProjectWorkflowCapabilityPolicy(),
    )

    service = WorkflowDefinitionControlService(
        authorizer=authorizer,
        repository_factory=workflow_definition_repository_factory,
        authority_reader=authority_reader,
        audit=audit,
    )
    failing_publish_service = WorkflowDefinitionControlService(
        authorizer=authorizer,
        repository_factory=workflow_definition_repository_factory,
        authority_reader=authority_reader,
        audit=_FailPublishAudit(audit),
    )
    app = _postgres_app(seed.factory, service, context)
    failing_app = _postgres_app(seed.factory, failing_publish_service, context)

    try:
        concurrent = await asyncio.gather(
            *(
                _request(
                    app,
                    "POST",
                    f"/api/projects/{context.project_id}/workflows",
                    json={"name": "Concurrent idempotent create", "description": ""},
                    headers={"Idempotency-Key": "g18-pg-concurrent-create"},
                )
                for _ in range(2)
            )
        )
        assert [response.status_code for response in concurrent] == [201, 201], [response.json() for response in concurrent]
        assert concurrent[0].json()["id"] == concurrent[1].json()["id"]
        changed_create = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows",
            json={"name": "Changed idempotent create", "description": ""},
            headers={"Idempotency-Key": "g18-pg-concurrent-create"},
        )
        assert changed_create.status_code == 409
        assert changed_create.json()["detail"]["code"] == "WORKFLOW_DRAFT_CONFLICT"

        created = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows",
            json={"name": "Postgres order review", "description": ""},
        )
        assert created.status_code == 201
        workflow_id = uuid.UUID(created.json()["id"])
        create_replay = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows",
            json={"name": "Postgres order review", "description": ""},
        )
        assert create_replay.status_code == 201
        assert create_replay.json() == created.json()

        update_path = f"/api/projects/{context.project_id}/workflows/{workflow_id}"
        first_update_body = {
            "expected_revision": created.json()["revision"],
            "name": "Postgres order review v2",
            "description": "first stable receipt",
        }
        first_update = await _request(
            app,
            "PATCH",
            update_path,
            json=first_update_body,
            headers={"Idempotency-Key": "g18-pg-update-one"},
        )
        assert first_update.status_code == 200
        second_update = await _request(
            app,
            "PATCH",
            update_path,
            json={
                "expected_revision": first_update.json()["revision"],
                "name": "Postgres order review v3",
                "description": "later mutable projection",
            },
            headers={"Idempotency-Key": "g18-pg-update-two"},
        )
        assert second_update.status_code == 200

        late_create_replay = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows",
            json={"name": "Postgres order review", "description": ""},
        )
        late_update_replay = await _request(
            app,
            "PATCH",
            update_path,
            json=first_update_body,
            headers={"Idempotency-Key": "g18-pg-update-one"},
        )
        assert late_create_replay.status_code == 201
        assert late_update_replay.status_code == 200
        assert late_create_replay.json() == created.json()
        assert late_update_replay.json() == first_update.json()

        draft_only_history = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/versions",
        )
        missing_history = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{uuid.uuid4()}/versions",
        )
        assert draft_only_history.status_code == 200
        assert draft_only_history.json() == {"items": [], "next_cursor": None}
        assert missing_history.status_code == 404
        assert missing_history.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"

        spec, canvas = _minimal_workflow_documents()
        saved = await _request(
            app,
            "PUT",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/draft",
            json={
                "expected_revision": 1,
                "spec": spec,
                "canvas": canvas,
            },
            headers={"Idempotency-Key": "g18-pg-save-one"},
        )
        assert saved.status_code == 200
        save_replay = await _request(
            app,
            "PUT",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/draft",
            json={
                "expected_revision": 1,
                "spec": spec,
                "canvas": canvas,
            },
            headers={"Idempotency-Key": "g18-pg-save-one"},
        )
        assert save_replay.status_code == 200
        assert save_replay.json() == saved.json()
        draft_revision = saved.json()["revision"]
        draft_checksum = saved.json()["draft_checksum"]

        validated = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/validate",
            json={
                "expected_revision": draft_revision,
                "expected_draft_checksum": draft_checksum,
            },
        )
        assert validated.status_code == 200
        assert validated.json()["valid"] is True

        publish_path = f"/api/projects/{context.project_id}/workflows/{workflow_id}/publish"
        publish_body = {
            "expected_revision": draft_revision,
            "expected_draft_checksum": draft_checksum,
        }
        failed = await _request(
            failing_app,
            "POST",
            publish_path,
            json=publish_body,
            headers={"Idempotency-Key": "g15-pg-publish"},
        )
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "WORKFLOW_UNAVAILABLE"

        async with seed.factory() as session:
            version_count = await session.scalar(
                sa.text("SELECT count(*) FROM workflow_versions WHERE workflow_id=:workflow"),
                {"workflow": workflow_id},
            )
            operation_count = await session.scalar(
                sa.text("SELECT count(*) FROM workflow_control_operations WHERE workflow_id=:workflow AND operation='publish'"),
                {"workflow": workflow_id},
            )
        assert version_count == operation_count == 0

        published = await _request(
            app,
            "POST",
            publish_path,
            json=publish_body,
            headers={"Idempotency-Key": "g15-pg-publish"},
        )
        replayed = await _request(
            app,
            "POST",
            publish_path,
            json=publish_body,
            headers={"Idempotency-Key": "g15-pg-publish"},
        )
        assert published.status_code == replayed.status_code == 201
        assert published.json()["version_id"] == replayed.json()["version_id"]

        advanced = await _request(
            app,
            "PUT",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/draft",
            json={
                "expected_revision": draft_revision,
                "spec": spec,
                "canvas": canvas,
            },
            headers={"Idempotency-Key": "g18-pg-save-two"},
        )
        assert advanced.status_code == 200
        stale_replay = await _request(
            app,
            "PUT",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/draft",
            json={
                "expected_revision": 1,
                "spec": spec,
                "canvas": canvas,
            },
            headers={"Idempotency-Key": "g18-pg-save-one"},
        )
        assert stale_replay.status_code == 409
        assert stale_replay.json()["detail"]["code"] == "WORKFLOW_DRAFT_CONFLICT"

        history = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/versions",
        )
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [published.json()["version_id"]]

        draft_only = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows",
            json={"name": "Postgres draft only", "description": ""},
        )
        assert draft_only.status_code == 201
        draft_history = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{draft_only.json()['id']}/versions",
        )
        missing_history = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{uuid.uuid4()}/versions",
        )
        assert draft_history.status_code == 200
        assert draft_history.json()["items"] == []
        assert missing_history.status_code == 404
        assert missing_history.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"

        current = await _request(
            app,
            "GET",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}",
        )
        archived = await _request(
            app,
            "POST",
            f"/api/projects/{context.project_id}/workflows/{workflow_id}/archive",
            json={"expected_revision": current.json()["revision"]},
        )
        assert archived.status_code == 200
        assert archived.json()["lifecycle"] == "archived"

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    sa.text(
                        """SELECT action, count(*)
                           FROM audit_logs
                           WHERE project_id=:project AND action LIKE 'workflow.%'
                           GROUP BY action"""
                    ),
                    {"project": context.project_id},
                )
            ).all()
            version_count = await session.scalar(
                sa.text("SELECT count(*) FROM workflow_versions WHERE workflow_id=:workflow"),
                {"workflow": workflow_id},
            )
            operation_count = await session.scalar(
                sa.text("SELECT count(*) FROM workflow_control_operations WHERE workflow_id=:workflow"),
                {"workflow": workflow_id},
            )
        assert dict(counts) == {
            "workflow.definition_archived": 1,
            "workflow.definition_created": 3,
            "workflow.definition_updated": 2,
            "workflow.draft_saved": 2,
            "workflow.version_published": 1,
        }
        assert version_count == 1
        assert operation_count == 7
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_create_read_update_and_archive_forward_only_closed_transport() -> None:
    service = _DefinitionService()
    context = _context(Capability.WORKFLOW_READ, Capability.WORKFLOW_EDIT)
    app = _app(service, context)

    created = await _request(
        app,
        "POST",
        f"/api/projects/{PROJECT_ID}/workflows",
        json={"name": "Order review", "description": ""},
        headers={"Idempotency-Key": "g18-pg-concurrent-create"},
    )
    read = await _request(app, "GET", f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}")
    updated = await _request(
        app,
        "PATCH",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}",
        json={"expected_revision": 1, "name": "Order review v2"},
    )
    archived = await _request(
        app,
        "POST",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/archive",
        json={"expected_revision": 1},
    )

    assert [created.status_code, read.status_code, updated.status_code, archived.status_code] == [201, 200, 200, 200]
    assert [call[0] for call in service.calls] == [
        "create_definition",
        "get_definition",
        "update_definition",
        "archive_definition",
    ]
    assert service.calls[0][1] == {
        "context": PrivateWorkContext.from_project(context),
        "name": "Order review",
        "description": "",
        "idempotency_key": "g18-pg-concurrent-create",
    }
    assert service.calls[2][1] == {
        "context": PrivateWorkContext.from_project(context),
        "workflow_id": WORKFLOW_ID,
        "expected_revision": 1,
        "name": "Order review v2",
        "description": None,
        "idempotency_key": service.calls[2][1]["idempotency_key"],
    }


@pytest.mark.asyncio
async def test_draft_save_validate_publish_and_version_history_are_cas_only() -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
    )
    app = _app(service, context)
    base = f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}"

    read = await _request(app, "GET", f"{base}/draft")
    saved = await _request(
        app,
        "PUT",
        f"{base}/draft",
        json={"expected_revision": 1, "spec": {"schema_version": 1}, "canvas": {"schema_version": 1}},
    )
    validated = await _request(
        app,
        "POST",
        f"{base}/validate",
        json={"expected_revision": 2, "expected_draft_checksum": "c" * 64},
    )
    published = await _request(
        app,
        "POST",
        f"{base}/publish",
        json={"expected_revision": 2, "expected_draft_checksum": "c" * 64},
        headers={"Idempotency-Key": "publish-order-review-v1"},
    )
    history = await _request(app, "GET", f"{base}/versions?cursor=next&limit=10")
    version = await _request(app, "GET", f"{base}/versions/{VERSION_ID}")

    assert [read.status_code, saved.status_code, validated.status_code, published.status_code, history.status_code, version.status_code] == [200, 200, 200, 201, 200, 200]
    publish_payload = published.json()
    assert publish_payload["spec"] == {"schema_version": 1}
    assert publish_payload["canvas"] == {"schema_version": 1}
    assert publish_payload["missing_required_credential_slot_ids"] == []
    for private_authority_field in (
        "requirements",
        "catalog_generation",
        "policy_revision",
    ):
        assert private_authority_field not in publish_payload
    save = next(call for call in service.calls if call[0] == "save_draft")[1]
    validation = next(call for call in service.calls if call[0] == "validate_draft")[1]
    publish = next(call for call in service.calls if call[0] == "publish")[1]
    assert type(save["request"]) is WorkflowDraftSaveRequestV1
    assert type(validation["request"]) is WorkflowDraftValidateRequestV1
    assert type(publish["request"]) is WorkflowPublishRequestV1
    assert publish["context"] == PrivateWorkContext.from_project(context)
    assert publish["workflow_id"] == WORKFLOW_ID
    assert publish["request"].expected_revision == 2
    assert publish["request"].expected_draft_checksum == "c" * 64
    assert publish["idempotency_key"] == "publish-order-review-v1"
    assert not hasattr(publish["request"], "spec") and not hasattr(publish["request"], "canvas")
    assert next(call for call in service.calls if call[0] == "list_versions")[1]["cursor"] == "next"


@pytest.mark.asyncio
async def test_invalid_validation_response_preserves_every_required_null() -> None:
    service = _DefinitionService()
    service.invalid_validation = True
    response = await _request(
        _app(service, _context(Capability.WORKFLOW_READ, Capability.WORKFLOW_EDIT)),
        "POST",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/validate",
        json={"expected_revision": 1, "expected_draft_checksum": "a" * 64},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    for field in (
        "semantic_checksum",
        "requirements",
        "catalog_generation",
        "policy_revision",
    ):
        assert field in payload and payload[field] is None
    assert payload["issues"][0]["node_id"] is None
    assert payload["issues"][0]["edge_id"] is None
    assert payload["issues"][0]["port_id"] is None


@pytest.mark.asyncio
async def test_grant_intent_and_version_grant_have_exact_safe_body_and_target_authority() -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
    )
    app = _app(service, context)
    body = {
        "credential_id": str(CREDENTIAL_ID),
        "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
        "expected_slot_schema_checksum": "d" * 64,
    }
    draft_path = f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/draft/credential-grant-intents/orders_api"
    version_path = f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/versions/{VERSION_ID}/credential-grants/orders_api"

    responses = [
        await _request(app, "PUT", draft_path, json=body),
        await _request(app, "DELETE", draft_path),
        await _request(app, "PUT", version_path, json=body),
        await _request(app, "DELETE", version_path),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert [call[0] for call in service.calls] == [
        "put_draft_grant_intent",
        "delete_draft_grant_intent",
        "put_version_grant",
        "revoke_version_grant",
    ]
    assert service.calls[0][1]["credential_id"] == CREDENTIAL_ID
    assert service.calls[0][1]["expected_credential_version_id"] == CREDENTIAL_VERSION_ID
    assert responses[3].json()["revoked_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_id", ["_Auth", "Auth", "foo:bar"])
async def test_grant_slot_path_accepts_the_definition_slot_contract(slot_id: str) -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
    )
    body = {
        "credential_id": str(CREDENTIAL_ID),
        "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
        "expected_slot_schema_checksum": "d" * 64,
    }

    response = await _request(
        _app(service, context),
        "PUT",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/draft/credential-grant-intents/{slot_id}",
        json=body,
    )

    assert response.status_code == 200
    assert response.json()["slot_id"] == slot_id
    assert service.calls[0][1]["slot_id"] == slot_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot_id",
    ["1Auth", "-Auth", ".Auth", ":Auth", "Auth!", "凭据", "A" * 129],
)
async def test_grant_slot_path_rejects_values_outside_the_definition_slot_contract(slot_id: str) -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
    )
    body = {
        "credential_id": str(CREDENTIAL_ID),
        "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
        "expected_slot_schema_checksum": "d" * 64,
    }

    response = await _request(
        _app(service, context),
        "PUT",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/draft/credential-grant-intents/{slot_id}",
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "WORKFLOW_INPUT_INVALID"
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "", {"name": "x", "description": "", "owner_user_id": str(USER_ID)}),
        (
            "PUT",
            f"/{WORKFLOW_ID}/draft",
            {"expected_revision": 1, "spec": {"schema_version": 1}, "canvas": {"schema_version": 1}, "executor": "worker"},
        ),
        (
            "PUT",
            f"/{WORKFLOW_ID}/draft",
            {
                "expected_revision": 1,
                "spec": {
                    "schema_version": 1,
                    "nodes": [
                        {
                            "type": "future_type",
                            "config": {"secret": "must-not-pass"},
                        }
                    ],
                },
                "canvas": {"schema_version": 1},
            },
        ),
        (
            "POST",
            f"/{WORKFLOW_ID}/publish",
            {"expected_revision": 1, "expected_draft_checksum": "a" * 64, "spec": {"schema_version": 1}},
        ),
        (
            "PUT",
            f"/{WORKFLOW_ID}/draft/credential-grant-intents/orders_api",
            {
                "credential_id": str(CREDENTIAL_ID),
                "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
                "expected_slot_schema_checksum": "d" * 64,
                "secret": "must-not-pass",
                "envelope_id": "must-not-pass",
            },
        ),
    ],
)
async def test_requests_reject_authority_runtime_executor_secret_and_independent_spec(
    method: str,
    path: str,
    body: dict[str, object],
) -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
    )
    response = await _request(
        _app(service, context),
        method,
        f"/api/projects/{PROJECT_ID}/workflows{path}",
        json=body,
        headers={"Idempotency-Key": "publish-key"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "WORKFLOW_INPUT_INVALID"
    assert set(response.json()["detail"]) == {"code", "message", "request_id"}
    assert service.calls == []


@pytest.mark.asyncio
async def test_publish_requires_bounded_idempotency_key() -> None:
    service = _DefinitionService()
    context = _context(Capability.WORKFLOW_READ, Capability.WORKFLOW_PUBLISH)
    path = f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/publish"
    body = {"expected_revision": 1, "expected_draft_checksum": "a" * 64}

    missing = await _request(_app(service, context), "POST", path, json=body, headers={})
    whitespace = await _request(_app(service, context), "POST", path, json=body, headers={"Idempotency-Key": "bad key"})

    assert missing.status_code == whitespace.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("POST", "", {"name": "Order review", "description": ""}),
        ("PATCH", f"/{WORKFLOW_ID}", {"expected_revision": 1, "name": "Order review v2"}),
        (
            "PUT",
            f"/{WORKFLOW_ID}/draft",
            {"expected_revision": 1, "spec": {"schema_version": 1}, "canvas": {"schema_version": 1}},
        ),
        ("POST", f"/{WORKFLOW_ID}/publish", {"expected_revision": 1, "expected_draft_checksum": "a" * 64}),
        (
            "PUT",
            f"/{WORKFLOW_ID}/draft/credential-grant-intents/orders_api",
            {
                "credential_id": str(CREDENTIAL_ID),
                "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
                "expected_slot_schema_checksum": "d" * 64,
            },
        ),
        ("DELETE", f"/{WORKFLOW_ID}/draft/credential-grant-intents/orders_api", None),
        (
            "PUT",
            f"/{WORKFLOW_ID}/versions/{VERSION_ID}/credential-grants/orders_api",
            {
                "credential_id": str(CREDENTIAL_ID),
                "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
                "expected_slot_schema_checksum": "d" * 64,
            },
        ),
        ("DELETE", f"/{WORKFLOW_ID}/versions/{VERSION_ID}/credential-grants/orders_api", None),
        ("POST", f"/{WORKFLOW_ID}/archive", {"expected_revision": 1}),
    ],
)
async def test_every_definition_state_mutation_requires_idempotency_header(
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    service = _DefinitionService()
    context = _context(
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
    )
    response = await _request(
        _app(service, context),
        method,
        f"/api/projects/{PROJECT_ID}/workflows{suffix}",
        json=body,
        headers={},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "WORKFLOW_INPUT_INVALID"
    assert service.calls == []


@pytest.mark.asyncio
async def test_list_rejects_unknown_authority_query_and_bounds_limit() -> None:
    service = _DefinitionService()
    app = _app(service, _context(Capability.WORKFLOW_READ))
    base = f"/api/projects/{PROJECT_ID}/workflows"

    forged = await _request(app, "GET", f"{base}?owner_user_id={USER_ID}")
    oversized = await _request(app, "GET", f"{base}?limit=101")

    assert forged.status_code == oversized.status_code == 422
    assert forged.json()["detail"]["code"] == "WORKFLOW_INPUT_INVALID"
    assert service.calls == []


@pytest.mark.asyncio
async def test_definition_and_version_cursors_accept_1024_visible_ascii_but_not_1025() -> None:
    service = _DefinitionService()
    app = _app(service, _context(Capability.WORKFLOW_READ))
    base = f"/api/projects/{PROJECT_ID}/workflows"
    exact = "a" * 1024
    oversized = "a" * 1025

    definition_ok = await _request(app, "GET", f"{base}?cursor={exact}")
    definition_bad = await _request(app, "GET", f"{base}?cursor={oversized}")
    version_ok = await _request(
        app,
        "GET",
        f"{base}/{WORKFLOW_ID}/versions?cursor={exact}",
    )
    version_bad = await _request(
        app,
        "GET",
        f"{base}/{WORKFLOW_ID}/versions?cursor={oversized}",
    )

    assert definition_ok.status_code == version_ok.status_code == 200
    assert definition_bad.status_code == version_bad.status_code == 422
    assert [call[0] for call in service.calls] == [
        "list_definitions",
        "list_versions",
    ]


@pytest.mark.asyncio
async def test_missing_definition_service_is_a_stable_retryable_503() -> None:
    response = await _request(
        _app(None, _context(Capability.WORKFLOW_READ)),
        "GET",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}",
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "WORKFLOW_UNAVAILABLE"
    assert set(response.json()["detail"]) == {"code", "message", "request_id"}


@pytest.mark.asyncio
async def test_definition_route_owns_commit_and_rolls_back_the_whole_service_failure() -> None:
    service = _DefinitionService()
    app = _app(service, _context(Capability.WORKFLOW_READ))
    path = f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}"

    success = await _request(app, "GET", path)
    service.error = WorkflowDraftConflict("req-transaction")
    failure = await _request(app, "GET", path)

    assert success.status_code == 200
    assert failure.status_code == 409
    assert SESSION.begin_count == 2
    assert SESSION.commit_count == 1
    assert SESSION.rollback_count == 1
    assert SESSION.active is False


@pytest.mark.asyncio
async def test_initial_project_context_read_ends_before_definition_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SESSION.reset()
    expected = _context(Capability.WORKFLOW_READ, request_id="req-context-boundary")

    async def resolve(session, user_id, project_id, request_id):
        assert session is SESSION
        assert SESSION.active
        assert (user_id, project_id, request_id) == (
            USER_ID,
            PROJECT_ID,
            "req-context-boundary",
        )
        return expected

    monkeypatch.setattr(deps, "resolve_project_context", resolve)
    monkeypatch.setattr(deps, "get_current_trace_id", lambda: "req-context-boundary")

    result = await workflow_project_context(
        PROJECT_ID,
        user=SimpleNamespace(id=USER_ID),
        session=SESSION,
    )

    assert result is expected
    assert SESSION.begin_count == 1
    assert SESSION.commit_count == 1
    assert SESSION.rollback_count == 0
    assert SESSION.active is False


@pytest.mark.asyncio
async def test_publish_invalid_is_exact_422_and_rolls_back_without_content_leak() -> None:
    service = _DefinitionService()
    service.error = WorkflowDraftInvalid("req-publish-invalid")
    response = await _request(
        _app(
            service,
            _context(
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_PUBLISH,
                request_id="req-publish-invalid",
            ),
        ),
        "POST",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}/publish",
        json={
            "expected_revision": 1,
            "expected_draft_checksum": "a" * 64,
        },
        headers={"Idempotency-Key": "publish-invalid"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "WORKFLOW_DRAFT_INVALID",
            "message": "Workflow draft is invalid.",
            "request_id": "req-publish-invalid",
        }
    }
    assert SESSION.commit_count == 0
    assert SESSION.rollback_count == 1
    lowered = response.text.lower()
    for forbidden in ("spec", "source", "secret", "credential", "traceback"):
        assert forbidden not in lowered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "capabilities", "body"),
    [
        ("", "POST", (Capability.WORKFLOW_READ,), {"name": "x", "description": ""}),
        (
            f"/{WORKFLOW_ID}/publish",
            "POST",
            (Capability.WORKFLOW_READ, Capability.WORKFLOW_EDIT),
            {"expected_revision": 1, "expected_draft_checksum": "a" * 64},
        ),
        (
            f"/{WORKFLOW_ID}/draft/credential-grant-intents/orders_api",
            "PUT",
            (Capability.WORKFLOW_READ, Capability.WORKFLOW_EDIT),
            {
                "credential_id": str(CREDENTIAL_ID),
                "expected_credential_version_id": str(CREDENTIAL_VERSION_ID),
                "expected_slot_schema_checksum": "d" * 64,
            },
        ),
    ],
)
async def test_mutations_reauthorize_closed_project_capability_conjunctions(
    path: str,
    method: str,
    capabilities: tuple[Capability, ...],
    body: dict[str, object],
) -> None:
    service = _DefinitionService()
    response = await _request(
        _app(service, _context(*capabilities, request_id="req-denied")),
        method,
        f"/api/projects/{PROJECT_ID}/workflows{path}",
        json=body,
        headers={"Idempotency-Key": "publish-key"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "WORKFLOW_FORBIDDEN",
            "message": "Workflow action is forbidden.",
            "request_id": "req-denied",
        }
    }
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (WorkflowNotFound("req-error"), 404, "WORKFLOW_NOT_FOUND"),
        (WorkflowForbidden("req-error"), 403, "WORKFLOW_FORBIDDEN"),
        (WorkflowDraftConflict("req-error"), 409, "WORKFLOW_DRAFT_CONFLICT"),
        (WorkflowDraftInvalid("req-error"), 422, "WORKFLOW_DRAFT_INVALID"),
        (WorkflowUnavailable("req-error"), 503, "WORKFLOW_UNAVAILABLE"),
    ],
)
async def test_domain_errors_are_stable_content_free_and_retryable_only_when_unavailable(
    error: Exception,
    status: int,
    code: str,
) -> None:
    service = _DefinitionService()
    service.error = error
    response = await _request(
        _app(service, _context(Capability.WORKFLOW_READ, request_id="req-error")),
        "GET",
        f"/api/projects/{PROJECT_ID}/workflows/{WORKFLOW_ID}",
    )

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert set(response.json()["detail"]) == {"code", "message", "request_id"}
    assert (response.headers.get("retry-after") == "1") is (status == 503)
    assert "secret" not in response.text and "credential" not in response.text.lower()


def test_g15_routes_are_complete_and_static_routes_remain_first() -> None:
    methods_by_path = {(route.path, method) for route in project_workflows.router.routes for method in route.methods}
    prefix = "/api/projects/{project_id}/workflows"
    expected = {
        (prefix + "/readiness", "GET"),
        (prefix + "/node-catalog", "GET"),
        (prefix, "GET"),
        (prefix, "POST"),
        (prefix + "/{workflow_id}", "GET"),
        (prefix + "/{workflow_id}", "PATCH"),
        (prefix + "/{workflow_id}/draft", "GET"),
        (prefix + "/{workflow_id}/draft", "PUT"),
        (prefix + "/{workflow_id}/validate", "POST"),
        (prefix + "/{workflow_id}/publish", "POST"),
        (prefix + "/{workflow_id}/versions", "GET"),
        (prefix + "/{workflow_id}/versions/{version_id}", "GET"),
        (prefix + "/{workflow_id}/draft/credential-grant-intents/{slot_id}", "PUT"),
        (prefix + "/{workflow_id}/draft/credential-grant-intents/{slot_id}", "DELETE"),
        (prefix + "/{workflow_id}/versions/{version_id}/credential-grants/{slot_id}", "PUT"),
        (prefix + "/{workflow_id}/versions/{version_id}/credential-grants/{slot_id}", "DELETE"),
        (prefix + "/{workflow_id}/archive", "POST"),
    }

    assert expected <= methods_by_path
    assert [route.path for route in project_workflows.router.routes[:2]] == [
        prefix + "/readiness",
        prefix + "/node-catalog",
    ]
