"""Session-bound persistence for standalone Workflow aggregates.

Callers own transaction commit and rollback.  Mutations acquire locks in the
fixed Project -> Membership -> Workflow resource order.  This module never
touches Agent Runs, Threads, Agent events, or LangGraph checkpoints.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets.credential_model import (
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.workflows.model import (
    WorkflowCodeSandboxLeaseRow,
    WorkflowControlOperationRow,
    WorkflowCredentialGrantRow,
    WorkflowDefinitionRow,
    WorkflowDraftCredentialGrantIntentRow,
    WorkflowDraftRow,
    WorkflowNodeEffectRow,
    WorkflowRunCodeSnapshotRow,
    WorkflowRunEventInvariantRow,
    WorkflowRunEventRow,
    WorkflowRunHttpSnapshotRow,
    WorkflowRunJobRow,
    WorkflowRunModelSnapshotRow,
    WorkflowRunRow,
    WorkflowRunRuntimePolicySnapshotRow,
    WorkflowRunSnapshotRow,
    WorkflowVersionCodeRequirementRow,
    WorkflowVersionCredentialSlotRow,
    WorkflowVersionHttpRequirementRow,
    WorkflowVersionModelRefRow,
    WorkflowVersionRow,
)
from deerflow.trace_context import normalize_trace_id
from deerflow.workflows.admission import (
    WorkflowRunAdmissionRequest,
    materialize_workflow_run_inputs_v1,
)
from deerflow.workflows.compiler.ir import FrozenObject, freeze_json, thaw_json
from deerflow.workflows.event_contracts import (
    WORKFLOW_EVENT_TYPES,
    canonical_workflow_event_activation_id,
    canonical_workflow_event_database_positive_int,
    canonical_workflow_event_payload_v1,
)

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_SLOT_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}")
_POLICY_ID = re.compile(r"[a-z][a-z0-9._-]{0,127}")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_DEFINITION_LIST_SORTS = frozenset({"updated_desc", "name_asc", "name_desc"})
_DEFINITION_LIFECYCLES = frozenset({"active", "archived"})
_DEFINITION_PUBLICATIONS = frozenset({"all", "draft_only", "published"})
_DEFINITION_CURSOR_VERSION = 1
_VERSION_CURSOR_VERSION = 1
WORKFLOW_CONTROL_OPERATIONS = frozenset(
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
_EMPTY_PROFILE_KEY = "0" * 64
_ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "side_effect_unknown"})
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead"})
_EVENT_TYPES = WORKFLOW_EVENT_TYPES


class WorkflowPersistenceError(RuntimeError):
    """Base class for safe, storage-independent Workflow conflicts."""


class WorkflowAuthorityMissing(WorkflowPersistenceError):
    """The Project, Membership, or scoped Workflow resource is absent."""


class WorkflowDefinitionConflict(WorkflowPersistenceError):
    """A Definition identity or name conflicts with current state."""


class WorkflowDraftCASConflict(WorkflowPersistenceError):
    """The Draft revision/checksum does not match the locked row."""


class WorkflowPublishIdempotencyConflict(WorkflowDraftCASConflict):
    """A durable Publish key was reused with a different request digest."""


class WorkflowControlIdempotencyConflict(WorkflowPersistenceError):
    """A durable control key was reused with a different request digest."""


class WorkflowCredentialGrantConflict(WorkflowPersistenceError):
    """A locked slot, Credential, or exact Credential version is stale."""


class WorkflowRunStateConflict(WorkflowPersistenceError):
    """A Run/Job state transition cannot be applied."""


class WorkflowRunIdempotencyConflict(WorkflowRunStateConflict):
    """A Run idempotency coordinate was reused for different authority."""


class WorkflowManualRetryForbidden(WorkflowRunStateConflict):
    """The source Run cannot be used for a manual retry."""


def _uuid(value: object, *, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a UUID") from None


def _user_id(value: object, *, field_name: str = "user_id") -> str:
    return str(_uuid(value, field_name=field_name))


def _checksum(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _slot(value: object, *, field_name: str = "slot_id") -> str:
    if not isinstance(value, str) or _SLOT_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _policy_id(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _POLICY_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _runtime_contract(value: object) -> str:
    if value != "python3.12-v1":
        raise ValueError("runtime_contract is invalid")
    return value


def _bounded_cursor(value: object, *, field_name: str = "cursor") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise ValueError(f"{field_name} must be a bounded opaque cursor")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError(f"{field_name} must contain visible ASCII only")
    return value


def _encode_cursor(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> dict[str, object]:
    value = _bounded_cursor(value) or ""
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
    if type(payload) is not dict or any(type(key) is not str for key in payload):
        raise ValueError("cursor is invalid")
    return payload


def _cursor_fingerprint(
    *,
    project_id: uuid.UUID,
    query: str | None,
    lifecycle: str,
    publication: str,
    sort: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "lifecycle": lifecycle,
                "project_id": str(project_id),
                "publication": publication,
                "query": query,
                "sort": sort,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def hash_workflow_control_idempotency_key(value: object) -> str:
    """Return the content-free durable identity for one control-plane key."""

    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 255 or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError("Workflow Idempotency-Key must be bounded visible ASCII")
    return hashlib.sha256(b"actweave.workflow.publish-key.v1\x00" + value.encode("ascii")).hexdigest()


def hash_workflow_publish_idempotency_key(value: object) -> str:
    """Compatibility spelling for the frozen v11 Publish receipt identity."""

    return hash_workflow_control_idempotency_key(value)


def canonical_workflow_control_scope_key(
    *,
    project_id: uuid.UUID,
    operation: str,
    workflow_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    slot_id: str | None = None,
) -> str:
    """Build the exact non-secret target scope of a control operation."""

    project_id = _uuid(project_id, field_name="project_id")
    if operation not in WORKFLOW_CONTROL_OPERATIONS:
        raise ValueError("unsupported Workflow control operation")
    if operation == "create":
        if workflow_id is not None or version_id is not None or slot_id is not None:
            raise ValueError("create uses only project scope")
        return f"project:{project_id}"
    workflow_id = _uuid(workflow_id, field_name="workflow_id")
    if operation in {"draft_grant_put", "draft_grant_delete"}:
        if version_id is not None:
            raise ValueError("Draft grant scope cannot contain a Version")
        return f"draft-slot:{workflow_id}:{_slot(slot_id)}"
    if operation in {"version_grant_put", "version_grant_delete"}:
        version_id = _uuid(version_id, field_name="version_id")
        return f"version-slot:{workflow_id}:{version_id}:{_slot(slot_id)}"
    if version_id is not None or slot_id is not None:
        raise ValueError("Definition operation has unexpected scope coordinates")
    return f"definition:{workflow_id}"


def _workflow_control_missing_slot_ids_csv(
    value: object,
) -> tuple[str, ...]:
    """Validate one canonical scalar encoding of safe public slot IDs."""

    if not isinstance(value, str) or len(value) > 33_000:
        raise ValueError("Workflow control missing-slot receipt is invalid")
    if value == "":
        return ()
    slot_ids = tuple(value.split(","))
    if tuple(sorted(set(slot_ids))) != slot_ids:
        raise ValueError("Workflow control missing-slot receipt is not canonical")
    for slot_id in slot_ids:
        _slot(slot_id, field_name="result_missing_slot_ids_csv item")
    if len(slot_ids) > 255:
        raise ValueError("Workflow control missing-slot receipt is too large")
    return slot_ids


def _encode_workflow_control_missing_slot_ids(
    value: Sequence[str],
) -> str:
    slot_ids = tuple(sorted(value))
    encoded = ",".join(slot_ids)
    if _workflow_control_missing_slot_ids_csv(encoded) != slot_ids:
        raise ValueError("Workflow control missing-slot result is invalid")
    return encoded


def _require_strict_json_types(value: object, *, field_name: str) -> None:
    """Reject Python-only containers before the shared G12 freeze authority."""

    stack = [value]
    while stack:
        current = stack.pop()
        if current is None or type(current) in {bool, str, int, float}:
            continue
        if type(current) is list:
            stack.extend(current)
            continue
        if type(current) is dict:
            if any(type(key) is not str for key in current):
                raise TypeError(f"{field_name} JSON object keys must be strings")
            stack.extend(current.values())
            continue
        raise TypeError(f"{field_name} must contain only strict JSON values")


def _object(value: object, *, field_name: str) -> FrozenObject:
    if isinstance(value, FrozenObject):
        value = thaw_json(value)
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a JSON object")
    _require_strict_json_types(value, field_name=field_name)
    frozen = freeze_json(value)
    if not isinstance(frozen, FrozenObject):  # pragma: no cover - guarded above
        raise AssertionError(f"{field_name} must freeze as a JSON object")
    return frozen


def _materialize_object(value: object, *, field_name: str) -> dict[str, object]:
    if not isinstance(value, FrozenObject):
        raise TypeError(f"{field_name} must be a frozen JSON object")
    materialized = thaw_json(value)
    if type(materialized) is not dict:  # pragma: no cover - FrozenObject invariant
        raise AssertionError(f"{field_name} must materialize as a JSON object")
    return materialized


def _positive(value: object, *, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("Workflow timestamps must be timezone-aware")
    return result


@dataclass(frozen=True, slots=True)
class WorkflowRunScope:
    project_id: uuid.UUID
    owner_user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _uuid(self.project_id, field_name="project_id"),
        )
        object.__setattr__(
            self,
            "owner_user_id",
            _user_id(self.owner_user_id, field_name="owner_user_id"),
        )


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionCreate:
    name: str
    description: str
    spec_schema_version: int
    canvas_schema_version: int
    spec: Mapping[str, object] | FrozenObject
    canvas: Mapping[str, object] | FrozenObject
    draft_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name != self.name.strip() or not 1 <= len(self.name) <= 255:
            raise ValueError("Workflow name must be trimmed and between 1 and 255 characters")
        if not isinstance(self.description, str):
            raise TypeError("Workflow description must be a string")
        if len(self.description) > 4096:
            raise ValueError("Workflow description exceeds 4096 characters")
        _positive(self.spec_schema_version, field_name="spec_schema_version")
        _positive(self.canvas_schema_version, field_name="canvas_schema_version")
        object.__setattr__(self, "spec", _object(self.spec, field_name="spec"))
        object.__setattr__(self, "canvas", _object(self.canvas, field_name="canvas"))
        _checksum(self.draft_checksum, field_name="draft_checksum")

    def materialize_spec(self) -> dict[str, object]:
        return _materialize_object(self.spec, field_name="spec")

    def materialize_canvas(self) -> dict[str, object]:
        return _materialize_object(self.canvas, field_name="canvas")


@dataclass(frozen=True, slots=True)
class WorkflowDraftUpdate:
    expected_revision: int
    spec_schema_version: int
    canvas_schema_version: int
    spec: Mapping[str, object] | FrozenObject
    canvas: Mapping[str, object] | FrozenObject
    draft_checksum: str
    credential_slot_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _positive(self.expected_revision, field_name="expected_revision")
        _positive(self.spec_schema_version, field_name="spec_schema_version")
        _positive(self.canvas_schema_version, field_name="canvas_schema_version")
        object.__setattr__(self, "spec", _object(self.spec, field_name="spec"))
        object.__setattr__(self, "canvas", _object(self.canvas, field_name="canvas"))
        _checksum(self.draft_checksum, field_name="draft_checksum")
        if type(self.credential_slot_ids) is not tuple:
            raise TypeError("credential_slot_ids must be a tuple")
        if any(type(slot_id) is not str for slot_id in self.credential_slot_ids):
            raise TypeError("credential_slot_ids must contain strings")
        for slot_id in self.credential_slot_ids:
            _slot(slot_id, field_name="credential_slot_ids item")
        if len(set(self.credential_slot_ids)) != len(self.credential_slot_ids):
            raise ValueError("credential_slot_ids contain duplicate slot IDs")

    def materialize_spec(self) -> dict[str, object]:
        return _materialize_object(self.spec, field_name="spec")

    def materialize_canvas(self) -> dict[str, object]:
        return _materialize_object(self.canvas, field_name="canvas")


@dataclass(frozen=True, slots=True)
class WorkflowModelRefCreate:
    node_id: uuid.UUID
    purpose: str
    logical_model_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _uuid(self.node_id, field_name="node_id"))
        if not isinstance(self.purpose, str) or not self.purpose:
            raise ValueError("model purpose is required")
        if not isinstance(self.logical_model_name, str) or not self.logical_model_name.strip():
            raise ValueError("logical model name is required")


@dataclass(frozen=True, slots=True)
class WorkflowCredentialSlotCreate:
    slot_id: str
    name: str
    purpose: str
    payload_schema: Mapping[str, object] | FrozenObject
    payload_schema_checksum: str

    def __post_init__(self) -> None:
        _slot(self.slot_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("credential slot name is required")
        if not isinstance(self.purpose, str) or not self.purpose:
            raise ValueError("credential slot purpose is required")
        object.__setattr__(
            self,
            "payload_schema",
            _object(self.payload_schema, field_name="payload_schema"),
        )
        _checksum(
            self.payload_schema_checksum,
            field_name="payload_schema_checksum",
        )

    def materialize_payload_schema(self) -> dict[str, object]:
        return _materialize_object(
            self.payload_schema,
            field_name="payload_schema",
        )


@dataclass(frozen=True, slots=True)
class WorkflowCodeRequirementCreate:
    node_id: uuid.UUID
    runtime_contract: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _uuid(self.node_id, field_name="node_id"))
        _runtime_contract(self.runtime_contract)


@dataclass(frozen=True, slots=True)
class WorkflowHttpRequirementCreate:
    node_id: uuid.UUID
    method: str
    endpoint_policy_id: str
    injection_profile_id: str | None = None
    credential_slot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _uuid(self.node_id, field_name="node_id"))
        if self.method not in _HTTP_METHODS:
            raise ValueError("HTTP requirement method is invalid")
        _policy_id(self.endpoint_policy_id, field_name="endpoint_policy_id")
        if (self.injection_profile_id is None) != (self.credential_slot_id is None):
            raise ValueError("HTTP injection profile and Credential slot must be paired")
        if self.injection_profile_id is not None:
            _policy_id(self.injection_profile_id, field_name="injection_profile_id")
            _slot(self.credential_slot_id, field_name="credential_slot_id")


@dataclass(frozen=True, slots=True)
class WorkflowVersionPublish:
    expected_draft_revision: int
    expected_draft_checksum: str
    graph_schema_version: int
    canvas_schema_version: int
    compiler_contract_version: int
    semantic_checksum: str
    model_refs: Sequence[WorkflowModelRefCreate] = field(default_factory=tuple)
    credential_slots: Sequence[WorkflowCredentialSlotCreate] = field(default_factory=tuple)
    code_requirements: Sequence[WorkflowCodeRequirementCreate] = field(default_factory=tuple)
    http_requirements: Sequence[WorkflowHttpRequirementCreate] = field(default_factory=tuple)
    idempotency_hash: str | None = None
    request_digest: str | None = None

    def __post_init__(self) -> None:
        _positive(self.expected_draft_revision, field_name="expected_draft_revision")
        _checksum(
            self.expected_draft_checksum,
            field_name="expected_draft_checksum",
        )
        _positive(self.graph_schema_version, field_name="graph_schema_version")
        _positive(self.canvas_schema_version, field_name="canvas_schema_version")
        _positive(
            self.compiler_contract_version,
            field_name="compiler_contract_version",
        )
        _checksum(self.semantic_checksum, field_name="semantic_checksum")
        refs = tuple(self.model_refs)
        slots = tuple(self.credential_slots)
        code_requirements = tuple(self.code_requirements)
        http_requirements = tuple(self.http_requirements)
        if any(type(item) is not WorkflowModelRefCreate for item in refs):
            raise TypeError("model_refs must contain WorkflowModelRefCreate")
        if any(type(item) is not WorkflowCredentialSlotCreate for item in slots):
            raise TypeError("credential_slots must contain WorkflowCredentialSlotCreate")
        if any(type(item) is not WorkflowCodeRequirementCreate for item in code_requirements):
            raise TypeError("code_requirements must contain WorkflowCodeRequirementCreate")
        if any(type(item) is not WorkflowHttpRequirementCreate for item in http_requirements):
            raise TypeError("http_requirements must contain WorkflowHttpRequirementCreate")
        if len({(item.node_id, item.purpose) for item in refs}) != len(refs):
            raise ValueError("model_refs contain duplicate node/purpose coordinates")
        if len({item.slot_id for item in slots}) != len(slots):
            raise ValueError("credential_slots contain duplicate slot IDs")
        if len({item.node_id for item in code_requirements}) != len(code_requirements):
            raise ValueError("code_requirements contain duplicate node IDs")
        if len({item.node_id for item in http_requirements}) != len(http_requirements):
            raise ValueError("http_requirements contain duplicate node IDs")
        slot_ids = {item.slot_id for item in slots}
        if any(item.credential_slot_id is not None and item.credential_slot_id not in slot_ids for item in http_requirements):
            raise ValueError("HTTP requirements must reference a published Credential slot")
        if (self.idempotency_hash is None) != (self.request_digest is None):
            raise ValueError("Publish idempotency hash and request digest must be provided together")
        if self.idempotency_hash is not None:
            _checksum(self.idempotency_hash, field_name="idempotency_hash")
            _checksum(self.request_digest, field_name="request_digest")
        object.__setattr__(self, "model_refs", refs)
        object.__setattr__(self, "credential_slots", slots)
        object.__setattr__(self, "code_requirements", code_requirements)
        object.__setattr__(self, "http_requirements", http_requirements)


@dataclass(frozen=True, slots=True)
class WorkflowRunCreate:
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    requested_workflow_version_id: uuid.UUID | None
    inputs: Mapping[str, object]
    input_digest: str
    idempotency_hash: str
    admission_request_digest: str
    trigger_kind: Literal["manual", "api"]
    trigger_ref: str | None
    origin_trace_id: str
    required_worker_profile_digest: str | None
    retry_of_run_id: uuid.UUID | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workflow_id",
            _uuid(self.workflow_id, field_name="workflow_id"),
        )
        object.__setattr__(
            self,
            "workflow_version_id",
            _uuid(self.workflow_version_id, field_name="workflow_version_id"),
        )
        if self.requested_workflow_version_id is not None:
            requested_workflow_version_id = _uuid(
                self.requested_workflow_version_id,
                field_name="requested_workflow_version_id",
            )
            object.__setattr__(
                self,
                "requested_workflow_version_id",
                requested_workflow_version_id,
            )
            if requested_workflow_version_id != self.workflow_version_id:
                raise ValueError("requested_workflow_version_id must match workflow_version_id")
        _checksum(self.input_digest, field_name="input_digest")
        _checksum(self.idempotency_hash, field_name="idempotency_hash")
        _checksum(
            self.admission_request_digest,
            field_name="admission_request_digest",
        )
        if self.trigger_kind not in {"manual", "api"}:
            raise ValueError("unsupported Workflow trigger kind")
        if self.trigger_ref is not None and (not isinstance(self.trigger_ref, str) or not self.trigger_ref or len(self.trigger_ref) > 128):
            raise ValueError("trigger_ref must be a non-empty bounded string")
        trace_id = normalize_trace_id(self.origin_trace_id)
        if trace_id is None:
            raise ValueError("origin_trace_id is invalid")
        object.__setattr__(self, "origin_trace_id", trace_id)
        if self.required_worker_profile_digest is not None:
            _checksum(
                self.required_worker_profile_digest,
                field_name="required_worker_profile_digest",
            )
        if self.retry_of_run_id is not None:
            object.__setattr__(
                self,
                "retry_of_run_id",
                _uuid(self.retry_of_run_id, field_name="retry_of_run_id"),
            )
        admission = WorkflowRunAdmissionRequest(
            requested_workflow_version_id=self.requested_workflow_version_id,
            inputs=self.inputs,
            trigger_kind=self.trigger_kind,
            trigger_ref=self.trigger_ref,
            retry_of_run_id=self.retry_of_run_id,
        )
        object.__setattr__(self, "inputs", admission.inputs)
        if self.input_digest != admission.input_digest:
            raise ValueError("input_digest does not match canonical inputs")
        if self.admission_request_digest != admission.digest:
            raise ValueError("admission_request_digest does not match client admission coordinates")

    def materialize_inputs(self) -> dict[str, object]:
        return materialize_workflow_run_inputs_v1(self.inputs)


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionListQuery:
    query: str | None = None
    lifecycle: Literal["active", "archived"] = "active"
    publication: Literal["all", "draft_only", "published"] = "all"
    sort: Literal["updated_desc", "name_asc", "name_desc"] = "updated_desc"
    cursor: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if self.query is not None:
            if not isinstance(self.query, str):
                raise TypeError("query must be a string")
            normalized_query = self.query.strip()
            if len(normalized_query) > 255:
                raise ValueError("query exceeds 255 characters")
            object.__setattr__(self, "query", normalized_query or None)
        if self.lifecycle not in _DEFINITION_LIFECYCLES:
            raise ValueError("unsupported Workflow lifecycle filter")
        if self.publication not in _DEFINITION_PUBLICATIONS:
            raise ValueError("unsupported Workflow publication filter")
        if self.sort not in _DEFINITION_LIST_SORTS:
            raise ValueError("unsupported Workflow sort")
        object.__setattr__(self, "cursor", _bounded_cursor(self.cursor))
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("Workflow list limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionArchive:
    expected_revision: int

    def __post_init__(self) -> None:
        _positive(self.expected_revision, field_name="expected_revision")


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionUpdate:
    expected_revision: int
    name: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        _positive(self.expected_revision, field_name="expected_revision")
        if self.name is None and self.description is None:
            raise ValueError("Definition update requires at least one field")
        if self.name is not None and (not isinstance(self.name, str) or self.name != self.name.strip() or not 1 <= len(self.name) <= 255):
            raise ValueError("Workflow name must be trimmed and between 1 and 255 characters")
        if self.description is not None and (not isinstance(self.description, str) or len(self.description) > 4096):
            raise ValueError("Workflow description exceeds 4096 characters")


@dataclass(frozen=True, slots=True)
class WorkflowCredentialGrantPut:
    credential_id: uuid.UUID
    expected_credential_version_id: uuid.UUID
    expected_slot_schema_checksum: str
    resolved_slot_schema_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "credential_id",
            _uuid(self.credential_id, field_name="credential_id"),
        )
        object.__setattr__(
            self,
            "expected_credential_version_id",
            _uuid(
                self.expected_credential_version_id,
                field_name="expected_credential_version_id",
            ),
        )
        _checksum(
            self.expected_slot_schema_checksum,
            field_name="expected_slot_schema_checksum",
        )
        _checksum(
            self.resolved_slot_schema_checksum,
            field_name="resolved_slot_schema_checksum",
        )
        if self.expected_slot_schema_checksum != self.resolved_slot_schema_checksum:
            raise ValueError("expected slot schema checksum is stale")


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionRecord:
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str
    status: str
    current_published_version_id: uuid.UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime
    current_published_version_number: int | None = None
    draft_revision: int | None = None
    draft_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDefinitionPage:
    items: tuple[WorkflowDefinitionRecord, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(type(item) is not WorkflowDefinitionRecord for item in items):
            raise TypeError("Definition page contains an invalid record")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "next_cursor", _bounded_cursor(self.next_cursor))


@dataclass(frozen=True, slots=True)
class WorkflowDraftRecord:
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    revision: int
    spec_schema_version: int
    canvas_schema_version: int
    spec: dict[str, object]
    canvas: dict[str, object]
    draft_checksum: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowVersionModelRefRecord:
    node_id: uuid.UUID
    purpose: str
    logical_model_name: str


@dataclass(frozen=True, slots=True)
class WorkflowVersionCredentialSlotRecord:
    slot_id: str
    name: str
    purpose: str
    payload_schema: FrozenObject
    payload_schema_checksum: str
    required: bool

    def materialize_payload_schema(self) -> dict[str, object]:
        return _materialize_object(
            self.payload_schema,
            field_name="payload_schema",
        )


@dataclass(frozen=True, slots=True)
class WorkflowVersionCodeRequirementRecord:
    node_id: uuid.UUID
    runtime_contract: str


@dataclass(frozen=True, slots=True)
class WorkflowVersionHttpRequirementRecord:
    node_id: uuid.UUID
    method: str
    endpoint_policy_id: str
    injection_profile_id: str | None
    credential_slot_id: str | None


@dataclass(frozen=True, slots=True)
class WorkflowVersionRecord:
    version_id: uuid.UUID
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    version_number: int
    graph_schema_version: int
    canvas_schema_version: int
    compiler_contract_version: int
    semantic_checksum: str
    published_at: datetime
    spec: FrozenObject = field(default_factory=lambda: _object({}, field_name="spec"))
    canvas: FrozenObject = field(default_factory=lambda: _object({}, field_name="canvas"))
    model_refs: tuple[WorkflowVersionModelRefRecord, ...] = field(default_factory=tuple)
    credential_slots: tuple[WorkflowVersionCredentialSlotRecord, ...] = field(default_factory=tuple)
    code_requirements: tuple[WorkflowVersionCodeRequirementRecord, ...] = field(default_factory=tuple)
    http_requirements: tuple[WorkflowVersionHttpRequirementRecord, ...] = field(default_factory=tuple)
    published_by: str = ""
    active_grants: tuple[WorkflowCredentialGrantRecord, ...] = field(default_factory=tuple)
    missing_required_slot_ids: tuple[str, ...] = field(default_factory=tuple)
    executable: bool = True

    def materialize_spec(self) -> dict[str, object]:
        return _materialize_object(self.spec, field_name="spec")

    def materialize_canvas(self) -> dict[str, object]:
        return _materialize_object(self.canvas, field_name="canvas")


@dataclass(frozen=True, slots=True)
class WorkflowVersionPublishResult:
    record: WorkflowVersionRecord
    created: bool

    def __post_init__(self) -> None:
        if type(self.record) is not WorkflowVersionRecord:
            raise TypeError("Publish result requires a WorkflowVersionRecord")
        if type(self.created) is not bool:
            raise TypeError("Publish result created flag must be a bool")

    # G13 callers predate the explicit mutation bit.  These two identity
    # projections keep their read-only assertions source-compatible while new
    # control-plane code must consume ``record`` and ``created`` explicitly.
    @property
    def version_id(self) -> uuid.UUID:
        return self.record.version_id

    @property
    def version_number(self) -> int:
        return self.record.version_number


@dataclass(frozen=True, slots=True)
class WorkflowVersionPage:
    items: tuple[WorkflowVersionRecord, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(type(item) is not WorkflowVersionRecord for item in items):
            raise TypeError("Version page contains an invalid record")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "next_cursor", _bounded_cursor(self.next_cursor))


@dataclass(frozen=True, slots=True)
class WorkflowDraftCredentialGrantIntentRecord:
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    slot_id: str
    slot_schema_checksum: str
    credential_id: uuid.UUID
    expected_credential_version_id: uuid.UUID
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowCredentialGrantRecord:
    grant_id: uuid.UUID
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    workflow_version_id: uuid.UUID
    slot_id: str
    payload_schema_checksum: str
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    status: Literal["active", "revoked"]
    revision: int
    granted_by: str
    revoked_by: str | None
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkflowControlOperationCreate:
    project_id: uuid.UUID
    workflow_id: uuid.UUID
    operation: str
    idempotency_hash: str
    request_digest: str
    created_by: str
    result_version_id: uuid.UUID | None = None
    result_revision: int | None = None
    result_checksum: str | None = None
    result_slot_id: str | None = None
    result_credential_id: uuid.UUID | None = None
    result_credential_version_id: uuid.UUID | None = None
    result_status: Literal["active", "revoked"] | None = None
    result_deleted: bool | None = None
    result_created_at: datetime | None = None
    result_updated_at: datetime | None = None
    result_revoked_at: datetime | None = None
    result_name: str | None = None
    result_description: str | None = None
    result_lifecycle: Literal["active", "archived"] | None = None
    result_published_version_id: uuid.UUID | None = None
    result_published_version_number: int | None = None
    result_draft_revision: int | None = None
    result_draft_checksum: str | None = None
    result_missing_slot_ids_csv: str | None = None

    @property
    def scope_key(self) -> str:
        """Derive the durable identity from validated result coordinates."""

        return canonical_workflow_control_scope_key(
            project_id=self.project_id,
            operation=self.operation,
            workflow_id=None if self.operation == "create" else self.workflow_id,
            version_id=(self.result_version_id if self.operation in {"version_grant_put", "version_grant_delete"} else None),
            slot_id=(
                self.result_slot_id
                if self.operation
                in {
                    "draft_grant_put",
                    "draft_grant_delete",
                    "version_grant_put",
                    "version_grant_delete",
                }
                else None
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _uuid(self.project_id, field_name="project_id"))
        object.__setattr__(self, "workflow_id", _uuid(self.workflow_id, field_name="workflow_id"))
        if self.operation not in WORKFLOW_CONTROL_OPERATIONS:
            raise ValueError("unsupported Workflow control operation")
        _checksum(self.idempotency_hash, field_name="idempotency_hash")
        _checksum(self.request_digest, field_name="request_digest")
        object.__setattr__(self, "created_by", _user_id(self.created_by, field_name="created_by"))
        if self.result_version_id is not None:
            object.__setattr__(self, "result_version_id", _uuid(self.result_version_id, field_name="result_version_id"))
        if self.result_revision is not None:
            _positive(self.result_revision, field_name="result_revision")
        if self.result_checksum is not None:
            _checksum(self.result_checksum, field_name="result_checksum")
        if self.result_slot_id is not None:
            _slot(self.result_slot_id, field_name="result_slot_id")
        if self.result_credential_id is not None:
            object.__setattr__(self, "result_credential_id", _uuid(self.result_credential_id, field_name="result_credential_id"))
        if self.result_credential_version_id is not None:
            object.__setattr__(
                self,
                "result_credential_version_id",
                _uuid(self.result_credential_version_id, field_name="result_credential_version_id"),
            )
        if self.result_published_version_id is not None:
            object.__setattr__(
                self,
                "result_published_version_id",
                _uuid(
                    self.result_published_version_id,
                    field_name="result_published_version_id",
                ),
            )
        if self.result_published_version_number is not None:
            _positive(
                self.result_published_version_number,
                field_name="result_published_version_number",
            )
        if self.result_draft_revision is not None:
            _positive(self.result_draft_revision, field_name="result_draft_revision")
        if self.result_draft_checksum is not None:
            _checksum(
                self.result_draft_checksum,
                field_name="result_draft_checksum",
            )
        if self.operation == "publish":
            if self.result_missing_slot_ids_csv is None:
                raise ValueError("Workflow control Publish result is incomplete")
            _workflow_control_missing_slot_ids_csv(self.result_missing_slot_ids_csv)
        elif self.result_missing_slot_ids_csv is not None:
            raise ValueError("Workflow control missing-slot result is unexpected")
        if self.result_status not in {None, "active", "revoked"}:
            raise ValueError("Workflow control result status is invalid")
        if self.result_lifecycle not in {None, "active", "archived"}:
            raise ValueError("Workflow control result lifecycle is invalid")
        if self.result_deleted is not None and type(self.result_deleted) is not bool:
            raise TypeError("Workflow control result_deleted must be a bool")
        for field_name in ("result_created_at", "result_updated_at", "result_revoked_at"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.result_name is not None and (not isinstance(self.result_name, str) or self.result_name != self.result_name.strip() or not 1 <= len(self.result_name) <= 255):
            raise ValueError("Workflow control result name is invalid")
        if self.result_description is not None and (not isinstance(self.result_description, str) or len(self.result_description) > 4096):
            raise ValueError("Workflow control result description is invalid")

        definition_values = (
            self.result_name,
            self.result_description,
            self.result_lifecycle,
            self.result_published_version_id,
            self.result_published_version_number,
            self.result_draft_revision,
            self.result_draft_checksum,
        )
        publication_complete = (self.result_published_version_id is None) is (self.result_published_version_number is None)
        if not publication_complete:
            raise ValueError("Workflow control published coordinates disagree")

        if self.operation in {"create", "update", "archive"}:
            if (
                self.result_version_id is not None
                or self.result_revision is None
                or self.result_checksum is not None
                or self.result_slot_id is not None
                or self.result_credential_id is not None
                or self.result_credential_version_id is not None
                or self.result_status is not None
                or self.result_deleted is not None
                or self.result_created_at is None
                or self.result_updated_at is None
                or self.result_revoked_at is not None
                or self.result_name is None
                or self.result_description is None
                or self.result_draft_revision is None
                or self.result_draft_checksum is None
                or self.result_lifecycle != ("archived" if self.operation == "archive" else "active")
            ):
                raise ValueError("Workflow control Definition result shape is invalid")
        elif self.operation == "save_draft":
            if (
                self.result_version_id is not None
                or self.result_revision is None
                or self.result_checksum is not None
                or self.result_slot_id is not None
                or self.result_credential_id is not None
                or self.result_credential_version_id is not None
                or self.result_status is not None
                or self.result_deleted is not None
                or self.result_created_at is not None
                or self.result_updated_at is None
                or self.result_revoked_at is not None
                or any(value is not None for value in definition_values[:-1])
                or self.result_draft_checksum is None
            ):
                raise ValueError("Workflow control Draft result shape is invalid")
        elif self.operation == "publish":
            if self.result_version_id is None or any(
                value is not None
                for value in (
                    self.result_revision,
                    self.result_checksum,
                    self.result_slot_id,
                    self.result_credential_id,
                    self.result_credential_version_id,
                    self.result_status,
                    self.result_deleted,
                    self.result_created_at,
                    self.result_updated_at,
                    self.result_revoked_at,
                    *definition_values,
                )
            ):
                raise ValueError("Workflow control Publish result shape is invalid")
        elif self.operation == "draft_grant_put":
            if (
                self.result_version_id is not None
                or self.result_revision is not None
                or self.result_checksum is None
                or self.result_slot_id is None
                or self.result_credential_id is None
                or self.result_credential_version_id is None
                or self.result_status is not None
                or self.result_deleted is not None
                or self.result_created_at is not None
                or self.result_updated_at is None
                or self.result_revoked_at is not None
                or any(value is not None for value in definition_values)
            ):
                raise ValueError("Workflow control Draft grant result shape is invalid")
        elif self.operation == "draft_grant_delete":
            if (
                self.result_version_id is not None
                or self.result_revision is not None
                or self.result_checksum is not None
                or self.result_slot_id is None
                or self.result_credential_id is not None
                or self.result_credential_version_id is not None
                or self.result_status is not None
                or self.result_deleted is not True
                or self.result_created_at is not None
                or self.result_updated_at is not None
                or self.result_revoked_at is not None
                or any(value is not None for value in definition_values)
            ):
                raise ValueError("Workflow control Draft grant delete result shape is invalid")
        else:
            expected_status = "active" if self.operation == "version_grant_put" else "revoked"
            if (
                self.result_version_id is None
                or self.result_revision is None
                or self.result_checksum is None
                or self.result_slot_id is None
                or self.result_credential_id is None
                or self.result_credential_version_id is None
                or self.result_status != expected_status
                or self.result_deleted is not None
                or self.result_created_at is None
                or self.result_updated_at is not None
                or (self.result_revoked_at is None) is (self.operation == "version_grant_delete")
                or any(value is not None for value in definition_values)
            ):
                raise ValueError("Workflow control Version grant result shape is invalid")

        # Force construction of the canonical scope after every result field
        # has been validated.  A caller cannot provide or override this value.
        _ = self.scope_key


@dataclass(frozen=True, slots=True)
class WorkflowControlOperationRecord:
    project_id: uuid.UUID
    workflow_id: uuid.UUID
    operation: str
    scope_key: str
    idempotency_hash: str
    request_digest: str
    created_by: str
    created_at: datetime
    result_version_id: uuid.UUID | None = None
    result_revision: int | None = None
    result_checksum: str | None = None
    result_slot_id: str | None = None
    result_credential_id: uuid.UUID | None = None
    result_credential_version_id: uuid.UUID | None = None
    result_status: Literal["active", "revoked"] | None = None
    result_deleted: bool | None = None
    result_created_at: datetime | None = None
    result_updated_at: datetime | None = None
    result_revoked_at: datetime | None = None
    result_name: str | None = None
    result_description: str | None = None
    result_lifecycle: Literal["active", "archived"] | None = None
    result_published_version_id: uuid.UUID | None = None
    result_published_version_number: int | None = None
    result_draft_revision: int | None = None
    result_draft_checksum: str | None = None
    result_missing_slot_ids_csv: str | None = None

    def __post_init__(self) -> None:
        command = WorkflowControlOperationCreate(
            project_id=self.project_id,
            workflow_id=self.workflow_id,
            operation=self.operation,
            idempotency_hash=self.idempotency_hash,
            request_digest=self.request_digest,
            created_by=self.created_by,
            result_version_id=self.result_version_id,
            result_revision=self.result_revision,
            result_checksum=self.result_checksum,
            result_slot_id=self.result_slot_id,
            result_credential_id=self.result_credential_id,
            result_credential_version_id=self.result_credential_version_id,
            result_status=self.result_status,
            result_deleted=self.result_deleted,
            result_created_at=self.result_created_at,
            result_updated_at=self.result_updated_at,
            result_revoked_at=self.result_revoked_at,
            result_name=self.result_name,
            result_description=self.result_description,
            result_lifecycle=self.result_lifecycle,
            result_published_version_id=self.result_published_version_id,
            result_published_version_number=self.result_published_version_number,
            result_draft_revision=self.result_draft_revision,
            result_draft_checksum=self.result_draft_checksum,
            result_missing_slot_ids_csv=self.result_missing_slot_ids_csv,
        )
        if self.scope_key != command.scope_key:
            raise ValueError("Workflow control stored scope does not match result coordinates")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WorkflowRunRecord:
    run_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    status: str
    inputs: dict[str, object]
    output: dict[str, object] | None
    input_digest: str
    idempotency_hash: str
    admission_request_digest: str
    trigger_kind: str
    trigger_ref: str | None
    origin_trace_id: str
    required_worker_profile_digest: str | None
    execution_epoch: int
    current_job_id: uuid.UUID | None
    retry_of_run_id: uuid.UUID | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRunStage:
    record: WorkflowRunRecord
    created: bool


@dataclass(frozen=True, slots=True)
class WorkflowRunJobRecord:
    run_id: uuid.UUID
    execution_epoch: int
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    worker_profile_key: str
    cause: str
    job_status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRetrySource:
    source_run_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    inputs: dict[str, object]
    input_digest: str
    required_worker_profile_digest: str | None
    source_origin_trace_id: str


@dataclass(frozen=True, slots=True)
class WorkflowRunEventAppend:
    event_type: str
    payload: Mapping[str, object] | FrozenObject = field(default_factory=dict)
    node_id: uuid.UUID | None = None
    activation_id: str | None = None
    scope_path_hash: str | None = None
    iteration_path: Sequence[int] = field(default_factory=tuple)
    attempt: int | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.event_type not in _EVENT_TYPES:
            raise ValueError("unsupported Workflow event type")
        source_payload = _materialize_object(self.payload, field_name="payload") if isinstance(self.payload, FrozenObject) else self.payload
        object.__setattr__(
            self,
            "payload",
            _object(
                canonical_workflow_event_payload_v1(
                    self.event_type,
                    source_payload,
                ),
                field_name="payload",
            ),
        )
        if self.node_id is not None:
            object.__setattr__(
                self,
                "node_id",
                _uuid(self.node_id, field_name="node_id"),
            )
        path = tuple(
            canonical_workflow_event_database_positive_int(
                item,
                field_name="iteration_path item",
            )
            for item in self.iteration_path
        )
        if len(path) > 16:
            raise ValueError("iteration_path exceeds the maximum depth")
        object.__setattr__(self, "iteration_path", path)
        if self.event_type.startswith("workflow.node."):
            if self.node_id is None or self.activation_id is None or self.scope_path_hash is None or self.attempt is None:
                raise ValueError("node events require activation authority")
            object.__setattr__(
                self,
                "activation_id",
                canonical_workflow_event_activation_id(self.activation_id),
            )
            _checksum(self.scope_path_hash, field_name="scope_path_hash")
            object.__setattr__(
                self,
                "attempt",
                canonical_workflow_event_database_positive_int(
                    self.attempt,
                    field_name="attempt",
                ),
            )
        elif self.node_id is not None or self.activation_id is not None or self.scope_path_hash is not None or self.attempt is not None or path:
            raise ValueError("Run events cannot carry node activation authority")
        if self.occurred_at is not None:
            _now(self.occurred_at)

    def materialize_payload(self) -> dict[str, object]:
        return _materialize_object(self.payload, field_name="payload")


@dataclass(frozen=True, slots=True)
class WorkflowExecutionFence:
    scope: WorkflowRunScope
    run_id: uuid.UUID
    job_id: uuid.UUID
    execution_epoch: int
    job_attempt_number: int
    worker_id: uuid.UUID
    lease_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.scope) is not WorkflowRunScope:
            raise TypeError("WorkflowRunScope is required")
        object.__setattr__(self, "run_id", _uuid(self.run_id, field_name="run_id"))
        object.__setattr__(self, "job_id", _uuid(self.job_id, field_name="job_id"))
        object.__setattr__(
            self,
            "execution_epoch",
            _positive(self.execution_epoch, field_name="execution_epoch"),
        )
        object.__setattr__(
            self,
            "job_attempt_number",
            _positive(self.job_attempt_number, field_name="job_attempt_number"),
        )
        object.__setattr__(
            self,
            "worker_id",
            _uuid(self.worker_id, field_name="worker_id"),
        )
        if not isinstance(self.lease_token, str) or not self.lease_token:
            raise ValueError("lease_token is required")

    @property
    def lease_token_hash(self) -> str:
        return hashlib.sha256(self.lease_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkflowRunEventRecord:
    event_id: int
    run_id: uuid.UUID
    workflow_version_id: uuid.UUID
    seq: int
    event_type: str
    node_id: uuid.UUID | None
    activation_id: str | None
    scope_path_hash: str | None
    iteration_path: tuple[int, ...]
    attempt: int | None
    payload: dict[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRunRetentionReport:
    run_id: uuid.UUID
    terminal: bool
    ordinary_delete_supported: bool
    epoch_job_count: int
    core_snapshot_count: int
    runtime_policy_snapshot_count: int
    model_snapshot_count: int
    code_snapshot_count: int
    http_snapshot_count: int
    event_count: int
    effect_count: int
    open_effect_count: int
    code_lease_count: int
    open_code_lease_count: int

    @property
    def retention_candidate(self) -> bool:
        return self.terminal and self.open_effect_count == 0 and self.open_code_lease_count == 0


class WorkflowRepository:
    """One-session Workflow aggregate repository; callers own commit."""

    def __init__(self, session: AsyncSession) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("AsyncSession is required")
        self.session = session

    @staticmethod
    def _definition_record(
        row: WorkflowDefinitionRow,
        *,
        draft_revision: int | None = None,
        draft_checksum: str | None = None,
        current_published_version_number: int | None = None,
    ) -> WorkflowDefinitionRecord:
        return WorkflowDefinitionRecord(
            workflow_id=row.id,
            project_id=row.project_id,
            name=row.name,
            description=row.description,
            status=row.status,
            current_published_version_id=row.current_published_version_id,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
            current_published_version_number=current_published_version_number,
            draft_revision=draft_revision,
            draft_checksum=draft_checksum,
        )

    @staticmethod
    def _draft_record(row: WorkflowDraftRow) -> WorkflowDraftRecord:
        return WorkflowDraftRecord(
            workflow_id=row.workflow_id,
            project_id=row.project_id,
            revision=row.revision,
            spec_schema_version=row.spec_schema_version,
            canvas_schema_version=row.canvas_schema_version,
            spec=dict(row.spec_json),
            canvas=dict(row.canvas_json),
            draft_checksum=row.draft_checksum,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _model_ref_record(
        row: WorkflowVersionModelRefRow,
    ) -> WorkflowVersionModelRefRecord:
        return WorkflowVersionModelRefRecord(
            node_id=row.node_id,
            purpose=row.purpose,
            logical_model_name=row.logical_model_name,
        )

    @staticmethod
    def _credential_slot_record(
        row: WorkflowVersionCredentialSlotRow,
    ) -> WorkflowVersionCredentialSlotRecord:
        return WorkflowVersionCredentialSlotRecord(
            slot_id=row.slot_id,
            name=row.name,
            purpose=row.purpose,
            payload_schema=_object(
                row.payload_schema_json,
                field_name="payload_schema",
            ),
            payload_schema_checksum=row.payload_schema_checksum,
            required=row.required,
        )

    @staticmethod
    def _code_requirement_record(
        row: WorkflowVersionCodeRequirementRow,
    ) -> WorkflowVersionCodeRequirementRecord:
        return WorkflowVersionCodeRequirementRecord(
            node_id=row.node_id,
            runtime_contract=row.runtime_contract,
        )

    @staticmethod
    def _http_requirement_record(
        row: WorkflowVersionHttpRequirementRow,
    ) -> WorkflowVersionHttpRequirementRecord:
        return WorkflowVersionHttpRequirementRecord(
            node_id=row.node_id,
            method=row.method,
            endpoint_policy_id=row.endpoint_policy_id,
            injection_profile_id=row.injection_profile_id,
            credential_slot_id=row.credential_slot_id,
        )

    @staticmethod
    def _version_record(
        row: WorkflowVersionRow,
        *,
        model_refs: Sequence[WorkflowVersionModelRefRow] = (),
        credential_slots: Sequence[WorkflowVersionCredentialSlotRow] = (),
        code_requirements: Sequence[WorkflowVersionCodeRequirementRow] = (),
        http_requirements: Sequence[WorkflowVersionHttpRequirementRow] = (),
        active_grants: Sequence[WorkflowCredentialGrantRow] = (),
    ) -> WorkflowVersionRecord:
        slot_records = tuple(WorkflowRepository._credential_slot_record(slot) for slot in credential_slots)
        grant_records = tuple(
            WorkflowRepository._credential_grant_record(
                grant,
                workflow_id=row.workflow_id,
            )
            for grant in active_grants
        )
        active_slot_ids = {grant.slot_id for grant in grant_records}
        missing_required_slot_ids = tuple(slot.slot_id for slot in slot_records if slot.required and slot.slot_id not in active_slot_ids)
        return WorkflowVersionRecord(
            version_id=row.id,
            workflow_id=row.workflow_id,
            project_id=row.project_id,
            version_number=row.version_number,
            graph_schema_version=row.graph_schema_version,
            canvas_schema_version=row.canvas_schema_version,
            compiler_contract_version=row.compiler_contract_version,
            semantic_checksum=row.semantic_checksum,
            published_at=row.published_at,
            spec=_object(row.spec_json, field_name="spec"),
            canvas=_object(row.canvas_json, field_name="canvas"),
            model_refs=tuple(WorkflowRepository._model_ref_record(reference) for reference in model_refs),
            credential_slots=slot_records,
            code_requirements=tuple(WorkflowRepository._code_requirement_record(requirement) for requirement in code_requirements),
            http_requirements=tuple(WorkflowRepository._http_requirement_record(requirement) for requirement in http_requirements),
            published_by=row.published_by,
            active_grants=grant_records,
            missing_required_slot_ids=missing_required_slot_ids,
            executable=not missing_required_slot_ids,
        )

    @staticmethod
    def _draft_grant_intent_record(
        row: WorkflowDraftCredentialGrantIntentRow,
    ) -> WorkflowDraftCredentialGrantIntentRecord:
        return WorkflowDraftCredentialGrantIntentRecord(
            workflow_id=row.workflow_id,
            project_id=row.project_id,
            slot_id=row.slot_id,
            slot_schema_checksum=row.slot_schema_checksum,
            credential_id=row.credential_id,
            expected_credential_version_id=row.expected_credential_version_id,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _credential_grant_record(
        row: WorkflowCredentialGrantRow,
        *,
        workflow_id: uuid.UUID,
    ) -> WorkflowCredentialGrantRecord:
        return WorkflowCredentialGrantRecord(
            grant_id=row.id,
            workflow_id=workflow_id,
            project_id=row.project_id,
            workflow_version_id=row.workflow_version_id,
            slot_id=row.slot_id,
            payload_schema_checksum=row.payload_schema_checksum,
            credential_id=row.credential_id,
            credential_version_id=row.credential_version_id,
            status=row.status,
            revision=row.revision,
            granted_by=row.granted_by,
            revoked_by=row.revoked_by,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )

    @staticmethod
    def _control_operation_record(
        row: WorkflowControlOperationRow,
    ) -> WorkflowControlOperationRecord:
        return WorkflowControlOperationRecord(
            project_id=row.project_id,
            workflow_id=row.workflow_id,
            operation=row.operation,
            scope_key=row.scope_key,
            idempotency_hash=row.idempotency_hash,
            request_digest=row.request_digest,
            created_by=row.created_by,
            created_at=row.created_at,
            result_version_id=row.result_version_id,
            result_revision=row.result_revision,
            result_checksum=row.result_checksum,
            result_slot_id=row.result_slot_id,
            result_credential_id=row.result_credential_id,
            result_credential_version_id=row.result_credential_version_id,
            result_status=row.result_status,
            result_deleted=row.result_deleted,
            result_created_at=row.result_created_at,
            result_updated_at=row.result_updated_at,
            result_revoked_at=row.result_revoked_at,
            result_name=row.result_name,
            result_description=row.result_description,
            result_lifecycle=row.result_lifecycle,
            result_published_version_id=row.result_published_version_id,
            result_published_version_number=(row.result_published_version_number),
            result_draft_revision=row.result_draft_revision,
            result_draft_checksum=row.result_draft_checksum,
            result_missing_slot_ids_csv=row.result_missing_slot_ids_csv,
        )

    @staticmethod
    def _publish_replay_record(
        record: WorkflowVersionRecord,
        receipt: WorkflowControlOperationRow,
    ) -> WorkflowVersionRecord:
        if receipt.operation != "publish":
            raise ValueError("Publish receipt is required")
        missing = _workflow_control_missing_slot_ids_csv(receipt.result_missing_slot_ids_csv)
        required = {slot.slot_id for slot in record.credential_slots if slot.required}
        if not set(missing) <= required:
            raise ValueError("Publish receipt contains an unknown slot")
        return replace(
            record,
            missing_required_slot_ids=missing,
            executable=not missing,
        )

    async def _complete_version_records(
        self,
        rows: Sequence[WorkflowVersionRow],
    ) -> tuple[WorkflowVersionRecord, ...]:
        version_rows = tuple(rows)
        if not version_rows:
            return ()
        version_ids = tuple(row.id for row in version_rows)
        model_rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionModelRefRow)
                    .where(WorkflowVersionModelRefRow.workflow_version_id.in_(version_ids))
                    .order_by(
                        WorkflowVersionModelRefRow.workflow_version_id,
                        WorkflowVersionModelRefRow.node_id,
                        WorkflowVersionModelRefRow.purpose,
                    )
                )
            ).scalars()
        )
        slot_rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionCredentialSlotRow)
                    .where(WorkflowVersionCredentialSlotRow.workflow_version_id.in_(version_ids))
                    .order_by(
                        WorkflowVersionCredentialSlotRow.workflow_version_id,
                        WorkflowVersionCredentialSlotRow.slot_id,
                    )
                )
            ).scalars()
        )
        code_rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionCodeRequirementRow)
                    .where(WorkflowVersionCodeRequirementRow.workflow_version_id.in_(version_ids))
                    .order_by(
                        WorkflowVersionCodeRequirementRow.workflow_version_id,
                        WorkflowVersionCodeRequirementRow.node_id,
                    )
                )
            ).scalars()
        )
        http_rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionHttpRequirementRow)
                    .where(WorkflowVersionHttpRequirementRow.workflow_version_id.in_(version_ids))
                    .order_by(
                        WorkflowVersionHttpRequirementRow.workflow_version_id,
                        WorkflowVersionHttpRequirementRow.node_id,
                    )
                )
            ).scalars()
        )
        grant_rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowCredentialGrantRow)
                    .where(
                        WorkflowCredentialGrantRow.workflow_version_id.in_(version_ids),
                        WorkflowCredentialGrantRow.status == "active",
                    )
                    .order_by(
                        WorkflowCredentialGrantRow.workflow_version_id,
                        WorkflowCredentialGrantRow.slot_id,
                        WorkflowCredentialGrantRow.id,
                    )
                )
            ).scalars()
        )
        refs_by_version: dict[uuid.UUID, list[WorkflowVersionModelRefRow]] = {}
        for reference in model_rows:
            refs_by_version.setdefault(reference.workflow_version_id, []).append(reference)
        slots_by_version: dict[uuid.UUID, list[WorkflowVersionCredentialSlotRow]] = {}
        for slot in slot_rows:
            slots_by_version.setdefault(slot.workflow_version_id, []).append(slot)
        code_by_version: dict[uuid.UUID, list[WorkflowVersionCodeRequirementRow]] = {}
        for requirement in code_rows:
            code_by_version.setdefault(requirement.workflow_version_id, []).append(requirement)
        http_by_version: dict[uuid.UUID, list[WorkflowVersionHttpRequirementRow]] = {}
        for requirement in http_rows:
            http_by_version.setdefault(requirement.workflow_version_id, []).append(requirement)
        grants_by_version: dict[uuid.UUID, list[WorkflowCredentialGrantRow]] = {}
        for grant in grant_rows:
            grants_by_version.setdefault(grant.workflow_version_id, []).append(grant)
        return tuple(
            self._version_record(
                row,
                model_refs=refs_by_version.get(row.id, ()),
                credential_slots=slots_by_version.get(row.id, ()),
                code_requirements=code_by_version.get(row.id, ()),
                http_requirements=http_by_version.get(row.id, ()),
                active_grants=grants_by_version.get(row.id, ()),
            )
            for row in version_rows
        )

    @staticmethod
    def _run_record(row: WorkflowRunRow) -> WorkflowRunRecord:
        return WorkflowRunRecord(
            run_id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            workflow_id=row.workflow_id,
            workflow_version_id=row.workflow_version_id,
            status=row.status,
            inputs=dict(row.input_json),
            output=None if row.output_json is None else dict(row.output_json),
            input_digest=row.input_digest,
            idempotency_hash=row.idempotency_hash,
            admission_request_digest=row.admission_request_digest,
            trigger_kind=row.trigger_kind,
            trigger_ref=row.trigger_ref,
            origin_trace_id=row.origin_trace_id,
            required_worker_profile_digest=row.required_worker_profile_digest,
            execution_epoch=row.execution_epoch,
            current_job_id=row.current_job_id,
            retry_of_run_id=row.retry_of_run_id,
            error_code=row.error_code,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            updated_at=row.updated_at,
        )

    async def _lock_authority(
        self,
        project_id: uuid.UUID,
        actor_user_id: str,
    ) -> None:
        locked_project = await self.session.scalar(
            sa.select(ProjectRow.id)
            .where(
                ProjectRow.id == project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
            .with_for_update(of=ProjectRow)
        )
        if locked_project is None:
            raise WorkflowAuthorityMissing
        locked_membership = await self.session.scalar(
            sa.select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == actor_user_id,
                ProjectMembershipRow.status == "active",
            )
            .with_for_update(of=ProjectMembershipRow)
        )
        if locked_membership is None:
            raise WorkflowAuthorityMissing

    async def _lock_scope_authority(self, scope: WorkflowRunScope) -> None:
        if type(scope) is not WorkflowRunScope:
            raise TypeError("WorkflowRunScope is required")
        await self._lock_authority(scope.project_id, scope.owner_user_id)

    async def _lock_project_credential(
        self,
        *,
        project_id: uuid.UUID,
        credential_id: uuid.UUID,
        credential_version_id: uuid.UUID,
        require_current_active: bool,
        stale_is_missing: bool = False,
    ) -> tuple[CredentialRow, CredentialVersionRow] | None:
        credential = (
            await self.session.execute(
                sa.select(CredentialRow)
                .where(
                    CredentialRow.id == credential_id,
                    CredentialRow.project_id == project_id,
                    CredentialRow.scope == "project",
                )
                .with_for_update(of=CredentialRow)
            )
        ).scalar_one_or_none()
        if credential is None:
            if stale_is_missing:
                return None
            raise WorkflowAuthorityMissing
        credential_version = (
            await self.session.execute(
                sa.select(CredentialVersionRow)
                .where(
                    CredentialVersionRow.id == credential_version_id,
                    CredentialVersionRow.credential_id == credential_id,
                )
                .with_for_update(of=CredentialVersionRow)
            )
        ).scalar_one_or_none()
        if credential_version is None:
            if stale_is_missing:
                return None
            raise WorkflowCredentialGrantConflict
        if require_current_active and (credential.status != "active" or credential.is_delete or credential.current_version_id != credential_version_id or credential_version.status != "active"):
            if stale_is_missing:
                return None
            raise WorkflowCredentialGrantConflict
        return credential, credential_version

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
    ) -> WorkflowControlOperationRecord | None:
        """Lock and read one exact durable control-operation identity."""

        project_id = _uuid(project_id, field_name="project_id")
        if operation not in WORKFLOW_CONTROL_OPERATIONS:
            raise ValueError("unsupported Workflow control operation")
        scope_key = canonical_workflow_control_scope_key(
            project_id=project_id,
            operation=operation,
            workflow_id=workflow_id,
            version_id=version_id,
            slot_id=slot_id,
        )
        normalized_workflow_id = None if operation == "create" else _uuid(workflow_id, field_name="workflow_id")
        normalized_version_id = _uuid(version_id, field_name="version_id") if operation in {"version_grant_put", "version_grant_delete"} else None
        normalized_slot_id = (
            _slot(slot_id)
            if operation
            in {
                "draft_grant_put",
                "draft_grant_delete",
                "version_grant_put",
                "version_grant_delete",
            }
            else None
        )
        idempotency_hash = _checksum(idempotency_hash, field_name="idempotency_hash")
        request_digest = _checksum(request_digest, field_name="request_digest")
        if type(lock_identity) is not bool:
            raise TypeError("lock_identity must be a bool")
        if lock_identity:
            lock_material = f"{project_id}:{operation}:{scope_key}:{idempotency_hash}".encode("ascii")
            advisory_key = int.from_bytes(hashlib.sha256(lock_material).digest()[:8], byteorder="big", signed=True)
            await self.session.execute(sa.select(sa.func.pg_advisory_xact_lock(advisory_key)))
        statement = sa.select(WorkflowControlOperationRow).where(
            WorkflowControlOperationRow.project_id == project_id,
            WorkflowControlOperationRow.operation == operation,
            WorkflowControlOperationRow.scope_key == scope_key,
            WorkflowControlOperationRow.idempotency_hash == idempotency_hash,
        )
        if normalized_workflow_id is not None:
            statement = statement.where(WorkflowControlOperationRow.workflow_id == normalized_workflow_id)
        if normalized_version_id is not None:
            statement = statement.where(WorkflowControlOperationRow.result_version_id == normalized_version_id)
        if normalized_slot_id is not None:
            statement = statement.where(WorkflowControlOperationRow.result_slot_id == normalized_slot_id)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        if row.request_digest != request_digest:
            raise WorkflowControlIdempotencyConflict
        return self._control_operation_record(row)

    async def record_control_operation(
        self,
        command: WorkflowControlOperationCreate,
    ) -> WorkflowControlOperationRecord:
        """Append one completed scalar-only receipt in the caller transaction."""

        if type(command) is not WorkflowControlOperationCreate:
            raise TypeError("WorkflowControlOperationCreate is required")
        row = WorkflowControlOperationRow(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            operation=command.operation,
            scope_key=command.scope_key,
            idempotency_hash=command.idempotency_hash,
            request_digest=command.request_digest,
            result_version_id=command.result_version_id,
            result_revision=command.result_revision,
            result_checksum=command.result_checksum,
            result_slot_id=command.result_slot_id,
            result_credential_id=command.result_credential_id,
            result_credential_version_id=command.result_credential_version_id,
            result_status=command.result_status,
            result_deleted=command.result_deleted,
            result_created_at=command.result_created_at,
            result_updated_at=command.result_updated_at,
            result_revoked_at=command.result_revoked_at,
            result_name=command.result_name,
            result_description=command.result_description,
            result_lifecycle=command.result_lifecycle,
            result_published_version_id=command.result_published_version_id,
            result_published_version_number=(command.result_published_version_number),
            result_draft_revision=command.result_draft_revision,
            result_draft_checksum=command.result_draft_checksum,
            result_missing_slot_ids_csv=command.result_missing_slot_ids_csv,
            created_by=command.created_by,
        )
        self.session.add(row)
        await self.session.flush()
        return self._control_operation_record(row)

    async def create_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        command: WorkflowDefinitionCreate,
    ) -> tuple[WorkflowDefinitionRecord, WorkflowDraftRecord]:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        if type(command) is not WorkflowDefinitionCreate:
            raise TypeError("WorkflowDefinitionCreate is required")
        await self._lock_authority(project_id, actor_user_id)
        now = _now()
        definition = WorkflowDefinitionRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name=command.name,
            description=command.description,
            status="active",
            revision=1,
            created_by=actor_user_id,
            updated_by=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        draft = WorkflowDraftRow(
            workflow_id=definition.id,
            project_id=project_id,
            revision=1,
            spec_schema_version=command.spec_schema_version,
            canvas_schema_version=command.canvas_schema_version,
            spec_json=command.materialize_spec(),
            canvas_json=command.materialize_canvas(),
            draft_checksum=command.draft_checksum,
            updated_by=actor_user_id,
            updated_at=now,
        )
        self.session.add_all((definition, draft))
        try:
            await self.session.flush()
        except sa.exc.IntegrityError:
            raise WorkflowDefinitionConflict from None
        return (
            self._definition_record(
                definition,
                draft_revision=draft.revision,
                draft_checksum=draft.draft_checksum,
            ),
            self._draft_record(draft),
        )

    async def list_definitions(
        self,
        project_id: uuid.UUID,
        query: WorkflowDefinitionListQuery,
    ) -> WorkflowDefinitionPage:
        project_id = _uuid(project_id, field_name="project_id")
        if type(query) is not WorkflowDefinitionListQuery:
            raise TypeError("WorkflowDefinitionListQuery is required")
        fingerprint = _cursor_fingerprint(
            project_id=project_id,
            query=query.query,
            lifecycle=query.lifecycle,
            publication=query.publication,
            sort=query.sort,
        )
        name_key = sa.func.lower(WorkflowDefinitionRow.name).label("name_key")
        statement = (
            sa.select(
                WorkflowDefinitionRow,
                name_key,
                WorkflowDraftRow.revision,
                WorkflowDraftRow.draft_checksum,
                WorkflowVersionRow.version_number,
            )
            .join(
                WorkflowDraftRow,
                sa.and_(
                    WorkflowDraftRow.workflow_id == WorkflowDefinitionRow.id,
                    WorkflowDraftRow.project_id == WorkflowDefinitionRow.project_id,
                ),
            )
            .outerjoin(
                WorkflowVersionRow,
                sa.and_(
                    WorkflowVersionRow.id == WorkflowDefinitionRow.current_published_version_id,
                    WorkflowVersionRow.workflow_id == WorkflowDefinitionRow.id,
                    WorkflowVersionRow.project_id == WorkflowDefinitionRow.project_id,
                ),
            )
            .where(
                WorkflowDefinitionRow.project_id == project_id,
                WorkflowDefinitionRow.status == query.lifecycle,
            )
        )
        if query.publication == "draft_only":
            statement = statement.where(WorkflowDefinitionRow.current_published_version_id.is_(None))
        elif query.publication == "published":
            statement = statement.where(WorkflowDefinitionRow.current_published_version_id.is_not(None))
        if query.query is not None:
            escaped = query.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                sa.or_(
                    WorkflowDefinitionRow.name.ilike(pattern, escape="\\"),
                    WorkflowDefinitionRow.description.ilike(
                        pattern,
                        escape="\\",
                    ),
                )
            )
        cursor_key: str | datetime | None = None
        cursor_id: uuid.UUID | None = None
        if query.cursor is not None:
            payload = _decode_cursor(query.cursor)
            if set(payload) != {"f", "id", "k", "s", "v"}:
                raise ValueError("cursor is invalid")
            if payload.get("v") != _DEFINITION_CURSOR_VERSION or payload.get("f") != fingerprint or payload.get("s") != query.sort:
                raise ValueError("cursor does not match the Workflow list query")
            cursor_id = _uuid(payload.get("id"), field_name="cursor.id")
            raw_key = payload.get("k")
            if query.sort == "updated_desc":
                if not isinstance(raw_key, str):
                    raise ValueError("cursor is invalid")
                try:
                    cursor_key = datetime.fromisoformat(raw_key)
                except ValueError:
                    raise ValueError("cursor is invalid") from None
                if cursor_key.tzinfo is None:
                    raise ValueError("cursor is invalid")
                statement = statement.where(
                    sa.or_(
                        WorkflowDefinitionRow.updated_at < cursor_key,
                        sa.and_(
                            WorkflowDefinitionRow.updated_at == cursor_key,
                            WorkflowDefinitionRow.id < cursor_id,
                        ),
                    )
                )
            else:
                if not isinstance(raw_key, str) or len(raw_key) > 255:
                    raise ValueError("cursor is invalid")
                cursor_key = raw_key
                comparator = name_key > cursor_key if query.sort == "name_asc" else name_key < cursor_key
                id_comparator = WorkflowDefinitionRow.id > cursor_id if query.sort == "name_asc" else WorkflowDefinitionRow.id < cursor_id
                statement = statement.where(
                    sa.or_(
                        comparator,
                        sa.and_(name_key == cursor_key, id_comparator),
                    )
                )
        if query.sort == "updated_desc":
            statement = statement.order_by(
                WorkflowDefinitionRow.updated_at.desc(),
                WorkflowDefinitionRow.id.desc(),
            )
        elif query.sort == "name_asc":
            statement = statement.order_by(
                name_key.asc(),
                WorkflowDefinitionRow.id.asc(),
            )
        else:
            statement = statement.order_by(
                name_key.desc(),
                WorkflowDefinitionRow.id.desc(),
            )
        rows = (await self.session.execute(statement.limit(query.limit + 1))).all()
        has_more = len(rows) > query.limit
        page_rows = rows[: query.limit]
        next_cursor = None
        if has_more and page_rows:
            last_definition = page_rows[-1][0]
            last_name_key = page_rows[-1][1]
            key = last_definition.updated_at.isoformat() if query.sort == "updated_desc" else last_name_key
            next_cursor = _encode_cursor(
                {
                    "f": fingerprint,
                    "id": str(last_definition.id),
                    "k": key,
                    "s": query.sort,
                    "v": _DEFINITION_CURSOR_VERSION,
                }
            )
        return WorkflowDefinitionPage(
            items=tuple(
                self._definition_record(
                    row[0],
                    draft_revision=row[2],
                    draft_checksum=row[3],
                    current_published_version_number=row[4],
                )
                for row in page_rows
            ),
            next_cursor=next_cursor,
        )

    async def update_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDefinitionUpdate,
    ) -> WorkflowDefinitionRecord:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        if type(command) is not WorkflowDefinitionUpdate:
            raise TypeError("WorkflowDefinitionUpdate is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                    WorkflowDefinitionRow.status == "active",
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        if definition.revision != command.expected_revision:
            raise WorkflowDefinitionConflict
        if command.name is not None:
            definition.name = command.name
        if command.description is not None:
            definition.description = command.description
        definition.revision += 1
        definition.updated_by = actor_user_id
        definition.updated_at = _now()
        try:
            await self.session.flush()
        except sa.exc.IntegrityError:
            raise WorkflowDefinitionConflict from None
        await self.session.refresh(definition)
        record = await self.get_definition(project_id, workflow_id)
        if record is None:  # guarded by the locked row
            raise WorkflowAuthorityMissing
        return record

    async def archive_definition(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDefinitionArchive,
    ) -> WorkflowDefinitionRecord:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        if type(command) is not WorkflowDefinitionArchive:
            raise TypeError("WorkflowDefinitionArchive is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        if definition.revision != command.expected_revision:
            raise WorkflowDefinitionConflict
        if definition.status == "archived":
            record = await self.get_definition(project_id, workflow_id)
            if record is None:  # guarded by the locked row
                raise WorkflowAuthorityMissing
            return record
        definition.status = "archived"
        definition.revision += 1
        definition.updated_by = actor_user_id
        definition.updated_at = _now()
        await self.session.flush()
        await self.session.refresh(definition)
        record = await self.get_definition(project_id, workflow_id)
        if record is None:  # guarded by the locked row
            raise WorkflowAuthorityMissing
        return record

    async def get_definition(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowDefinitionRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        statement = (
            sa.select(
                WorkflowDefinitionRow,
                WorkflowDraftRow.revision,
                WorkflowDraftRow.draft_checksum,
                WorkflowVersionRow.version_number,
            )
            .join(
                WorkflowDraftRow,
                sa.and_(
                    WorkflowDraftRow.workflow_id == WorkflowDefinitionRow.id,
                    WorkflowDraftRow.project_id == WorkflowDefinitionRow.project_id,
                ),
            )
            .outerjoin(
                WorkflowVersionRow,
                sa.and_(
                    WorkflowVersionRow.id == WorkflowDefinitionRow.current_published_version_id,
                    WorkflowVersionRow.workflow_id == WorkflowDefinitionRow.id,
                    WorkflowVersionRow.project_id == WorkflowDefinitionRow.project_id,
                ),
            )
            .where(
                WorkflowDefinitionRow.id == workflow_id,
                WorkflowDefinitionRow.project_id == project_id,
            )
        )
        if lock:
            statement = statement.with_for_update(of=WorkflowDefinitionRow)
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        return self._definition_record(
            row[0],
            draft_revision=row[1],
            draft_checksum=row[2],
            current_published_version_number=row[3],
        )

    async def get_draft(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowDraftRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        statement = sa.select(WorkflowDraftRow).where(
            WorkflowDraftRow.workflow_id == workflow_id,
            WorkflowDraftRow.project_id == project_id,
        )
        if lock:
            statement = statement.with_for_update(of=WorkflowDraftRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._draft_record(row)

    async def get_publish_replay(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        idempotency_hash: str,
        request_digest: str,
    ) -> WorkflowVersionRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        idempotency_hash = _checksum(
            idempotency_hash,
            field_name="idempotency_hash",
        )
        request_digest = _checksum(request_digest, field_name="request_digest")
        scope_key = canonical_workflow_control_scope_key(
            project_id=project_id,
            operation="publish",
            workflow_id=workflow_id,
        )
        operation = (
            await self.session.execute(
                sa.select(WorkflowControlOperationRow).where(
                    WorkflowControlOperationRow.project_id == project_id,
                    WorkflowControlOperationRow.workflow_id == workflow_id,
                    WorkflowControlOperationRow.operation == "publish",
                    WorkflowControlOperationRow.scope_key == scope_key,
                    WorkflowControlOperationRow.idempotency_hash == idempotency_hash,
                )
            )
        ).scalar_one_or_none()
        if operation is None:
            return None
        if operation.request_digest != request_digest:
            raise WorkflowPublishIdempotencyConflict
        version = (
            await self.session.execute(
                sa.select(WorkflowVersionRow).where(
                    WorkflowVersionRow.id == operation.result_version_id,
                    WorkflowVersionRow.workflow_id == workflow_id,
                    WorkflowVersionRow.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:  # guarded by the exact composite FK
            raise WorkflowAuthorityMissing
        return self._publish_replay_record(
            (await self._complete_version_records((version,)))[0],
            operation,
        )

    async def save_draft(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowDraftUpdate,
    ) -> WorkflowDraftRecord:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        if type(command) is not WorkflowDraftUpdate:
            raise TypeError("WorkflowDraftUpdate is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                    WorkflowDefinitionRow.status == "active",
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        draft = (
            await self.session.execute(
                sa.select(WorkflowDraftRow)
                .where(
                    WorkflowDraftRow.workflow_id == workflow_id,
                    WorkflowDraftRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDraftRow)
            )
        ).scalar_one_or_none()
        if draft is None:
            raise WorkflowAuthorityMissing
        if draft.revision != command.expected_revision:
            raise WorkflowDraftCASConflict
        intent_delete = sa.delete(WorkflowDraftCredentialGrantIntentRow).where(
            WorkflowDraftCredentialGrantIntentRow.workflow_id == workflow_id,
            WorkflowDraftCredentialGrantIntentRow.project_id == project_id,
        )
        if command.credential_slot_ids:
            intent_delete = intent_delete.where(WorkflowDraftCredentialGrantIntentRow.slot_id.not_in(command.credential_slot_ids))
        await self.session.execute(intent_delete)
        now = _now()
        draft.revision += 1
        draft.spec_schema_version = command.spec_schema_version
        draft.canvas_schema_version = command.canvas_schema_version
        draft.spec_json = command.materialize_spec()
        draft.canvas_json = command.materialize_canvas()
        draft.draft_checksum = command.draft_checksum
        draft.updated_by = actor_user_id
        draft.updated_at = now
        definition.revision += 1
        definition.updated_by = actor_user_id
        definition.updated_at = now
        await self.session.flush()
        await self.session.refresh(draft)
        return self._draft_record(draft)

    async def publish_version(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        command: WorkflowVersionPublish,
    ) -> WorkflowVersionPublishResult:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        if type(command) is not WorkflowVersionPublish:
            raise TypeError("WorkflowVersionPublish is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                    WorkflowDefinitionRow.status == "active",
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        if command.idempotency_hash is not None:
            scope_key = canonical_workflow_control_scope_key(
                project_id=project_id,
                operation="publish",
                workflow_id=workflow_id,
            )
            operation = (
                await self.session.execute(
                    sa.select(WorkflowControlOperationRow)
                    .where(
                        WorkflowControlOperationRow.project_id == project_id,
                        WorkflowControlOperationRow.workflow_id == workflow_id,
                        WorkflowControlOperationRow.operation == "publish",
                        WorkflowControlOperationRow.scope_key == scope_key,
                        WorkflowControlOperationRow.idempotency_hash == command.idempotency_hash,
                    )
                    .with_for_update(of=WorkflowControlOperationRow)
                )
            ).scalar_one_or_none()
            if operation is not None:
                if operation.request_digest != command.request_digest:
                    raise WorkflowPublishIdempotencyConflict
                replay_row = (
                    await self.session.execute(
                        sa.select(WorkflowVersionRow).where(
                            WorkflowVersionRow.id == operation.result_version_id,
                            WorkflowVersionRow.workflow_id == workflow_id,
                            WorkflowVersionRow.project_id == project_id,
                        )
                    )
                ).scalar_one_or_none()
                if replay_row is None:  # guarded by the exact composite FK
                    raise WorkflowAuthorityMissing
                return WorkflowVersionPublishResult(
                    record=self._publish_replay_record(
                        (await self._complete_version_records((replay_row,)))[0],
                        operation,
                    ),
                    created=False,
                )
        draft = (
            await self.session.execute(
                sa.select(WorkflowDraftRow)
                .where(
                    WorkflowDraftRow.workflow_id == workflow_id,
                    WorkflowDraftRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDraftRow)
            )
        ).scalar_one_or_none()
        if draft is None:
            raise WorkflowAuthorityMissing
        if draft.revision != command.expected_draft_revision or draft.draft_checksum != command.expected_draft_checksum:
            raise WorkflowDraftCASConflict
        if command.graph_schema_version != draft.spec_schema_version or command.canvas_schema_version != draft.canvas_schema_version:
            raise WorkflowDraftCASConflict
        grant_intents: dict[str, WorkflowDraftCredentialGrantIntentRow] = {}
        slot_ids = tuple(sorted(slot.slot_id for slot in command.credential_slots))
        if slot_ids:
            intent_rows = (
                await self.session.execute(
                    sa.select(WorkflowDraftCredentialGrantIntentRow)
                    .where(
                        WorkflowDraftCredentialGrantIntentRow.workflow_id == workflow_id,
                        WorkflowDraftCredentialGrantIntentRow.project_id == project_id,
                        WorkflowDraftCredentialGrantIntentRow.slot_id.in_(slot_ids),
                    )
                    .order_by(WorkflowDraftCredentialGrantIntentRow.slot_id)
                    .with_for_update(of=WorkflowDraftCredentialGrantIntentRow)
                )
            ).scalars()
            grant_intents = {row.slot_id: row for row in intent_rows}
        existing = (
            await self.session.execute(
                sa.select(WorkflowVersionRow).where(
                    WorkflowVersionRow.workflow_id == workflow_id,
                    WorkflowVersionRow.project_id == project_id,
                    WorkflowVersionRow.semantic_checksum == command.semantic_checksum,
                    WorkflowVersionRow.compiler_contract_version == command.compiler_contract_version,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if definition.current_published_version_id == existing.id:
                record = (await self._complete_version_records((existing,)))[0]
                if command.idempotency_hash is not None:
                    self.session.add(
                        WorkflowControlOperationRow(
                            project_id=project_id,
                            workflow_id=workflow_id,
                            operation="publish",
                            scope_key=canonical_workflow_control_scope_key(
                                project_id=project_id,
                                operation="publish",
                                workflow_id=workflow_id,
                            ),
                            idempotency_hash=command.idempotency_hash,
                            request_digest=command.request_digest,
                            result_version_id=existing.id,
                            result_missing_slot_ids_csv=(_encode_workflow_control_missing_slot_ids(record.missing_required_slot_ids)),
                            created_by=actor_user_id,
                        )
                    )
                    await self.session.flush()
                return WorkflowVersionPublishResult(
                    record=record,
                    created=False,
                )
            raise WorkflowDraftCASConflict
        version_number = (
            await self.session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(WorkflowVersionRow.version_number), 0)).where(
                    WorkflowVersionRow.workflow_id == workflow_id,
                    WorkflowVersionRow.project_id == project_id,
                )
            )
        ) + 1
        now = _now()
        version = WorkflowVersionRow(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            project_id=project_id,
            version_number=version_number,
            graph_schema_version=command.graph_schema_version,
            canvas_schema_version=command.canvas_schema_version,
            compiler_contract_version=command.compiler_contract_version,
            spec_json=thaw_json(freeze_json(draft.spec_json)),
            canvas_json=thaw_json(freeze_json(draft.canvas_json)),
            semantic_checksum=command.semantic_checksum,
            published_by=actor_user_id,
            published_at=now,
        )
        self.session.add(version)
        # These child rows intentionally have no ORM relationship to infer
        # insert ordering from.  Establish the immutable Version parent first;
        # the caller-owned transaction still rolls everything back together.
        await self.session.flush()
        self.session.add_all(
            WorkflowVersionModelRefRow(
                workflow_version_id=version.id,
                project_id=project_id,
                node_id=reference.node_id,
                logical_model_name=reference.logical_model_name,
                purpose=reference.purpose,
            )
            for reference in command.model_refs
        )
        self.session.add_all(
            WorkflowVersionCredentialSlotRow(
                workflow_version_id=version.id,
                project_id=project_id,
                slot_id=slot.slot_id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema_json=slot.materialize_payload_schema(),
                payload_schema_checksum=slot.payload_schema_checksum,
                required=True,
            )
            for slot in command.credential_slots
        )
        await self.session.flush()
        self.session.add_all(
            WorkflowVersionCodeRequirementRow(
                workflow_version_id=version.id,
                project_id=project_id,
                node_id=requirement.node_id,
                runtime_contract=requirement.runtime_contract,
            )
            for requirement in command.code_requirements
        )
        self.session.add_all(
            WorkflowVersionHttpRequirementRow(
                workflow_version_id=version.id,
                project_id=project_id,
                node_id=requirement.node_id,
                method=requirement.method,
                endpoint_policy_id=requirement.endpoint_policy_id,
                injection_profile_id=requirement.injection_profile_id,
                credential_slot_id=requirement.credential_slot_id,
            )
            for requirement in command.http_requirements
        )
        await self.session.flush()
        locked_slots = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionCredentialSlotRow)
                    .where(
                        WorkflowVersionCredentialSlotRow.workflow_version_id == version.id,
                        WorkflowVersionCredentialSlotRow.project_id == project_id,
                    )
                    .order_by(WorkflowVersionCredentialSlotRow.slot_id)
                    .with_for_update(of=WorkflowVersionCredentialSlotRow)
                )
            ).scalars()
        )
        for slot in locked_slots:
            intent = grant_intents.get(slot.slot_id)
            if intent is None or intent.slot_schema_checksum != slot.payload_schema_checksum:
                continue
            credential = await self._lock_project_credential(
                project_id=project_id,
                credential_id=intent.credential_id,
                credential_version_id=intent.expected_credential_version_id,
                require_current_active=True,
                stale_is_missing=True,
            )
            if credential is None:
                continue
            self.session.add(
                WorkflowCredentialGrantRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    workflow_version_id=version.id,
                    slot_id=slot.slot_id,
                    credential_scope="project",
                    credential_id=intent.credential_id,
                    credential_version_id=intent.expected_credential_version_id,
                    payload_schema_checksum=slot.payload_schema_checksum,
                    status="active",
                    revision=1,
                    granted_by=actor_user_id,
                    created_at=now,
                )
            )
        definition.current_published_version_id = version.id
        definition.revision += 1
        definition.updated_by = actor_user_id
        definition.updated_at = now
        await self.session.flush()
        record = (await self._complete_version_records((version,)))[0]
        if command.idempotency_hash is not None:
            self.session.add(
                WorkflowControlOperationRow(
                    project_id=project_id,
                    workflow_id=workflow_id,
                    operation="publish",
                    scope_key=canonical_workflow_control_scope_key(
                        project_id=project_id,
                        operation="publish",
                        workflow_id=workflow_id,
                    ),
                    idempotency_hash=command.idempotency_hash,
                    request_digest=command.request_digest,
                    result_version_id=version.id,
                    result_missing_slot_ids_csv=(_encode_workflow_control_missing_slot_ids(record.missing_required_slot_ids)),
                    created_by=actor_user_id,
                    created_at=now,
                )
            )
        await self.session.flush()
        return WorkflowVersionPublishResult(
            record=record,
            created=True,
        )

    async def list_versions(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
    ) -> tuple[WorkflowVersionRecord, ...]:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        rows = tuple(
            (
                await self.session.execute(
                    sa.select(WorkflowVersionRow)
                    .where(
                        WorkflowVersionRow.project_id == project_id,
                        WorkflowVersionRow.workflow_id == workflow_id,
                    )
                    .order_by(WorkflowVersionRow.version_number)
                )
            ).scalars()
        )
        return await self._complete_version_records(rows)

    async def get_version(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowVersionRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        version_id = _uuid(version_id, field_name="version_id")
        statement = sa.select(WorkflowVersionRow).where(
            WorkflowVersionRow.id == version_id,
            WorkflowVersionRow.workflow_id == workflow_id,
            WorkflowVersionRow.project_id == project_id,
        )
        if lock:
            statement = statement.with_for_update(of=WorkflowVersionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            return None
        return (await self._complete_version_records((row,)))[0]

    async def list_version_history(
        self,
        project_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> WorkflowVersionPage:
        project_id = _uuid(project_id, field_name="project_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        definition_id = await self.session.scalar(
            sa.select(WorkflowDefinitionRow.id).where(
                WorkflowDefinitionRow.id == workflow_id,
                WorkflowDefinitionRow.project_id == project_id,
            )
        )
        if definition_id is None:
            raise WorkflowAuthorityMissing
        cursor = _bounded_cursor(cursor)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Workflow version history limit must be between 1 and 100")
        fingerprint = hashlib.sha256(f"{project_id}:workflow-version-history:{workflow_id}".encode("ascii")).hexdigest()
        statement = sa.select(WorkflowVersionRow).where(
            WorkflowVersionRow.project_id == project_id,
            WorkflowVersionRow.workflow_id == workflow_id,
        )
        if cursor is not None:
            payload = _decode_cursor(cursor)
            if set(payload) != {"f", "id", "n", "v"}:
                raise ValueError("cursor is invalid")
            if payload.get("v") != _VERSION_CURSOR_VERSION or payload.get("f") != fingerprint:
                raise ValueError("cursor does not match the Workflow version history")
            version_number = payload.get("n")
            if type(version_number) is not int or version_number < 1:
                raise ValueError("cursor is invalid")
            version_id = _uuid(payload.get("id"), field_name="cursor.id")
            statement = statement.where(
                sa.or_(
                    WorkflowVersionRow.version_number < version_number,
                    sa.and_(
                        WorkflowVersionRow.version_number == version_number,
                        WorkflowVersionRow.id < version_id,
                    ),
                )
            )
        rows = tuple(
            (
                await self.session.execute(
                    statement.order_by(
                        WorkflowVersionRow.version_number.desc(),
                        WorkflowVersionRow.id.desc(),
                    ).limit(limit + 1)
                )
            ).scalars()
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = _encode_cursor(
                {
                    "f": fingerprint,
                    "id": str(last.id),
                    "n": last.version_number,
                    "v": _VERSION_CURSOR_VERSION,
                }
            )
        return WorkflowVersionPage(
            items=await self._complete_version_records(page_rows),
            next_cursor=next_cursor,
        )

    async def put_draft_grant_intent(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        slot_id: str,
        resolved_draft_revision: int,
        command: WorkflowCredentialGrantPut,
    ) -> WorkflowDraftCredentialGrantIntentRecord:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        slot_id = _slot(slot_id)
        resolved_draft_revision = _positive(
            resolved_draft_revision,
            field_name="resolved_draft_revision",
        )
        if type(command) is not WorkflowCredentialGrantPut:
            raise TypeError("WorkflowCredentialGrantPut is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                    WorkflowDefinitionRow.status == "active",
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        draft = (
            await self.session.execute(
                sa.select(WorkflowDraftRow)
                .where(
                    WorkflowDraftRow.workflow_id == workflow_id,
                    WorkflowDraftRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDraftRow)
            )
        ).scalar_one_or_none()
        if draft is None:
            raise WorkflowAuthorityMissing
        if draft.revision != resolved_draft_revision:
            raise WorkflowDraftCASConflict
        intent = (
            await self.session.execute(
                sa.select(WorkflowDraftCredentialGrantIntentRow)
                .where(
                    WorkflowDraftCredentialGrantIntentRow.workflow_id == workflow_id,
                    WorkflowDraftCredentialGrantIntentRow.project_id == project_id,
                    WorkflowDraftCredentialGrantIntentRow.slot_id == slot_id,
                )
                .with_for_update(of=WorkflowDraftCredentialGrantIntentRow)
            )
        ).scalar_one_or_none()
        await self._lock_project_credential(
            project_id=project_id,
            credential_id=command.credential_id,
            credential_version_id=command.expected_credential_version_id,
            require_current_active=True,
        )
        now = _now()
        if intent is None:
            intent = WorkflowDraftCredentialGrantIntentRow(
                workflow_id=workflow_id,
                project_id=project_id,
                slot_id=slot_id,
                slot_schema_checksum=command.resolved_slot_schema_checksum,
                credential_scope="project",
                credential_id=command.credential_id,
                expected_credential_version_id=command.expected_credential_version_id,
                updated_by=actor_user_id,
                updated_at=now,
            )
            self.session.add(intent)
        else:
            intent.slot_schema_checksum = command.resolved_slot_schema_checksum
            intent.credential_id = command.credential_id
            intent.expected_credential_version_id = command.expected_credential_version_id
            intent.updated_by = actor_user_id
            intent.updated_at = now
        await self.session.flush()
        await self.session.refresh(intent)
        return self._draft_grant_intent_record(intent)

    async def delete_draft_grant_intent(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        slot_id: str,
        resolved_draft_revision: int,
    ) -> WorkflowDraftCredentialGrantIntentRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        slot_id = _slot(slot_id)
        resolved_draft_revision = _positive(
            resolved_draft_revision,
            field_name="resolved_draft_revision",
        )
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                    WorkflowDefinitionRow.status == "active",
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        draft = (
            await self.session.execute(
                sa.select(WorkflowDraftRow)
                .where(
                    WorkflowDraftRow.workflow_id == workflow_id,
                    WorkflowDraftRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDraftRow)
            )
        ).scalar_one_or_none()
        if draft is None:
            raise WorkflowAuthorityMissing
        if draft.revision != resolved_draft_revision:
            raise WorkflowDraftCASConflict
        intent = (
            await self.session.execute(
                sa.select(WorkflowDraftCredentialGrantIntentRow)
                .where(
                    WorkflowDraftCredentialGrantIntentRow.workflow_id == workflow_id,
                    WorkflowDraftCredentialGrantIntentRow.project_id == project_id,
                    WorkflowDraftCredentialGrantIntentRow.slot_id == slot_id,
                )
                .with_for_update(of=WorkflowDraftCredentialGrantIntentRow)
            )
        ).scalar_one_or_none()
        if intent is None:
            return None
        await self._lock_project_credential(
            project_id=project_id,
            credential_id=intent.credential_id,
            credential_version_id=intent.expected_credential_version_id,
            require_current_active=False,
        )
        record = self._draft_grant_intent_record(intent)
        await self.session.delete(intent)
        await self.session.flush()
        return record

    async def put_version_grant(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
        command: WorkflowCredentialGrantPut,
    ) -> WorkflowCredentialGrantRecord:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        version_id = _uuid(version_id, field_name="version_id")
        slot_id = _slot(slot_id)
        if type(command) is not WorkflowCredentialGrantPut:
            raise TypeError("WorkflowCredentialGrantPut is required")
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        version = (
            await self.session.execute(
                sa.select(WorkflowVersionRow)
                .where(
                    WorkflowVersionRow.id == version_id,
                    WorkflowVersionRow.workflow_id == workflow_id,
                    WorkflowVersionRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowVersionRow)
            )
        ).scalar_one_or_none()
        if version is None:
            raise WorkflowAuthorityMissing
        slot = (
            await self.session.execute(
                sa.select(WorkflowVersionCredentialSlotRow)
                .where(
                    WorkflowVersionCredentialSlotRow.workflow_version_id == version_id,
                    WorkflowVersionCredentialSlotRow.project_id == project_id,
                    WorkflowVersionCredentialSlotRow.slot_id == slot_id,
                )
                .with_for_update(of=WorkflowVersionCredentialSlotRow)
            )
        ).scalar_one_or_none()
        if slot is None:
            raise WorkflowAuthorityMissing
        if slot.payload_schema_checksum != command.resolved_slot_schema_checksum:
            raise WorkflowCredentialGrantConflict
        active_grant = (
            await self.session.execute(
                sa.select(WorkflowCredentialGrantRow)
                .where(
                    WorkflowCredentialGrantRow.project_id == project_id,
                    WorkflowCredentialGrantRow.workflow_version_id == version_id,
                    WorkflowCredentialGrantRow.slot_id == slot_id,
                    WorkflowCredentialGrantRow.status == "active",
                )
                .with_for_update(of=WorkflowCredentialGrantRow)
            )
        ).scalar_one_or_none()
        await self._lock_project_credential(
            project_id=project_id,
            credential_id=command.credential_id,
            credential_version_id=command.expected_credential_version_id,
            require_current_active=True,
        )
        if (
            active_grant is not None
            and active_grant.credential_id == command.credential_id
            and active_grant.credential_version_id == command.expected_credential_version_id
            and active_grant.payload_schema_checksum == command.resolved_slot_schema_checksum
        ):
            return self._credential_grant_record(
                active_grant,
                workflow_id=workflow_id,
            )
        now = _now()
        if active_grant is not None:
            active_grant.status = "revoked"
            active_grant.revision += 1
            active_grant.revoked_by = actor_user_id
            active_grant.revoked_at = now
            await self.session.flush()
        grant = WorkflowCredentialGrantRow(
            id=uuid.uuid4(),
            project_id=project_id,
            workflow_version_id=version_id,
            slot_id=slot_id,
            credential_scope="project",
            credential_id=command.credential_id,
            credential_version_id=command.expected_credential_version_id,
            payload_schema_checksum=command.resolved_slot_schema_checksum,
            status="active",
            revision=1,
            granted_by=actor_user_id,
            created_at=now,
        )
        self.session.add(grant)
        await self.session.flush()
        return self._credential_grant_record(grant, workflow_id=workflow_id)

    async def revoke_version_grant(
        self,
        *,
        project_id: uuid.UUID,
        actor_user_id: str,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        slot_id: str,
    ) -> WorkflowCredentialGrantRecord | None:
        project_id = _uuid(project_id, field_name="project_id")
        actor_user_id = _user_id(actor_user_id, field_name="actor_user_id")
        workflow_id = _uuid(workflow_id, field_name="workflow_id")
        version_id = _uuid(version_id, field_name="version_id")
        slot_id = _slot(slot_id)
        await self._lock_authority(project_id, actor_user_id)
        definition = (
            await self.session.execute(
                sa.select(WorkflowDefinitionRow)
                .where(
                    WorkflowDefinitionRow.id == workflow_id,
                    WorkflowDefinitionRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowDefinitionRow)
            )
        ).scalar_one_or_none()
        if definition is None:
            raise WorkflowAuthorityMissing
        version = (
            await self.session.execute(
                sa.select(WorkflowVersionRow)
                .where(
                    WorkflowVersionRow.id == version_id,
                    WorkflowVersionRow.workflow_id == workflow_id,
                    WorkflowVersionRow.project_id == project_id,
                )
                .with_for_update(of=WorkflowVersionRow)
            )
        ).scalar_one_or_none()
        if version is None:
            raise WorkflowAuthorityMissing
        slot = (
            await self.session.execute(
                sa.select(WorkflowVersionCredentialSlotRow)
                .where(
                    WorkflowVersionCredentialSlotRow.workflow_version_id == version_id,
                    WorkflowVersionCredentialSlotRow.project_id == project_id,
                    WorkflowVersionCredentialSlotRow.slot_id == slot_id,
                )
                .with_for_update(of=WorkflowVersionCredentialSlotRow)
            )
        ).scalar_one_or_none()
        if slot is None:
            raise WorkflowAuthorityMissing
        grant = (
            await self.session.execute(
                sa.select(WorkflowCredentialGrantRow)
                .where(
                    WorkflowCredentialGrantRow.project_id == project_id,
                    WorkflowCredentialGrantRow.workflow_version_id == version_id,
                    WorkflowCredentialGrantRow.slot_id == slot_id,
                    WorkflowCredentialGrantRow.status == "active",
                )
                .with_for_update(of=WorkflowCredentialGrantRow)
            )
        ).scalar_one_or_none()
        if grant is None:
            return None
        await self._lock_project_credential(
            project_id=project_id,
            credential_id=grant.credential_id,
            credential_version_id=grant.credential_version_id,
            require_current_active=False,
        )
        grant.status = "revoked"
        grant.revision += 1
        grant.revoked_by = actor_user_id
        grant.revoked_at = _now()
        await self.session.flush()
        return self._credential_grant_record(grant, workflow_id=workflow_id)

    @staticmethod
    def _run_predicates(
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            WorkflowRunRow.id == run_id,
            WorkflowRunRow.project_id == scope.project_id,
            WorkflowRunRow.owner_user_id == scope.owner_user_id,
        )

    async def stage_run(
        self,
        scope: WorkflowRunScope,
        command: WorkflowRunCreate,
    ) -> WorkflowRunStage:
        if type(scope) is not WorkflowRunScope:
            raise TypeError("WorkflowRunScope is required")
        if type(command) is not WorkflowRunCreate:
            raise TypeError("WorkflowRunCreate is required")
        if command.requested_workflow_version_id is not None and command.requested_workflow_version_id != command.workflow_version_id:
            raise ValueError("requested_workflow_version_id must match workflow_version_id")
        await self._lock_scope_authority(scope)
        existing = (
            await self.session.execute(
                sa.select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.project_id == scope.project_id,
                    WorkflowRunRow.owner_user_id == scope.owner_user_id,
                    WorkflowRunRow.workflow_id == command.workflow_id,
                    WorkflowRunRow.idempotency_hash == command.idempotency_hash,
                )
                .with_for_update(of=WorkflowRunRow)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not self._same_run_request(existing, command):
                raise WorkflowRunIdempotencyConflict
            return WorkflowRunStage(record=self._run_record(existing), created=False)
        if command.retry_of_run_id is not None:
            source = (await self.session.execute(sa.select(WorkflowRunRow).where(*self._run_predicates(scope, command.retry_of_run_id)).with_for_update(of=WorkflowRunRow))).scalar_one_or_none()
            if source is None:
                raise WorkflowAuthorityMissing
            if source.status == "side_effect_unknown":
                raise WorkflowManualRetryForbidden
            if source.status not in {"succeeded", "failed", "cancelled"}:
                raise WorkflowRunStateConflict
            if (
                source.workflow_id != command.workflow_id
                or source.workflow_version_id != command.workflow_version_id
                or source.input_json != command.materialize_inputs()
                or source.input_digest != command.input_digest
                or source.required_worker_profile_digest != command.required_worker_profile_digest
                or source.origin_trace_id == command.origin_trace_id
            ):
                raise WorkflowRunStateConflict
        definition_exists = await self.session.scalar(
            sa.select(WorkflowDefinitionRow.id)
            .where(
                WorkflowDefinitionRow.id == command.workflow_id,
                WorkflowDefinitionRow.project_id == scope.project_id,
                WorkflowDefinitionRow.status == "active",
            )
            .with_for_update(of=WorkflowDefinitionRow)
        )
        if definition_exists is None:
            raise WorkflowAuthorityMissing
        version_exists = await self.session.scalar(
            sa.select(WorkflowVersionRow.id).where(
                WorkflowVersionRow.id == command.workflow_version_id,
                WorkflowVersionRow.workflow_id == command.workflow_id,
                WorkflowVersionRow.project_id == scope.project_id,
            )
        )
        if version_exists is None:
            raise WorkflowAuthorityMissing
        run_id = uuid.uuid4()
        now = _now()
        profile_key = command.required_worker_profile_digest or _EMPTY_PROFILE_KEY
        inserted_id = await self.session.scalar(
            pg_insert(WorkflowRunRow)
            .values(
                id=run_id,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                workflow_id=command.workflow_id,
                workflow_version_id=command.workflow_version_id,
                status="queued",
                input_json=command.materialize_inputs(),
                # PostgreSQL JSONB otherwise serializes Python ``None`` as a
                # JSON ``null`` scalar, which is not SQL NULL and violates the
                # queued Run lifecycle shape.
                output_json=sa.null(),
                input_digest=command.input_digest,
                idempotency_hash=command.idempotency_hash,
                admission_request_digest=command.admission_request_digest,
                trigger_kind=command.trigger_kind,
                trigger_ref=command.trigger_ref,
                origin_trace_id=command.origin_trace_id,
                required_worker_profile_digest=command.required_worker_profile_digest,
                worker_profile_key=profile_key,
                execution_epoch=1,
                current_job_id=None,
                retry_of_run_id=command.retry_of_run_id,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    WorkflowRunRow.project_id,
                    WorkflowRunRow.owner_user_id,
                    WorkflowRunRow.workflow_id,
                    WorkflowRunRow.idempotency_hash,
                ]
            )
            .returning(WorkflowRunRow.id)
        )
        if inserted_id is not None:
            row = (await self.session.execute(sa.select(WorkflowRunRow).where(*self._run_predicates(scope, inserted_id)))).scalar_one()
            return WorkflowRunStage(record=self._run_record(row), created=True)
        existing = (
            await self.session.execute(
                sa.select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.project_id == scope.project_id,
                    WorkflowRunRow.owner_user_id == scope.owner_user_id,
                    WorkflowRunRow.workflow_id == command.workflow_id,
                    WorkflowRunRow.idempotency_hash == command.idempotency_hash,
                )
                .with_for_update(of=WorkflowRunRow)
            )
        ).scalar_one()
        if not self._same_run_request(existing, command):
            raise WorkflowRunIdempotencyConflict
        return WorkflowRunStage(record=self._run_record(existing), created=False)

    @staticmethod
    def _same_run_request(
        row: WorkflowRunRow,
        command: WorkflowRunCreate,
    ) -> bool:
        return row.workflow_id == command.workflow_id and row.admission_request_digest == command.admission_request_digest

    async def get_run(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> WorkflowRunRecord | None:
        if type(scope) is not WorkflowRunScope:
            raise TypeError("WorkflowRunScope is required")
        run_id = _uuid(run_id, field_name="run_id")
        statement = sa.select(WorkflowRunRow).where(*self._run_predicates(scope, run_id))
        if lock:
            statement = statement.with_for_update(of=WorkflowRunRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._run_record(row)

    async def _locked_run(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
    ) -> WorkflowRunRow:
        row = (await self.session.execute(sa.select(WorkflowRunRow).where(*self._run_predicates(scope, run_id)).with_for_update(of=WorkflowRunRow))).scalar_one_or_none()
        if row is None:
            raise WorkflowAuthorityMissing
        return row

    async def attach_initial_job(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> WorkflowRunRecord:
        run_id = _uuid(run_id, field_name="run_id")
        job_id = _uuid(job_id, field_name="job_id")
        await self._lock_scope_authority(scope)
        run = await self._locked_run(scope, run_id)
        existing = await self.session.scalar(
            sa.select(WorkflowRunJobRow.job_id).where(
                WorkflowRunJobRow.workflow_run_id == run_id,
                WorkflowRunJobRow.execution_epoch == 1,
            )
        )
        if existing is not None:
            if existing != job_id or run.current_job_id != job_id:
                raise WorkflowRunStateConflict
            return self._run_record(run)
        if run.status != "queued" or run.execution_epoch != 1 or run.current_job_id is not None:
            raise WorkflowRunStateConflict
        job = await self._matching_job(scope, run, job_id, execution_epoch=1)
        if job is None or job.status != "queued":
            raise WorkflowRunStateConflict
        self.session.add(
            WorkflowRunJobRow(
                workflow_run_id=run.id,
                execution_epoch=1,
                job_id=job.id,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                worker_profile_key=run.worker_profile_key,
                cause="initial",
                created_at=_now(),
            )
        )
        run.current_job_id = job.id
        run.updated_at = _now()
        await self.session.flush()
        await self.session.refresh(run)
        return self._run_record(run)

    async def attach_resume_job(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        expected_execution_epoch: int,
    ) -> WorkflowRunRecord:
        run_id = _uuid(run_id, field_name="run_id")
        job_id = _uuid(job_id, field_name="job_id")
        expected_execution_epoch = _positive(
            expected_execution_epoch,
            field_name="expected_execution_epoch",
        )
        await self._lock_scope_authority(scope)
        run = await self._locked_run(scope, run_id)
        if run.status not in _ACTIVE_RUN_STATUSES or run.execution_epoch != expected_execution_epoch or run.current_job_id is None:
            raise WorkflowRunStateConflict
        previous_job_status = await self.session.scalar(
            sa.select(JobRow.status).where(
                JobRow.id == run.current_job_id,
                JobRow.project_id == scope.project_id,
                JobRow.owner_user_id == scope.owner_user_id,
                JobRow.workflow_run_id == run.id,
                JobRow.workflow_epoch == run.execution_epoch,
            )
        )
        if previous_job_status not in _TERMINAL_JOB_STATUSES:
            raise WorkflowRunStateConflict
        target_epoch = expected_execution_epoch + 1
        job = await self._matching_job(
            scope,
            run,
            job_id,
            execution_epoch=target_epoch,
        )
        if job is None or job.status != "queued":
            raise WorkflowRunStateConflict
        self.session.add(
            WorkflowRunJobRow(
                workflow_run_id=run.id,
                execution_epoch=target_epoch,
                job_id=job.id,
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                worker_profile_key=run.worker_profile_key,
                cause="resume",
                created_at=_now(),
            )
        )
        run.execution_epoch = target_epoch
        run.current_job_id = job.id
        run.updated_at = _now()
        await self.session.flush()
        await self.session.refresh(run)
        return self._run_record(run)

    async def _matching_job(
        self,
        scope: WorkflowRunScope,
        run: WorkflowRunRow,
        job_id: uuid.UUID,
        *,
        execution_epoch: int,
    ) -> JobRow | None:
        return (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.job_type == "workflow_run",
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.workflow_run_id == run.id,
                    JobRow.workflow_epoch == execution_epoch,
                    JobRow.workflow_profile_key == run.worker_profile_key,
                    JobRow.origin_trace_id == run.origin_trace_id,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()

    async def list_epoch_jobs(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
    ) -> tuple[WorkflowRunJobRecord, ...]:
        run_id = _uuid(run_id, field_name="run_id")
        rows = (
            await self.session.execute(
                sa.select(WorkflowRunJobRow, JobRow.status)
                .join(JobRow, JobRow.id == WorkflowRunJobRow.job_id)
                .where(
                    WorkflowRunJobRow.workflow_run_id == run_id,
                    WorkflowRunJobRow.project_id == scope.project_id,
                    WorkflowRunJobRow.owner_user_id == scope.owner_user_id,
                )
                .order_by(WorkflowRunJobRow.execution_epoch)
            )
        ).all()
        return tuple(
            WorkflowRunJobRecord(
                run_id=mapping.workflow_run_id,
                execution_epoch=mapping.execution_epoch,
                job_id=mapping.job_id,
                project_id=mapping.project_id,
                owner_user_id=mapping.owner_user_id,
                worker_profile_key=mapping.worker_profile_key,
                cause=mapping.cause,
                job_status=job_status,
                created_at=mapping.created_at,
            )
            for mapping, job_status in rows
        )

    async def settle_queued_cancel(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
        *,
        completed_at: datetime | None = None,
    ) -> WorkflowRunRecord:
        run_id = _uuid(run_id, field_name="run_id")
        await self._lock_scope_authority(scope)
        run = await self._locked_run(scope, run_id)
        if run.status == "cancelled":
            return self._run_record(run)
        if run.status != "queued" or run.current_job_id is None:
            raise WorkflowRunStateConflict
        job_status = await self.session.scalar(
            sa.select(JobRow.status).where(
                JobRow.id == run.current_job_id,
                JobRow.job_type == "workflow_run",
                JobRow.project_id == scope.project_id,
                JobRow.owner_user_id == scope.owner_user_id,
                JobRow.workflow_run_id == run.id,
                JobRow.workflow_epoch == run.execution_epoch,
            )
        )
        if job_status != "cancelled":
            raise WorkflowRunStateConflict
        settled_at = _now(completed_at)
        run.status = "cancelled"
        run.current_job_id = None
        run.completed_at = settled_at
        run.updated_at = settled_at
        await self.session.flush()
        await self.session.refresh(run)
        return self._run_record(run)

    async def prepare_manual_retry(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
    ) -> WorkflowRetrySource:
        run_id = _uuid(run_id, field_name="run_id")
        await self._lock_scope_authority(scope)
        run = await self._locked_run(scope, run_id)
        if run.status == "side_effect_unknown":
            raise WorkflowManualRetryForbidden
        if run.status not in {"succeeded", "failed", "cancelled"}:
            raise WorkflowRunStateConflict
        return WorkflowRetrySource(
            source_run_id=run.id,
            workflow_id=run.workflow_id,
            workflow_version_id=run.workflow_version_id,
            inputs=dict(run.input_json),
            input_digest=run.input_digest,
            required_worker_profile_digest=run.required_worker_profile_digest,
            source_origin_trace_id=run.origin_trace_id,
        )

    async def append_execution_event(
        self,
        fence: WorkflowExecutionFence,
        event: WorkflowRunEventAppend,
    ) -> WorkflowRunEventRecord:
        """Append a non-terminal Worker event under the current raw lease.

        Terminal Worker settlement will be added with the G32 executor so its
        Job, Run, event, and checkpoint ordering can be one transaction.  G13
        does not expose a terminal-event-only operation that could brick replay.
        """

        if type(fence) is not WorkflowExecutionFence:
            raise TypeError("WorkflowExecutionFence is required")
        if type(event) is not WorkflowRunEventAppend:
            raise TypeError("WorkflowRunEventAppend is required")
        if event.event_type not in {
            "workflow.run.started",
            "workflow.node.queued",
            "workflow.node.started",
            "workflow.node.delta",
            "workflow.node.log",
            "workflow.node.completed",
            "workflow.node.failed",
        }:
            raise WorkflowRunStateConflict
        await self._lock_scope_authority(fence.scope)
        run = await self._locked_run(fence.scope, fence.run_id)
        if run.status not in _ACTIVE_RUN_STATUSES or run.execution_epoch != fence.execution_epoch or run.current_job_id != fence.job_id:
            raise WorkflowRunStateConflict
        job = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == fence.job_id,
                    JobRow.job_type == "workflow_run",
                    JobRow.project_id == fence.scope.project_id,
                    JobRow.owner_user_id == fence.scope.owner_user_id,
                    JobRow.workflow_run_id == fence.run_id,
                    JobRow.workflow_epoch == fence.execution_epoch,
                    JobRow.workflow_profile_key == run.worker_profile_key,
                    JobRow.status.in_(("leased", "running")),
                    JobRow.attempt_count == fence.job_attempt_number,
                    JobRow.lease_owner_id == fence.worker_id,
                    JobRow.lease_token_hash == fence.lease_token_hash,
                    JobRow.lease_expires_at > sa.func.clock_timestamp(),
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if job is None:
            raise WorkflowRunStateConflict
        attempt_exists = await self.session.scalar(
            sa.select(JobAttemptRow.id).where(
                JobAttemptRow.job_id == fence.job_id,
                JobAttemptRow.attempt_number == fence.job_attempt_number,
                JobAttemptRow.worker_id == fence.worker_id,
                JobAttemptRow.lease_token_hash == fence.lease_token_hash,
                JobAttemptRow.outcome.is_(None),
            )
        )
        if attempt_exists is None:
            raise WorkflowRunStateConflict
        next_seq = (
            await self.session.scalar(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(WorkflowRunEventInvariantRow.seq),
                        0,
                    )
                ).where(
                    WorkflowRunEventInvariantRow.project_id == fence.scope.project_id,
                    WorkflowRunEventInvariantRow.owner_user_id == fence.scope.owner_user_id,
                    WorkflowRunEventInvariantRow.workflow_run_id == run.id,
                )
            )
        ) + 1
        row = WorkflowRunEventRow(
            project_id=fence.scope.project_id,
            owner_user_id=fence.scope.owner_user_id,
            workflow_run_id=run.id,
            workflow_version_id=run.workflow_version_id,
            seq=next_seq,
            event_type=event.event_type,
            node_id=event.node_id,
            activation_id=event.activation_id,
            scope_path_hash=event.scope_path_hash,
            iteration_path=list(event.iteration_path),
            attempt=event.attempt,
            payload=event.materialize_payload(),
            occurred_at=_now(event.occurred_at),
        )
        self.session.add(row)
        await self.session.flush()
        return self._event_record(row)

    async def append_control_event(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
        event: WorkflowRunEventAppend,
    ) -> WorkflowRunEventRecord:
        run_id = _uuid(run_id, field_name="run_id")
        if type(event) is not WorkflowRunEventAppend:
            raise TypeError("WorkflowRunEventAppend is required")
        if event.event_type not in {
            "workflow.run.completed",
            "workflow.run.failed",
            "workflow.run.cancelled",
            "workflow.run.side_effect_unknown",
        }:
            raise WorkflowRunStateConflict
        await self._lock_scope_authority(scope)
        run = await self._locked_run(scope, run_id)
        expected_event = {
            "succeeded": "workflow.run.completed",
            "failed": "workflow.run.failed",
            "cancelled": "workflow.run.cancelled",
            "side_effect_unknown": "workflow.run.side_effect_unknown",
        }.get(run.status)
        if expected_event != event.event_type:
            raise WorkflowRunStateConflict
        existing = (
            await self.session.execute(
                sa.select(WorkflowRunEventRow)
                .where(
                    WorkflowRunEventRow.project_id == scope.project_id,
                    WorkflowRunEventRow.owner_user_id == scope.owner_user_id,
                    WorkflowRunEventRow.workflow_run_id == run.id,
                    WorkflowRunEventRow.event_type == event.event_type,
                )
                .order_by(WorkflowRunEventRow.seq)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._event_record(existing)
        next_seq = (
            await self.session.scalar(
                sa.select(
                    sa.func.coalesce(
                        sa.func.max(WorkflowRunEventInvariantRow.seq),
                        0,
                    )
                ).where(
                    WorkflowRunEventInvariantRow.project_id == scope.project_id,
                    WorkflowRunEventInvariantRow.owner_user_id == scope.owner_user_id,
                    WorkflowRunEventInvariantRow.workflow_run_id == run.id,
                )
            )
        ) + 1
        row = WorkflowRunEventRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            workflow_run_id=run.id,
            workflow_version_id=run.workflow_version_id,
            seq=next_seq,
            event_type=event.event_type,
            node_id=event.node_id,
            activation_id=event.activation_id,
            scope_path_hash=event.scope_path_hash,
            iteration_path=list(event.iteration_path),
            attempt=event.attempt,
            payload=event.materialize_payload(),
            occurred_at=_now(event.occurred_at),
        )
        self.session.add(row)
        await self.session.flush()
        return self._event_record(row)

    @staticmethod
    def _event_record(row: WorkflowRunEventRow) -> WorkflowRunEventRecord:
        return WorkflowRunEventRecord(
            event_id=row.id,
            run_id=row.workflow_run_id,
            workflow_version_id=row.workflow_version_id,
            seq=row.seq,
            event_type=row.event_type,
            node_id=row.node_id,
            activation_id=row.activation_id,
            scope_path_hash=row.scope_path_hash,
            iteration_path=tuple(row.iteration_path),
            attempt=row.attempt,
            payload=dict(row.payload),
            occurred_at=row.occurred_at,
        )

    async def retention_report(
        self,
        scope: WorkflowRunScope,
        run_id: uuid.UUID,
    ) -> WorkflowRunRetentionReport | None:
        run_id = _uuid(run_id, field_name="run_id")
        run = await self.get_run(scope, run_id)
        if run is None:
            return None

        async def count(
            row_type,
            *extra: sa.ColumnElement[bool],
        ) -> int:
            return int(
                await self.session.scalar(
                    sa.select(sa.func.count())
                    .select_from(row_type)
                    .where(
                        row_type.workflow_run_id == run_id,
                        row_type.project_id == scope.project_id,
                        row_type.owner_user_id == scope.owner_user_id,
                        *extra,
                    )
                )
                or 0
            )

        epoch_job_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkflowRunJobRow)
                .where(
                    WorkflowRunJobRow.workflow_run_id == run_id,
                    WorkflowRunJobRow.project_id == scope.project_id,
                    WorkflowRunJobRow.owner_user_id == scope.owner_user_id,
                )
            )
            or 0
        )
        event_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(WorkflowRunEventInvariantRow)
                .where(
                    WorkflowRunEventInvariantRow.workflow_run_id == run_id,
                    WorkflowRunEventInvariantRow.project_id == scope.project_id,
                    WorkflowRunEventInvariantRow.owner_user_id == scope.owner_user_id,
                )
            )
            or 0
        )
        return WorkflowRunRetentionReport(
            run_id=run_id,
            terminal=run.status in _TERMINAL_RUN_STATUSES,
            # G51 owns privileged physical purge.  Ordinary G13 repository
            # deletion must remain blocked by append-only journals and RESTRICT.
            ordinary_delete_supported=False,
            epoch_job_count=epoch_job_count,
            core_snapshot_count=await count(WorkflowRunSnapshotRow),
            runtime_policy_snapshot_count=await count(WorkflowRunRuntimePolicySnapshotRow),
            model_snapshot_count=await count(WorkflowRunModelSnapshotRow),
            code_snapshot_count=await count(WorkflowRunCodeSnapshotRow),
            http_snapshot_count=await count(WorkflowRunHttpSnapshotRow),
            event_count=event_count,
            effect_count=await count(WorkflowNodeEffectRow),
            open_effect_count=await count(
                WorkflowNodeEffectRow,
                WorkflowNodeEffectRow.status.in_(("prepared", "dispatching")),
            ),
            code_lease_count=await count(WorkflowCodeSandboxLeaseRow),
            open_code_lease_count=await count(
                WorkflowCodeSandboxLeaseRow,
                WorkflowCodeSandboxLeaseRow.state != "destroyed",
            ),
        )


__all__ = [
    "WorkflowAuthorityMissing",
    "WorkflowCodeRequirementCreate",
    "WorkflowCredentialSlotCreate",
    "WorkflowDefinitionConflict",
    "WorkflowDefinitionCreate",
    "WorkflowDefinitionRecord",
    "WorkflowDraftCASConflict",
    "WorkflowDraftRecord",
    "WorkflowDraftUpdate",
    "WorkflowExecutionFence",
    "WorkflowHttpRequirementCreate",
    "WorkflowManualRetryForbidden",
    "WorkflowModelRefCreate",
    "WorkflowPersistenceError",
    "WorkflowRepository",
    "WorkflowRetrySource",
    "WorkflowRunAdmissionRequest",
    "WorkflowRunCreate",
    "WorkflowRunEventAppend",
    "WorkflowRunEventRecord",
    "WorkflowRunIdempotencyConflict",
    "WorkflowRunJobRecord",
    "WorkflowRunRecord",
    "WorkflowRunRetentionReport",
    "WorkflowRunScope",
    "WorkflowRunStage",
    "WorkflowRunStateConflict",
    "WorkflowVersionPublish",
    "WorkflowVersionCodeRequirementRecord",
    "WorkflowVersionHttpRequirementRecord",
    "WorkflowVersionRecord",
]
