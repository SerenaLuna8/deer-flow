"""Application control service for Workflow Draft validation and publication.

The caller owns the database transaction.  This service has no Model client,
HTTP client, Sandbox provider, Worker handle, or commit path.  Persistence,
durable idempotency, grants and content-free audit are narrow injected ports so
all publication state can be committed or rolled back together.
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditAction
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.system_runtime_settings.materializer import SystemRuntimePolicyMaterializer
from app.system_runtime_settings.workflow_runtime import WorkflowRuntimeConvergence
from app.workflows.authorization import WorkflowAction
from app.workflows.catalog_contracts import (
    WorkflowCatalogCapabilityProjectionV1,
    build_project_node_catalog_v1,
)
from app.workflows.compiler_policy import workflow_compilation_limits_from_graph_policy
from app.workflows.contracts import WorkflowValidationIssueV1
from app.workflows.definition_contracts import (
    WorkflowCredentialGrantMutationRequestV1,
    WorkflowCredentialGrantResponseV1,
    WorkflowDefinitionArchiveRequestV1,
    WorkflowDefinitionCreateRequestV1,
    WorkflowDefinitionListQueryV1,
    WorkflowDefinitionPageV1,
    WorkflowDefinitionResponseV1,
    WorkflowDefinitionUpdateRequestV1,
    WorkflowDraftGrantIntentDeleteResponseV1,
    WorkflowDraftGrantIntentResponseV1,
    WorkflowDraftResponseV1,
    WorkflowDraftSaveRequestV1,
    WorkflowDraftSpecV1,
    WorkflowDraftValidateRequestV1,
    WorkflowDraftValidationResponseV1,
    WorkflowPublishedCredentialSlotV1,
    WorkflowPublishedRequirementsV1,
    WorkflowPublishRequestV1,
    WorkflowPublishResponseV1,
    WorkflowVersionListQueryV1,
    WorkflowVersionPageV1,
    WorkflowVersionResponseV1,
    _trusted_definition_contract_validate,
    workflow_draft_canvas_public_projection_v1,
    workflow_draft_spec_public_projection_v1,
)
from app.workflows.definition_domain import (
    WorkflowDefinitionAuthoritySnapshot,
    WorkflowDefinitionDependencyError,
    WorkflowDefinitionValidationArtifact,
    canonical_workflow_control_request_digest_v1,
    canonical_workflow_draft_checksum_v1,
    canonical_workflow_publish_request_digest_v1,
    canonical_workflow_slot_schema_checksum_v1,
    derive_workflow_published_requirements_v1,
)
from app.workflows.errors import (
    WorkflowDraftConflict,
    WorkflowDraftInvalid,
    WorkflowInputInvalid,
    WorkflowNotFound,
    WorkflowUnavailable,
)
from app.workflows.repository import (
    WorkflowAuthorityMissing,
    WorkflowCodeRequirementCreate,
    WorkflowControlIdempotencyConflict,
    WorkflowControlOperationCreate,
    WorkflowControlOperationRecord,
    WorkflowCredentialGrantConflict,
    WorkflowCredentialGrantPut,
    WorkflowCredentialGrantRecord,
    WorkflowCredentialSlotCreate,
    WorkflowDefinitionArchive,
    WorkflowDefinitionConflict,
    WorkflowDefinitionCreate,
    WorkflowDefinitionListQuery,
    WorkflowDefinitionPage,
    WorkflowDefinitionRecord,
    WorkflowDefinitionUpdate,
    WorkflowDraftCASConflict,
    WorkflowDraftCredentialGrantIntentRecord,
    WorkflowDraftRecord,
    WorkflowDraftUpdate,
    WorkflowHttpRequirementCreate,
    WorkflowModelRefCreate,
    WorkflowPublishIdempotencyConflict,
    WorkflowRepository,
    WorkflowVersionPage,
    WorkflowVersionPublish,
    WorkflowVersionPublishResult,
    WorkflowVersionRecord,
    canonical_workflow_control_scope_key,
    hash_workflow_control_idempotency_key,
    hash_workflow_publish_idempotency_key,
)
from deerflow.workflows import CanvasDocumentV1, WorkflowCredentialSlotDecl, WorkflowSpecV1
from deerflow.workflows.compiler import (
    CURRENT_COMPILER_CONTRACT_VERSION,
    GRAPH_SCHEMA_VERSION_V1,
    WorkflowCompilerUnavailableError,
    compile_workflow,
)
from deerflow.workflows.validation import (
    WorkflowValidationError,
    WorkflowValidationIssue,
    validate_canvas_document,
)

_SLOT_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")


class WorkflowDefinitionAuthorizationPort(Protocol):
    async def require(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        action: WorkflowAction,
        *,
        lock: bool,
    ) -> ProjectContext: ...


class WorkflowDefinitionRepositoryPort(Protocol):
    async def get_control_operation(
        self,
        *,
        project_id: uuid.UUID,
        operation: str,
        idempotency_hash: str,
        request_digest: str,
        workflow_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
        slot_id: str | None = None,
        lock_identity: bool = True,
    ) -> WorkflowControlOperationRecord | None: ...

    async def record_control_operation(
        self,
        command: WorkflowControlOperationCreate,
    ) -> WorkflowControlOperationRecord: ...

    async def list_definitions(
        self,
        project_id: uuid.UUID,
        query: WorkflowDefinitionListQuery,
    ) -> WorkflowDefinitionPage: ...

    async def create_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        command: WorkflowDefinitionCreate,
    ) -> tuple[WorkflowDefinitionRecord, WorkflowDraftRecord]: ...

    async def get_definition(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowDefinitionRecord | None: ...

    async def update_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDefinitionUpdate,
    ) -> WorkflowDefinitionRecord: ...

    async def archive_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDefinitionArchive,
    ) -> WorkflowDefinitionRecord: ...

    async def get_draft(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowDraftRecord | None: ...

    async def save_draft(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDraftUpdate,
    ) -> WorkflowDraftRecord: ...

    async def publish_version(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowVersionPublish,
    ) -> WorkflowVersionPublishResult: ...

    async def get_publish_replay(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        idempotency_hash: str,
        request_digest: str,
    ) -> WorkflowVersionRecord | None: ...

    async def list_version_history(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> WorkflowVersionPage: ...

    async def get_version(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowVersionRecord | None: ...

    async def put_draft_grant_intent(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        slot_id: str,
        resolved_draft_revision: int,
        command: WorkflowCredentialGrantPut,
    ) -> WorkflowDraftCredentialGrantIntentRecord: ...

    async def delete_draft_grant_intent(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        slot_id: str,
        resolved_draft_revision: int,
    ) -> WorkflowDraftCredentialGrantIntentRecord | None: ...

    async def put_version_grant(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
        command: WorkflowCredentialGrantPut,
    ) -> WorkflowCredentialGrantRecord: ...

    async def revoke_version_grant(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
    ) -> WorkflowCredentialGrantRecord | None: ...


class WorkflowDefinitionRepositoryFactoryPort(Protocol):
    def __call__(
        self,
        session: AsyncSession,
    ) -> WorkflowDefinitionRepositoryPort: ...


class WorkflowDefinitionAuthorityReaderPort(Protocol):
    async def read_current(
        self,
        session: AsyncSession,
        *,
        for_update: bool,
    ) -> WorkflowDefinitionAuthoritySnapshot: ...


class PostgresWorkflowDefinitionAuthorityReader:
    """Materialize the exact current policy and convergence in one session."""

    def __init__(
        self,
        *,
        convergence: WorkflowRuntimeConvergence | None = None,
    ) -> None:
        self._convergence = convergence if convergence is not None else WorkflowRuntimeConvergence()

    async def read_current(
        self,
        session: AsyncSession,
        *,
        for_update: bool,
    ) -> WorkflowDefinitionAuthoritySnapshot:
        locked = await SystemRuntimePolicyMaterializer.materialize_workflow_runtime_current_locked_in_session(
            session,
            for_update=for_update,
        )
        facets = await self._convergence.read_facets_in_session(session, locked)
        return WorkflowDefinitionAuthoritySnapshot(
            locked_policy=locked,
            facets=facets,
        )


def workflow_definition_repository_factory(
    session: AsyncSession,
) -> WorkflowDefinitionRepositoryPort:
    """Create the caller-session-bound persistence adapter exactly once."""

    return WorkflowRepository(session)


class WorkflowDefinitionAuditPort(Protocol):
    async def record(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        action: AuditAction,
        target_id: uuid.UUID,
    ) -> None: ...


_ISSUE_MESSAGES = {
    "WORKFLOW_NODE_TYPE_UNAVAILABLE": "Workflow node type is unavailable.",
    "WORKFLOW_NODE_VERSION_UNAVAILABLE": "Workflow node version is unavailable.",
    "WORKFLOW_DRAFT_CHECKSUM_INVALID": "Workflow Draft integrity check failed.",
    "WORKFLOW_DRAFT_TRANSPORT_INCOMPLETE": "Workflow Draft is incomplete or invalid.",
    "WORKFLOW_CANVAS_TRANSPORT_INCOMPLETE": "Workflow Canvas is incomplete or invalid.",
    "WORKFLOW_DISABLED": "Workflow is disabled.",
    "WORKFLOW_NODE_CAPABILITY_REQUIRED": "Workflow node capability is required.",
    "WORKFLOW_NODE_NOT_ALLOWED": "Workflow node is not allowed by policy.",
    "WORKFLOW_CODE_DISABLED": "Workflow Code is disabled.",
    "WORKFLOW_CODE_PROFILE_UNAVAILABLE": "Workflow Code profile is unavailable.",
    "WORKFLOW_HTTP_DISABLED": "Workflow HTTP is disabled.",
    "WORKFLOW_HTTP_PROFILE_UNAVAILABLE": "Workflow HTTP profile is unavailable.",
    "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN": "Workflow HTTP endpoint is not allowed.",
    "WORKFLOW_CREDENTIAL_SLOT_SCHEMA_INVALID": "Workflow Credential slot schema is invalid.",
    "WORKFLOW_COMPILER_UNAVAILABLE": "Workflow compiler is unavailable.",
}


def _safe_issue(
    code: str,
    *,
    phase: str = "dependency",
    node_id: str | None = None,
    edge_id: str | None = None,
    port_id: str | None = None,
) -> WorkflowValidationIssueV1:
    return WorkflowValidationIssueV1(
        severity="error",
        code=code,
        message=_ISSUE_MESSAGES.get(code, "Workflow validation failed."),
        path=(phase,),
        node_id=None if node_id is None else uuid.UUID(node_id),
        edge_id=edge_id,
        port_id=port_id,
    )


def _compiler_issue(issue: WorkflowValidationIssue) -> WorkflowValidationIssueV1:
    return _safe_issue(
        issue.code,
        phase=issue.phase,
        node_id=issue.node_id,
        edge_id=issue.transition_id,
        port_id=issue.port_id,
    )


def _sorted_issues(
    issues: list[WorkflowValidationIssueV1],
) -> tuple[WorkflowValidationIssueV1, ...]:
    unique: dict[tuple[object, ...], WorkflowValidationIssueV1] = {}
    for issue in issues:
        key = (
            issue.code,
            issue.path,
            str(issue.node_id) if issue.node_id is not None else "",
            issue.edge_id or "",
            issue.port_id or "",
        )
        unique.setdefault(key, issue)
    return tuple(unique[key] for key in sorted(unique))


class WorkflowDefinitionControlService:
    def __init__(
        self,
        *,
        authorizer: WorkflowDefinitionAuthorizationPort,
        repository_factory: WorkflowDefinitionRepositoryFactoryPort = workflow_definition_repository_factory,
        authority_reader: WorkflowDefinitionAuthorityReaderPort | None = None,
        audit: WorkflowDefinitionAuditPort,
    ) -> None:
        dependencies = (
            authorizer,
            repository_factory,
            audit,
        )
        if any(dependency is None for dependency in dependencies):
            raise TypeError("Workflow Definition control ports are required")
        self._authorizer = authorizer
        self._repository_factory = repository_factory
        self._authority_reader = authority_reader if authority_reader is not None else PostgresWorkflowDefinitionAuthorityReader()
        self._audit = audit

    def _repository(
        self,
        session: AsyncSession,
    ) -> WorkflowDefinitionRepositoryPort:
        repository = self._repository_factory(session)
        if repository is None:
            raise TypeError("Workflow Definition repository factory returned no adapter")
        return repository

    @staticmethod
    def _control_identity(
        *,
        context: PrivateWorkContext,
        project_id: uuid.UUID,
        operation: str,
        idempotency_key: str,
        request: dict[str, object],
        workflow_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
        slot_id: str | None = None,
    ) -> tuple[str, str]:
        try:
            canonical_workflow_control_scope_key(
                project_id=project_id,
                operation=operation,
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
            )
            idempotency_hash = hash_workflow_control_idempotency_key(idempotency_key)
            request_digest = canonical_workflow_control_request_digest_v1(
                operation=operation,
                project_id=project_id,
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
                request=request,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        return idempotency_hash, request_digest

    @staticmethod
    async def _control_replay(
        repository: WorkflowDefinitionRepositoryPort,
        *,
        context: PrivateWorkContext,
        project_id: uuid.UUID,
        operation: str,
        idempotency_hash: str,
        request_digest: str,
        workflow_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
        slot_id: str | None = None,
    ) -> WorkflowControlOperationRecord | None:
        try:
            expected_scope_key = canonical_workflow_control_scope_key(
                project_id=project_id,
                operation=operation,
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
            )
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        try:
            replay = await repository.get_control_operation(
                project_id=project_id,
                operation=operation,
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
            )
        except WorkflowControlIdempotencyConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if replay is not None and type(replay) is not WorkflowControlOperationRecord:
            raise WorkflowUnavailable(context.request_id)
        if replay is not None:
            coordinates_match = (
                replay.project_id == project_id
                and replay.operation == operation
                and replay.scope_key == expected_scope_key
                and replay.idempotency_hash == idempotency_hash
                and replay.request_digest == request_digest
                and (operation == "create" or replay.workflow_id == workflow_id)
                and (operation not in {"version_grant_put", "version_grant_delete"} or replay.result_version_id == version_id)
                and (
                    operation
                    not in {
                        "draft_grant_put",
                        "draft_grant_delete",
                        "version_grant_put",
                        "version_grant_delete",
                    }
                    or replay.result_slot_id == slot_id
                )
            )
            if not coordinates_match:
                raise WorkflowUnavailable(context.request_id)
        return replay

    @staticmethod
    async def _record_control_operation(
        repository: WorkflowDefinitionRepositoryPort,
        *,
        context: PrivateWorkContext,
        command: WorkflowControlOperationCreate,
    ) -> None:
        try:
            receipt = await repository.record_control_operation(command)
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(receipt) is not WorkflowControlOperationRecord:
            raise WorkflowUnavailable(context.request_id)
        expected = WorkflowControlOperationRecord(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            operation=command.operation,
            scope_key=command.scope_key,
            idempotency_hash=command.idempotency_hash,
            request_digest=command.request_digest,
            created_by=command.created_by,
            created_at=receipt.created_at,
            result_version_id=command.result_version_id,
            result_revision=command.result_revision,
            result_checksum=command.result_checksum,
            result_slot_id=command.result_slot_id,
            result_credential_id=command.result_credential_id,
            result_credential_version_id=(command.result_credential_version_id),
            result_status=command.result_status,
            result_deleted=command.result_deleted,
            result_created_at=command.result_created_at,
            result_updated_at=command.result_updated_at,
            result_revoked_at=command.result_revoked_at,
            result_name=command.result_name,
            result_description=command.result_description,
            result_lifecycle=command.result_lifecycle,
            result_published_version_id=(command.result_published_version_id),
            result_published_version_number=(command.result_published_version_number),
            result_draft_revision=command.result_draft_revision,
            result_draft_checksum=command.result_draft_checksum,
            result_missing_slot_ids_csv=(command.result_missing_slot_ids_csv),
        )
        if receipt != expected:
            raise WorkflowUnavailable(context.request_id)

    @staticmethod
    def _require_context(context: PrivateWorkContext) -> PrivateWorkContext:
        if type(context) is not PrivateWorkContext:
            raise TypeError("server-issued PrivateWorkContext is required")
        return context

    @staticmethod
    def _require_workflow_id(workflow_id: uuid.UUID) -> uuid.UUID:
        if type(workflow_id) is not uuid.UUID:
            raise TypeError("Workflow Definition ID must be a UUID")
        return workflow_id

    @staticmethod
    def _require_slot_id(slot_id: str) -> str:
        if type(slot_id) is not str or _SLOT_ID.fullmatch(slot_id) is None:
            raise TypeError("Workflow Credential slot ID is invalid")
        return slot_id

    async def _authorize(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        action: WorkflowAction,
        *,
        lock: bool,
    ) -> ProjectContext:
        current = await self._authorizer.require(
            session,
            context,
            action,
            lock=lock,
        )
        if type(current) is not ProjectContext:
            raise WorkflowUnavailable(context.request_id)
        return current

    async def _record_audit(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        action: AuditAction,
        target_id: uuid.UUID,
    ) -> None:
        try:
            await self._audit.record(
                session,
                context,
                action=action,
                target_id=target_id,
            )
        except Exception:  # ordinary sink failure; CancelledError/BaseException propagate
            raise WorkflowUnavailable(context.request_id) from None

    async def _current_authority(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        for_update: bool,
    ) -> WorkflowDefinitionAuthoritySnapshot:
        try:
            authority = await self._authority_reader.read_current(
                session,
                for_update=for_update,
            )
        except Exception:  # ordinary port failure; CancelledError/BaseException propagate
            raise WorkflowUnavailable(request_id) from None
        if type(authority) is not WorkflowDefinitionAuthoritySnapshot:
            raise WorkflowUnavailable(request_id)
        return authority

    async def _locked_draft(
        self,
        *,
        repository: WorkflowDefinitionRepositoryPort,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        expected_revision: int,
        expected_checksum: str | None,
        request_id: str,
    ) -> WorkflowDraftRecord:
        try:
            definition = await repository.get_definition(
                project_id,
                workflow_id,
                lock=True,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(request_id) from None
        except Exception:
            raise WorkflowUnavailable(request_id) from None
        if definition is None:
            raise WorkflowNotFound(request_id)
        if type(definition) is not WorkflowDefinitionRecord:
            raise WorkflowUnavailable(request_id)
        if definition.status != "active":
            raise WorkflowNotFound(request_id)
        try:
            draft = await repository.get_draft(
                project_id,
                workflow_id,
                lock=True,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(request_id) from None
        except Exception:
            raise WorkflowUnavailable(request_id) from None
        if draft is None:
            raise WorkflowNotFound(request_id)
        if type(draft) is not WorkflowDraftRecord:
            raise WorkflowUnavailable(request_id)
        if draft.revision != expected_revision or (expected_checksum is not None and draft.draft_checksum != expected_checksum):
            raise WorkflowDraftConflict(request_id)
        return draft

    async def _lock_current_draft(
        self,
        *,
        repository: WorkflowDefinitionRepositoryPort,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        request_id: str,
    ) -> WorkflowDraftRecord:
        try:
            definition = await repository.get_definition(
                project_id,
                workflow_id,
                lock=True,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(request_id) from None
        except Exception:
            raise WorkflowUnavailable(request_id) from None
        if definition is None or (type(definition) is WorkflowDefinitionRecord and definition.status != "active"):
            raise WorkflowNotFound(request_id)
        if type(definition) is not WorkflowDefinitionRecord:
            raise WorkflowUnavailable(request_id)
        try:
            draft = await repository.get_draft(
                project_id,
                workflow_id,
                lock=True,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(request_id) from None
        except Exception:
            raise WorkflowUnavailable(request_id) from None
        if draft is None:
            raise WorkflowNotFound(request_id)
        if type(draft) is not WorkflowDraftRecord:
            raise WorkflowUnavailable(request_id)
        return draft

    @staticmethod
    def _draft_response(record: WorkflowDraftRecord) -> WorkflowDraftResponseV1:
        return WorkflowDraftResponseV1(
            workflow_id=record.workflow_id,
            revision=record.revision,
            spec=record.spec,
            canvas=record.canvas,
            draft_checksum=record.draft_checksum,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _definition_response(
        record: WorkflowDefinitionRecord,
        *,
        draft: WorkflowDraftRecord | None = None,
    ) -> WorkflowDefinitionResponseV1:
        draft_revision = draft.revision if draft is not None else record.draft_revision
        draft_checksum = draft.draft_checksum if draft is not None else record.draft_checksum
        if draft_revision is None or draft_checksum is None:
            raise TypeError("Definition projection requires exact Draft coordinates")
        published = record.current_published_version_id is not None
        return WorkflowDefinitionResponseV1(
            id=record.workflow_id,
            name=record.name,
            description=record.description,
            lifecycle=record.status,
            publication="published" if published else "draft_only",
            revision=record.revision,
            current_published_version_id=record.current_published_version_id,
            current_published_version_number=(record.current_published_version_number),
            draft_revision=draft_revision,
            draft_checksum=draft_checksum,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _definition_response_from_receipt(
        receipt: WorkflowControlOperationRecord,
    ) -> WorkflowDefinitionResponseV1:
        """Rebuild the original public projection without mutable reads."""

        if receipt.operation not in {"create", "update", "archive"}:
            raise TypeError("Definition receipt is required")
        if (
            receipt.result_name is None
            or receipt.result_description is None
            or receipt.result_lifecycle is None
            or receipt.result_revision is None
            or receipt.result_draft_revision is None
            or receipt.result_draft_checksum is None
            or receipt.result_created_at is None
            or receipt.result_updated_at is None
        ):
            raise TypeError("Definition receipt is incomplete")
        published = receipt.result_published_version_id is not None
        return WorkflowDefinitionResponseV1(
            id=receipt.workflow_id,
            name=receipt.result_name,
            description=receipt.result_description,
            lifecycle=receipt.result_lifecycle,
            publication="published" if published else "draft_only",
            revision=receipt.result_revision,
            current_published_version_id=(receipt.result_published_version_id),
            current_published_version_number=(receipt.result_published_version_number),
            draft_revision=receipt.result_draft_revision,
            draft_checksum=receipt.result_draft_checksum,
            created_at=receipt.result_created_at,
            updated_at=receipt.result_updated_at,
        )

    @staticmethod
    def _version_slots(
        record: WorkflowVersionRecord,
    ) -> tuple[WorkflowPublishedCredentialSlotV1, ...]:
        return tuple(
            WorkflowPublishedCredentialSlotV1(
                slot_id=slot.slot_id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema=slot.payload_schema,
                payload_schema_checksum=slot.payload_schema_checksum,
                required=slot.required,
            )
            for slot in record.credential_slots
        )

    @classmethod
    def _version_response(
        cls,
        record: WorkflowVersionRecord,
    ) -> WorkflowVersionResponseV1:
        result = _trusted_definition_contract_validate(
            WorkflowVersionResponseV1,
            {
                "id": record.version_id,
                "workflow_id": record.workflow_id,
                "version_number": record.version_number,
                "graph_schema_version": record.graph_schema_version,
                "canvas_schema_version": record.canvas_schema_version,
                "compiler_contract_version": record.compiler_contract_version,
                "semantic_checksum": record.semantic_checksum,
                "spec": record.spec,
                "canvas": record.canvas,
                "credential_slots": cls._version_slots(record),
                "missing_required_credential_slot_ids": (record.missing_required_slot_ids),
                "executable": record.executable,
                "published_at": record.published_at,
            },
        )
        if type(result) is not WorkflowVersionResponseV1:  # pragma: no cover
            raise AssertionError("trusted Version projection returned wrong type")
        return result

    @classmethod
    def _publish_response(
        cls,
        record: WorkflowVersionRecord,
        *,
        request_id: str,
    ) -> WorkflowPublishResponseV1:
        result = _trusted_definition_contract_validate(
            WorkflowPublishResponseV1,
            {
                "request_id": request_id,
                "workflow_id": record.workflow_id,
                "version_id": record.version_id,
                "version_number": record.version_number,
                "graph_schema_version": record.graph_schema_version,
                "canvas_schema_version": record.canvas_schema_version,
                "compiler_contract_version": record.compiler_contract_version,
                "semantic_checksum": record.semantic_checksum,
                "spec": record.spec,
                "canvas": record.canvas,
                "credential_slots": cls._version_slots(record),
                "missing_required_credential_slot_ids": (record.missing_required_slot_ids),
                "executable": record.executable,
                "published_at": record.published_at,
            },
        )
        if type(result) is not WorkflowPublishResponseV1:  # pragma: no cover
            raise AssertionError("trusted Publish projection returned wrong type")
        return result

    @staticmethod
    def _grant_response(
        record: WorkflowCredentialGrantRecord,
    ) -> WorkflowCredentialGrantResponseV1:
        return WorkflowCredentialGrantResponseV1(
            workflow_id=record.workflow_id,
            workflow_version_id=record.workflow_version_id,
            slot_id=record.slot_id,
            payload_schema_checksum=record.payload_schema_checksum,
            credential_id=record.credential_id,
            credential_version_id=record.credential_version_id,
            status=record.status,
            revision=record.revision,
            created_at=record.created_at,
            revoked_at=record.revoked_at,
        )

    async def list_definitions(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        query: str | None = None,
        lifecycle: str = "active",
        publication: str = "all",
        sort: str = "updated_desc",
        cursor: str | None = None,
        limit: int = 50,
    ) -> WorkflowDefinitionPageV1:
        context = self._require_context(context)
        try:
            request = WorkflowDefinitionListQueryV1(
                query=query,
                lifecycle=lifecycle,
                publication=publication,
                sort=sort,
                cursor=cursor,
                limit=limit,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.READ,
            lock=False,
        )
        repository = self._repository(session)
        try:
            page = await repository.list_definitions(
                current.project_id,
                WorkflowDefinitionListQuery(
                    query=request.query,
                    lifecycle=request.lifecycle,
                    publication=request.publication,
                    sort=request.sort,
                    cursor=request.cursor,
                    limit=request.limit,
                ),
            )
        except ValueError:
            raise WorkflowInputInvalid(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(page) is not WorkflowDefinitionPage:
            raise WorkflowUnavailable(context.request_id)
        try:
            items = tuple(self._definition_response(item) for item in page.items)
            result = _trusted_definition_contract_validate(
                WorkflowDefinitionPageV1,
                {"items": items, "next_cursor": page.next_cursor},
            )
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        if type(result) is not WorkflowDefinitionPageV1:  # pragma: no cover
            raise WorkflowUnavailable(context.request_id)
        return result

    async def create_definition(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        name: str,
        description: str,
        idempotency_key: str,
    ) -> WorkflowDefinitionResponseV1:
        context = self._require_context(context)
        try:
            request = WorkflowDefinitionCreateRequestV1(
                name=name,
                description=description,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        spec = {"schema_version": 1}
        canvas = {"schema_version": 1}
        command = WorkflowDefinitionCreate(
            name=request.name,
            description=request.description,
            spec_schema_version=1,
            canvas_schema_version=1,
            spec=spec,
            canvas=canvas,
            draft_checksum=canonical_workflow_draft_checksum_v1(
                spec=spec,
                canvas=canvas,
            ),
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="create",
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="create",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
        )
        if replay is not None:
            try:
                return self._definition_response_from_receipt(replay)
            except (TypeError, ValueError):
                raise WorkflowUnavailable(context.request_id) from None
        try:
            record, draft = await repository.create_definition(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                command=command,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowDefinitionConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowDefinitionRecord or type(draft) is not WorkflowDraftRecord or record.project_id != current.project_id or draft.project_id != current.project_id or draft.workflow_id != record.workflow_id:
            raise WorkflowUnavailable(context.request_id)
        try:
            response = self._definition_response(record, draft=draft)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_DEFINITION_CREATED,
            target_id=record.workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=record.workflow_id,
                operation="create",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_revision=record.revision,
                result_created_at=record.created_at,
                result_updated_at=record.updated_at,
                result_name=record.name,
                result_description=record.description,
                result_lifecycle=record.status,
                result_published_version_id=(record.current_published_version_id),
                result_published_version_number=(record.current_published_version_number),
                result_draft_revision=draft.revision,
                result_draft_checksum=draft.draft_checksum,
            ),
        )
        return response

    async def get_definition(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
    ) -> WorkflowDefinitionResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        current = await self._authorize(
            session,
            context,
            WorkflowAction.READ,
            lock=False,
        )
        repository = self._repository(session)
        try:
            record = await repository.get_definition(
                current.project_id,
                workflow_id,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if record is None:
            raise WorkflowNotFound(context.request_id)
        if type(record) is not WorkflowDefinitionRecord or record.project_id != current.project_id or record.workflow_id != workflow_id:
            raise WorkflowUnavailable(context.request_id)
        try:
            return self._definition_response(record)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None

    async def update_definition(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        *,
        expected_revision: int,
        name: str | None,
        description: str | None,
        idempotency_key: str,
    ) -> WorkflowDefinitionResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        try:
            request = WorkflowDefinitionUpdateRequestV1(
                expected_revision=expected_revision,
                name=name,
                description=description,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="update",
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="update",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
        )
        if replay is not None:
            try:
                return self._definition_response_from_receipt(replay)
            except (TypeError, ValueError):
                raise WorkflowUnavailable(context.request_id) from None
        try:
            record = await repository.update_definition(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                command=WorkflowDefinitionUpdate(
                    expected_revision=request.expected_revision,
                    name=request.name,
                    description=request.description,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowDefinitionConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowDefinitionRecord or record.project_id != current.project_id or record.workflow_id != workflow_id:
            raise WorkflowUnavailable(context.request_id)
        try:
            response = self._definition_response(record)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_DEFINITION_UPDATED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="update",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_revision=record.revision,
                result_created_at=record.created_at,
                result_updated_at=record.updated_at,
                result_name=record.name,
                result_description=record.description,
                result_lifecycle=record.status,
                result_published_version_id=(record.current_published_version_id),
                result_published_version_number=(record.current_published_version_number),
                result_draft_revision=record.draft_revision,
                result_draft_checksum=record.draft_checksum,
            ),
        )
        return response

    async def archive_definition(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> WorkflowDefinitionResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        try:
            request = WorkflowDefinitionArchiveRequestV1(
                expected_revision=expected_revision,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="archive",
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="archive",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
        )
        if replay is not None:
            try:
                return self._definition_response_from_receipt(replay)
            except (TypeError, ValueError):
                raise WorkflowUnavailable(context.request_id) from None
        try:
            record = await repository.archive_definition(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                command=WorkflowDefinitionArchive(
                    expected_revision=request.expected_revision,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowDefinitionConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowDefinitionRecord or record.project_id != current.project_id or record.workflow_id != workflow_id:
            raise WorkflowUnavailable(context.request_id)
        try:
            response = self._definition_response(record)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_DEFINITION_ARCHIVED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="archive",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_revision=record.revision,
                result_created_at=record.created_at,
                result_updated_at=record.updated_at,
                result_name=record.name,
                result_description=record.description,
                result_lifecycle=record.status,
                result_published_version_id=(record.current_published_version_id),
                result_published_version_number=(record.current_published_version_number),
                result_draft_revision=record.draft_revision,
                result_draft_checksum=record.draft_checksum,
            ),
        )
        return response

    async def get_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
    ) -> WorkflowDraftResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        current = await self._authorize(
            session,
            context,
            WorkflowAction.READ,
            lock=False,
        )
        repository = self._repository(session)
        try:
            record = await repository.get_draft(
                current.project_id,
                workflow_id,
                lock=False,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if record is None:
            raise WorkflowNotFound(context.request_id)
        if type(record) is not WorkflowDraftRecord or record.project_id != current.project_id or record.workflow_id != workflow_id:
            raise WorkflowUnavailable(context.request_id)
        try:
            return self._draft_response(record)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None

    async def save_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowDraftSaveRequestV1,
        *,
        idempotency_key: str,
    ) -> WorkflowDraftResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(request) is not WorkflowDraftSaveRequestV1:
            raise TypeError("exact WorkflowDraftSaveRequestV1 is required")
        request = WorkflowDraftSaveRequestV1.model_validate(request.model_dump(mode="json", by_alias=True, exclude_unset=True))
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        spec = workflow_draft_spec_public_projection_v1(request.spec)
        canvas = workflow_draft_canvas_public_projection_v1(request.canvas)
        checksum = canonical_workflow_draft_checksum_v1(
            spec=spec,
            canvas=canvas,
        )
        credential_slot_ids = tuple(sorted({slot.id for slot in (request.spec.credential_slots or ()) if slot.id is not None}))
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="save_draft",
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="save_draft",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
        )
        if replay is not None:
            try:
                replay_draft = await repository.get_draft(
                    current.project_id,
                    workflow_id,
                    lock=True,
                )
            except Exception:
                raise WorkflowUnavailable(context.request_id) from None
            if type(replay_draft) is not WorkflowDraftRecord or replay_draft.project_id != current.project_id or replay_draft.workflow_id != workflow_id:
                raise WorkflowNotFound(context.request_id)
            if replay_draft.revision != replay.result_revision or replay_draft.draft_checksum != replay.result_draft_checksum:
                raise WorkflowDraftConflict(context.request_id)
            try:
                return self._draft_response(replay_draft)
            except (TypeError, ValueError):
                raise WorkflowUnavailable(context.request_id) from None
        try:
            record = await repository.save_draft(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                command=WorkflowDraftUpdate(
                    expected_revision=request.expected_revision,
                    spec_schema_version=request.spec.schema_version,
                    canvas_schema_version=request.canvas.schema_version,
                    spec=spec,
                    canvas=canvas,
                    draft_checksum=checksum,
                    credential_slot_ids=credential_slot_ids,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowDraftCASConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowDraftRecord or record.project_id != current.project_id or record.workflow_id != workflow_id:
            raise WorkflowUnavailable(context.request_id)
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_DRAFT_SAVED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="save_draft",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_revision=record.revision,
                result_draft_checksum=record.draft_checksum,
                result_updated_at=record.updated_at,
            ),
        )
        return self._draft_response(record)

    @staticmethod
    def _parse_complete_draft(
        draft: WorkflowDraftRecord,
    ) -> tuple[WorkflowSpecV1 | None, CanvasDocumentV1 | None, list[WorkflowValidationIssueV1]]:
        issues: list[WorkflowValidationIssueV1] = []
        try:
            checksum = canonical_workflow_draft_checksum_v1(
                spec=draft.spec,
                canvas=draft.canvas,
            )
        except (TypeError, ValueError):
            checksum = None
        if checksum != draft.draft_checksum:
            issues.append(_safe_issue("WORKFLOW_DRAFT_CHECKSUM_INVALID", phase="transport"))

        raw_nodes = draft.spec.get("nodes")
        known = {
            "start",
            "llm",
            "condition",
            "transform",
            "variable_aggregate",
            "loop",
            "http_request",
            "python_code",
            "end",
        }
        if isinstance(raw_nodes, list):
            for raw in raw_nodes:
                if not isinstance(raw, dict):
                    continue
                raw_type = raw.get("type")
                raw_id = raw.get("id")
                safe_id = raw_id if isinstance(raw_id, str) else None
                if raw_type not in known:
                    issues.append(
                        _safe_issue(
                            "WORKFLOW_NODE_TYPE_UNAVAILABLE",
                            phase="transport",
                            node_id=(safe_id if safe_id is not None and len(safe_id) == 36 else None),
                        )
                    )
                elif raw.get("type_version") != 1:
                    issues.append(
                        _safe_issue(
                            "WORKFLOW_NODE_VERSION_UNAVAILABLE",
                            phase="transport",
                            node_id=(safe_id if safe_id is not None and len(safe_id) == 36 else None),
                        )
                    )
        spec: WorkflowSpecV1 | None = None
        canvas: CanvasDocumentV1 | None = None
        try:
            spec = WorkflowSpecV1.model_validate(draft.spec)
        except Exception:
            if not any(issue.code.startswith("WORKFLOW_NODE_") for issue in issues):
                issues.append(
                    _safe_issue(
                        "WORKFLOW_DRAFT_TRANSPORT_INCOMPLETE",
                        phase="transport",
                    )
                )
        try:
            canvas = CanvasDocumentV1.model_validate(draft.canvas)
        except Exception:
            issues.append(
                _safe_issue(
                    "WORKFLOW_CANVAS_TRANSPORT_INCOMPLETE",
                    phase="transport",
                )
            )
        return spec, canvas, issues

    @staticmethod
    def _catalog_issues(
        *,
        spec: WorkflowSpecV1,
        current: ProjectContext,
        authority: WorkflowDefinitionAuthoritySnapshot,
    ) -> tuple[str, list[WorkflowValidationIssueV1]]:
        capabilities = WorkflowCatalogCapabilityProjectionV1(
            code_use=Capability.WORKFLOW_CODE_USE in current.capabilities,
            http_use=Capability.WORKFLOW_HTTP_USE in current.capabilities,
        )
        catalog = build_project_node_catalog_v1(
            locked=authority.locked_policy,
            capabilities=capabilities,
            facets=authority.facets,
        )
        by_type = {entry.definition.type: entry for entry in catalog.entries}
        issues: list[WorkflowValidationIssueV1] = []
        for node in spec.nodes:
            entry = by_type.get(node.type)
            if entry is None:
                issues.append(
                    _safe_issue(
                        "WORKFLOW_NODE_TYPE_UNAVAILABLE",
                        node_id=node.id,
                    )
                )
            elif entry.availability.state == "disabled":
                assert entry.availability.reason_code is not None
                issues.append(
                    _safe_issue(
                        entry.availability.reason_code,
                        node_id=node.id,
                    )
                )
        return catalog.catalog_generation, issues

    @classmethod
    def _validate_locked_record(
        cls,
        *,
        draft: WorkflowDraftRecord,
        current: ProjectContext,
        authority: WorkflowDefinitionAuthoritySnapshot,
    ) -> tuple[
        WorkflowDefinitionValidationArtifact | None,
        tuple[WorkflowValidationIssueV1, ...],
    ]:
        spec, canvas, issues = cls._parse_complete_draft(draft)
        if spec is None or canvas is None or issues:
            return None, _sorted_issues(issues)
        catalog_generation, catalog_issues = cls._catalog_issues(
            spec=spec,
            current=current,
            authority=authority,
        )
        issues.extend(catalog_issues)
        if issues:
            return None, _sorted_issues(issues)

        limits = workflow_compilation_limits_from_graph_policy(authority.locked_policy.value.graph_limits)
        try:
            ir = compile_workflow(
                spec,
                graph_schema_version=GRAPH_SCHEMA_VERSION_V1,
                compiler_contract_version=CURRENT_COMPILER_CONTRACT_VERSION,
                limits=limits,
                cache=None,
            )
        except WorkflowValidationError as error:
            issues.extend(_compiler_issue(issue) for issue in error.issues)
            return None, _sorted_issues(issues)
        except WorkflowCompilerUnavailableError:
            issues.append(_safe_issue("WORKFLOW_COMPILER_UNAVAILABLE", phase="compiler"))
            return None, _sorted_issues(issues)

        canvas_validation = validate_canvas_document(spec, canvas)
        issues.extend(_compiler_issue(issue) for issue in canvas_validation.issues)
        try:
            requirements = derive_workflow_published_requirements_v1(
                spec=spec,
                policy=authority.locked_policy.value,
            )
        except WorkflowDefinitionDependencyError as error:
            issues.append(
                _safe_issue(
                    error.code,
                    node_id=error.node_id,
                    port_id=error.port_id,
                )
            )
            return None, _sorted_issues(issues)
        if issues:
            return None, _sorted_issues(issues)
        return (
            WorkflowDefinitionValidationArtifact(
                spec=spec,
                canvas=canvas,
                ir=ir,
                requirements=requirements,
                catalog_generation=catalog_generation,
                policy_revision=authority.locked_policy.revision,
            ),
            (),
        )

    async def validate_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowDraftValidateRequestV1,
    ) -> WorkflowDraftValidationResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(request) is not WorkflowDraftValidateRequestV1:
            raise TypeError("exact WorkflowDraftValidateRequestV1 is required")
        request = WorkflowDraftValidateRequestV1.model_validate(request.model_dump(mode="json"))
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        repository = self._repository(session)
        draft = await self._locked_draft(
            repository=repository,
            project_id=current.project_id,
            workflow_id=workflow_id,
            expected_revision=request.expected_revision,
            expected_checksum=request.expected_draft_checksum,
            request_id=context.request_id,
        )
        authority = await self._current_authority(
            session,
            request_id=context.request_id,
            for_update=False,
        )
        artifact, issues = self._validate_locked_record(
            draft=draft,
            current=current,
            authority=authority,
        )
        result = _trusted_definition_contract_validate(
            WorkflowDraftValidationResponseV1,
            {
                "request_id": context.request_id,
                "workflow_id": workflow_id,
                "draft_revision": draft.revision,
                "draft_checksum": draft.draft_checksum,
                "valid": artifact is not None,
                "issues": issues,
                "semantic_checksum": (None if artifact is None else artifact.ir.semantic_checksum),
                "requirements": None if artifact is None else artifact.requirements,
                "catalog_generation": (None if artifact is None else artifact.catalog_generation),
                "policy_revision": (None if artifact is None else artifact.policy_revision),
            },
        )
        if type(result) is not WorkflowDraftValidationResponseV1:  # pragma: no cover
            raise WorkflowUnavailable(context.request_id)
        return result

    async def _require_specialized_actions(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        draft: WorkflowDraftRecord,
    ) -> None:
        raw_nodes = draft.spec.get("nodes")
        if not isinstance(raw_nodes, list):
            return
        node_types = {raw.get("type") for raw in raw_nodes if isinstance(raw, dict) and isinstance(raw.get("type"), str)}
        if "python_code" in node_types:
            await self._authorize(
                session,
                context,
                WorkflowAction.CODE_USE,
                lock=True,
            )
        if "http_request" in node_types:
            await self._authorize(
                session,
                context,
                WorkflowAction.HTTP_USE,
                lock=True,
            )
            write_methods = {"POST", "PUT", "PATCH", "DELETE"}
            if any(isinstance(raw, dict) and raw.get("type") == "http_request" and isinstance(raw.get("config"), dict) and raw["config"].get("method") in write_methods for raw in raw_nodes):
                await self._authorize(
                    session,
                    context,
                    WorkflowAction.HTTP_WRITE,
                    lock=True,
                )

    @staticmethod
    def _persistence_dependencies(
        requirements: WorkflowPublishedRequirementsV1,
    ) -> tuple[
        tuple[WorkflowModelRefCreate, ...],
        tuple[WorkflowCredentialSlotCreate, ...],
        tuple[WorkflowCodeRequirementCreate, ...],
        tuple[WorkflowHttpRequirementCreate, ...],
    ]:
        model_refs = tuple(
            WorkflowModelRefCreate(
                node_id=item.node_id,
                purpose=item.purpose,
                logical_model_name=item.logical_model_name,
            )
            for item in requirements.model_refs
        )
        slots = tuple(
            WorkflowCredentialSlotCreate(
                slot_id=item.slot_id,
                name=item.name,
                purpose=item.purpose,
                payload_schema=item.model_dump(mode="json")["payload_schema"],
                payload_schema_checksum=item.payload_schema_checksum,
            )
            for item in requirements.credential_slots
        )
        code_requirements = tuple(
            WorkflowCodeRequirementCreate(
                node_id=item.node_id,
                runtime_contract=item.runtime_contract,
            )
            for item in requirements.code
        )
        http_requirements = tuple(
            WorkflowHttpRequirementCreate(
                node_id=item.node_id,
                method=item.method,
                endpoint_policy_id=item.endpoint_policy_id,
                injection_profile_id=item.injection_profile_id,
                credential_slot_id=item.credential_slot_id,
            )
            for item in requirements.http
        )
        return model_refs, slots, code_requirements, http_requirements

    async def publish(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        request: WorkflowPublishRequestV1,
        *,
        idempotency_key: str,
    ) -> WorkflowPublishResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(request) is not WorkflowPublishRequestV1:
            raise TypeError("exact WorkflowPublishRequestV1 is required")
        request = WorkflowPublishRequestV1.model_validate(request.model_dump(mode="json"))
        try:
            idempotency_hash = hash_workflow_publish_idempotency_key(idempotency_key)
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.PUBLISH,
            lock=True,
        )
        request_digest = canonical_workflow_publish_request_digest_v1(
            expected_revision=request.expected_revision,
            expected_draft_checksum=request.expected_draft_checksum,
        )
        repository = self._repository(session)
        try:
            replay = await repository.get_publish_replay(
                current.project_id,
                workflow_id,
                idempotency_hash,
                request_digest,
            )
        except WorkflowPublishIdempotencyConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if replay is not None:
            if type(replay) is not WorkflowVersionRecord:
                raise WorkflowUnavailable(context.request_id)
            try:
                return self._publish_response(
                    replay,
                    request_id=context.request_id,
                )
            except (TypeError, ValueError):
                raise WorkflowUnavailable(context.request_id) from None

        draft = await self._locked_draft(
            repository=repository,
            project_id=current.project_id,
            workflow_id=workflow_id,
            expected_revision=request.expected_revision,
            expected_checksum=request.expected_draft_checksum,
            request_id=context.request_id,
        )
        await self._require_specialized_actions(session, context, draft)
        # The specialized checks may have revalidated membership.  Use the
        # server-issued current ProjectContext from the primary Publish check
        # as the sole capability projection for exact Catalog validation.
        authority = await self._current_authority(
            session,
            request_id=context.request_id,
            for_update=True,
        )
        artifact, _issues = self._validate_locked_record(
            draft=draft,
            current=current,
            authority=authority,
        )
        if artifact is None:
            raise WorkflowDraftInvalid(context.request_id)

        model_refs, slots, code_requirements, http_requirements = self._persistence_dependencies(artifact.requirements)
        try:
            result = await repository.publish_version(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                command=WorkflowVersionPublish(
                    expected_draft_revision=draft.revision,
                    expected_draft_checksum=draft.draft_checksum,
                    graph_schema_version=GRAPH_SCHEMA_VERSION_V1,
                    canvas_schema_version=draft.canvas_schema_version,
                    compiler_contract_version=CURRENT_COMPILER_CONTRACT_VERSION,
                    semantic_checksum=artifact.ir.semantic_checksum,
                    model_refs=model_refs,
                    credential_slots=slots,
                    code_requirements=code_requirements,
                    http_requirements=http_requirements,
                    idempotency_hash=idempotency_hash,
                    request_digest=request_digest,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except (
            WorkflowDraftCASConflict,
            WorkflowDefinitionConflict,
            WorkflowPublishIdempotencyConflict,
        ):
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id)
        if type(result) is not WorkflowVersionPublishResult:
            raise WorkflowUnavailable(context.request_id)
        version = result.record
        if type(version) is not WorkflowVersionRecord:
            raise WorkflowUnavailable(context.request_id)
        try:
            response = self._publish_response(
                version,
                request_id=context.request_id,
            )
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        if result.created:
            await self._record_audit(
                session,
                context,
                action=AuditAction.WORKFLOW_VERSION_PUBLISHED,
                target_id=workflow_id,
            )
        return response

    async def publish_draft(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        *,
        expected_revision: int,
        expected_draft_checksum: str,
        idempotency_key: str,
    ) -> WorkflowPublishResponseV1:
        try:
            request = WorkflowPublishRequestV1(
                expected_revision=expected_revision,
                expected_draft_checksum=expected_draft_checksum,
            )
        except (TypeError, ValueError):
            safe_context = self._require_context(context)
            raise WorkflowInputInvalid(safe_context.request_id) from None
        return await self.publish(
            session,
            context,
            workflow_id,
            request,
            idempotency_key=idempotency_key,
        )

    async def list_versions(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> WorkflowVersionPageV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        try:
            request = WorkflowVersionListQueryV1(
                cursor=cursor,
                limit=limit,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.READ,
            lock=False,
        )
        repository = self._repository(session)
        try:
            page = await repository.list_version_history(
                current.project_id,
                workflow_id,
                cursor=request.cursor,
                limit=request.limit,
            )
        except ValueError:
            raise WorkflowInputInvalid(context.request_id) from None
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(page) is not WorkflowVersionPage:
            raise WorkflowUnavailable(context.request_id)
        try:
            items = tuple(self._version_response(record) for record in page.items)
            result = _trusted_definition_contract_validate(
                WorkflowVersionPageV1,
                {"items": items, "next_cursor": page.next_cursor},
            )
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None
        if type(result) is not WorkflowVersionPageV1:  # pragma: no cover
            raise WorkflowUnavailable(context.request_id)
        return result

    async def get_version(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> WorkflowVersionResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(version_id) is not uuid.UUID:
            raise TypeError("Workflow Version ID must be a UUID")
        current = await self._authorize(
            session,
            context,
            WorkflowAction.READ,
            lock=False,
        )
        repository = self._repository(session)
        try:
            record = await repository.get_version(
                current.project_id,
                workflow_id,
                version_id,
                lock=False,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if record is None:
            raise WorkflowNotFound(context.request_id)
        if type(record) is not WorkflowVersionRecord:
            raise WorkflowUnavailable(context.request_id)
        try:
            return self._version_response(record)
        except (TypeError, ValueError):
            raise WorkflowUnavailable(context.request_id) from None

    @staticmethod
    def _draft_slot_checksum(
        draft: WorkflowDraftRecord,
        *,
        slot_id: str,
        request_id: str,
    ) -> str:
        try:
            spec = WorkflowDraftSpecV1.model_validate(draft.spec)
        except Exception:
            raise WorkflowDraftInvalid(request_id) from None
        slots = tuple(slot for slot in (spec.credential_slots or ()) if slot.id == slot_id)
        if not slots:
            raise WorkflowNotFound(request_id)
        if len(slots) != 1:
            raise WorkflowDraftInvalid(request_id)
        try:
            slot = WorkflowCredentialSlotDecl.model_validate(slots[0].model_dump(mode="json", exclude_unset=True))
            return canonical_workflow_slot_schema_checksum_v1(slot.payload_schema)
        except (TypeError, ValueError):
            raise WorkflowDraftInvalid(request_id) from None

    async def put_draft_grant_intent(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        slot_id: str,
        *,
        credential_id: uuid.UUID,
        expected_credential_version_id: uuid.UUID,
        expected_slot_schema_checksum: str,
        idempotency_key: str,
    ) -> WorkflowDraftGrantIntentResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        try:
            slot_id = self._require_slot_id(slot_id)
            request = WorkflowCredentialGrantMutationRequestV1(
                credential_id=credential_id,
                expected_credential_version_id=expected_credential_version_id,
                expected_slot_schema_checksum=expected_slot_schema_checksum,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        await self._authorize(
            session,
            context,
            WorkflowAction.CREDENTIAL_GRANT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="draft_grant_put",
            workflow_id=workflow_id,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="draft_grant_put",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
            slot_id=slot_id,
        )
        if replay is not None:
            if replay.result_slot_id is None or replay.result_checksum is None or replay.result_credential_id is None or replay.result_credential_version_id is None or replay.result_updated_at is None:
                raise WorkflowUnavailable(context.request_id)
            return WorkflowDraftGrantIntentResponseV1(
                workflow_id=replay.workflow_id,
                slot_id=replay.result_slot_id,
                slot_schema_checksum=replay.result_checksum,
                credential_id=replay.result_credential_id,
                expected_credential_version_id=replay.result_credential_version_id,
                updated_at=replay.result_updated_at,
            )
        draft = await self._lock_current_draft(
            repository=repository,
            project_id=current.project_id,
            workflow_id=workflow_id,
            request_id=context.request_id,
        )
        resolved_checksum = self._draft_slot_checksum(
            draft,
            slot_id=slot_id,
            request_id=context.request_id,
        )
        if request.expected_slot_schema_checksum != resolved_checksum:
            raise WorkflowDraftConflict(context.request_id)
        try:
            record = await repository.put_draft_grant_intent(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                slot_id=slot_id,
                resolved_draft_revision=draft.revision,
                command=WorkflowCredentialGrantPut(
                    credential_id=request.credential_id,
                    expected_credential_version_id=(request.expected_credential_version_id),
                    expected_slot_schema_checksum=(request.expected_slot_schema_checksum),
                    resolved_slot_schema_checksum=resolved_checksum,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except (WorkflowDraftCASConflict, WorkflowCredentialGrantConflict):
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowDraftCredentialGrantIntentRecord or record.project_id != current.project_id or record.workflow_id != workflow_id or record.slot_id != slot_id:
            raise WorkflowUnavailable(context.request_id)
        response = WorkflowDraftGrantIntentResponseV1(
            workflow_id=record.workflow_id,
            slot_id=record.slot_id,
            slot_schema_checksum=record.slot_schema_checksum,
            credential_id=record.credential_id,
            expected_credential_version_id=record.expected_credential_version_id,
            updated_at=record.updated_at,
        )
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_UPDATED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="draft_grant_put",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_checksum=record.slot_schema_checksum,
                result_slot_id=record.slot_id,
                result_credential_id=record.credential_id,
                result_credential_version_id=record.expected_credential_version_id,
                result_updated_at=record.updated_at,
            ),
        )
        return response

    async def delete_draft_grant_intent(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        slot_id: str,
        *,
        idempotency_key: str,
    ) -> WorkflowDraftGrantIntentDeleteResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        try:
            slot_id = self._require_slot_id(slot_id)
        except TypeError:
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.EDIT,
            lock=True,
        )
        await self._authorize(
            session,
            context,
            WorkflowAction.CREDENTIAL_GRANT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="draft_grant_delete",
            workflow_id=workflow_id,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
            request={},
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="draft_grant_delete",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
            slot_id=slot_id,
        )
        if replay is not None:
            if replay.result_slot_id != slot_id or replay.result_deleted is not True:
                raise WorkflowUnavailable(context.request_id)
            return WorkflowDraftGrantIntentDeleteResponseV1(
                workflow_id=replay.workflow_id,
                slot_id=replay.result_slot_id,
                deleted=True,
            )
        draft = await self._lock_current_draft(
            repository=repository,
            project_id=current.project_id,
            workflow_id=workflow_id,
            request_id=context.request_id,
        )
        try:
            record = await repository.delete_draft_grant_intent(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                slot_id=slot_id,
                resolved_draft_revision=draft.revision,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except (WorkflowDraftCASConflict, WorkflowCredentialGrantConflict):
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if record is not None and (type(record) is not WorkflowDraftCredentialGrantIntentRecord or record.project_id != current.project_id or record.workflow_id != workflow_id or record.slot_id != slot_id):
            raise WorkflowUnavailable(context.request_id)
        if record is not None:
            await self._record_audit(
                session,
                context,
                action=AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_DELETED,
                target_id=workflow_id,
            )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="draft_grant_delete",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_slot_id=slot_id,
                result_deleted=True,
            ),
        )
        return WorkflowDraftGrantIntentDeleteResponseV1(
            workflow_id=workflow_id,
            slot_id=slot_id,
            deleted=True,
        )

    async def put_version_grant(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
        *,
        credential_id: uuid.UUID,
        expected_credential_version_id: uuid.UUID,
        expected_slot_schema_checksum: str,
        idempotency_key: str,
    ) -> WorkflowCredentialGrantResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(version_id) is not uuid.UUID:
            raise TypeError("Workflow Version ID must be a UUID")
        try:
            slot_id = self._require_slot_id(slot_id)
            request = WorkflowCredentialGrantMutationRequestV1(
                credential_id=credential_id,
                expected_credential_version_id=expected_credential_version_id,
                expected_slot_schema_checksum=expected_slot_schema_checksum,
            )
        except (TypeError, ValueError):
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.PUBLISH,
            lock=True,
        )
        await self._authorize(
            session,
            context,
            WorkflowAction.CREDENTIAL_GRANT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="version_grant_put",
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
            request=request.model_dump(mode="json"),
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="version_grant_put",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
        )
        if replay is not None:
            if (
                replay.result_version_id is None
                or replay.result_slot_id is None
                or replay.result_checksum is None
                or replay.result_credential_id is None
                or replay.result_credential_version_id is None
                or replay.result_status is None
                or replay.result_revision is None
                or replay.result_created_at is None
            ):
                raise WorkflowUnavailable(context.request_id)
            return WorkflowCredentialGrantResponseV1(
                workflow_id=replay.workflow_id,
                workflow_version_id=replay.result_version_id,
                slot_id=replay.result_slot_id,
                payload_schema_checksum=replay.result_checksum,
                credential_id=replay.result_credential_id,
                credential_version_id=replay.result_credential_version_id,
                status=replay.result_status,
                revision=replay.result_revision,
                created_at=replay.result_created_at,
                revoked_at=replay.result_revoked_at,
            )
        try:
            version = await repository.get_version(
                current.project_id,
                workflow_id,
                version_id,
                lock=False,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if version is None:
            raise WorkflowNotFound(context.request_id)
        if type(version) is not WorkflowVersionRecord:
            raise WorkflowUnavailable(context.request_id)
        slots = tuple(slot for slot in version.credential_slots if slot.slot_id == slot_id)
        if len(slots) != 1:
            raise WorkflowNotFound(context.request_id)
        resolved_checksum = slots[0].payload_schema_checksum
        if request.expected_slot_schema_checksum != resolved_checksum:
            raise WorkflowDraftConflict(context.request_id)
        try:
            record = await repository.put_version_grant(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
                command=WorkflowCredentialGrantPut(
                    credential_id=request.credential_id,
                    expected_credential_version_id=(request.expected_credential_version_id),
                    expected_slot_schema_checksum=(request.expected_slot_schema_checksum),
                    resolved_slot_schema_checksum=resolved_checksum,
                ),
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowCredentialGrantConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if type(record) is not WorkflowCredentialGrantRecord or record.project_id != current.project_id or record.workflow_id != workflow_id or record.workflow_version_id != version_id or record.slot_id != slot_id:
            raise WorkflowUnavailable(context.request_id)
        response = self._grant_response(record)
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_VERSION_GRANT_UPDATED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="version_grant_put",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_version_id=record.workflow_version_id,
                result_revision=record.revision,
                result_checksum=record.payload_schema_checksum,
                result_slot_id=record.slot_id,
                result_credential_id=record.credential_id,
                result_credential_version_id=record.credential_version_id,
                result_status=record.status,
                result_created_at=record.created_at,
                result_revoked_at=record.revoked_at,
            ),
        )
        return response

    async def revoke_version_grant(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
        *,
        idempotency_key: str,
    ) -> WorkflowCredentialGrantResponseV1:
        context = self._require_context(context)
        workflow_id = self._require_workflow_id(workflow_id)
        if type(version_id) is not uuid.UUID:
            raise TypeError("Workflow Version ID must be a UUID")
        try:
            slot_id = self._require_slot_id(slot_id)
        except TypeError:
            raise WorkflowInputInvalid(context.request_id) from None
        current = await self._authorize(
            session,
            context,
            WorkflowAction.PUBLISH,
            lock=True,
        )
        await self._authorize(
            session,
            context,
            WorkflowAction.CREDENTIAL_GRANT,
            lock=True,
        )
        repository = self._repository(session)
        idempotency_hash, request_digest = self._control_identity(
            context=context,
            project_id=current.project_id,
            operation="version_grant_delete",
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
            request={},
        )
        replay = await self._control_replay(
            repository,
            context=context,
            project_id=current.project_id,
            operation="version_grant_delete",
            idempotency_hash=idempotency_hash,
            request_digest=request_digest,
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
        )
        if replay is not None:
            if (
                replay.result_version_id is None
                or replay.result_slot_id is None
                or replay.result_checksum is None
                or replay.result_credential_id is None
                or replay.result_credential_version_id is None
                or replay.result_status is None
                or replay.result_revision is None
                or replay.result_created_at is None
            ):
                raise WorkflowUnavailable(context.request_id)
            return WorkflowCredentialGrantResponseV1(
                workflow_id=replay.workflow_id,
                workflow_version_id=replay.result_version_id,
                slot_id=replay.result_slot_id,
                payload_schema_checksum=replay.result_checksum,
                credential_id=replay.result_credential_id,
                credential_version_id=replay.result_credential_version_id,
                status=replay.result_status,
                revision=replay.result_revision,
                created_at=replay.result_created_at,
                revoked_at=replay.result_revoked_at,
            )
        try:
            record = await repository.revoke_version_grant(
                project_id=current.project_id,
                actor_user_id=str(current.user_id),
                workflow_id=workflow_id,
                version_id=version_id,
                slot_id=slot_id,
            )
        except WorkflowAuthorityMissing:
            raise WorkflowNotFound(context.request_id) from None
        except WorkflowCredentialGrantConflict:
            raise WorkflowDraftConflict(context.request_id) from None
        except Exception:
            raise WorkflowUnavailable(context.request_id) from None
        if record is None:
            raise WorkflowNotFound(context.request_id)
        if type(record) is not WorkflowCredentialGrantRecord or record.project_id != current.project_id or record.workflow_id != workflow_id or record.workflow_version_id != version_id or record.slot_id != slot_id:
            raise WorkflowUnavailable(context.request_id)
        response = self._grant_response(record)
        await self._record_audit(
            session,
            context,
            action=AuditAction.WORKFLOW_VERSION_GRANT_REVOKED,
            target_id=workflow_id,
        )
        await self._record_control_operation(
            repository,
            context=context,
            command=WorkflowControlOperationCreate(
                project_id=current.project_id,
                workflow_id=workflow_id,
                operation="version_grant_delete",
                idempotency_hash=idempotency_hash,
                request_digest=request_digest,
                created_by=str(current.user_id),
                result_version_id=record.workflow_version_id,
                result_revision=record.revision,
                result_checksum=record.payload_schema_checksum,
                result_slot_id=record.slot_id,
                result_credential_id=record.credential_id,
                result_credential_version_id=record.credential_version_id,
                result_status=record.status,
                result_created_at=record.created_at,
                result_revoked_at=record.revoked_at,
            ),
        )
        return response


__all__ = [
    "PostgresWorkflowDefinitionAuthorityReader",
    "WorkflowDefinitionAuditPort",
    "WorkflowDefinitionAuthorityReaderPort",
    "WorkflowDefinitionAuthorizationPort",
    "WorkflowDefinitionControlService",
    "WorkflowDefinitionRepositoryFactoryPort",
    "WorkflowDefinitionRepositoryPort",
    "workflow_definition_repository_factory",
]
