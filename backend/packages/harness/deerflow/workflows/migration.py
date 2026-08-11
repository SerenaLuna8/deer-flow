"""Version-dispatched parsing and migration for authored Workflow documents.

Published Workflow versions are never rewritten.  This module is the explicit
Draft migration boundary: a caller selects a supported source schema and an
exact target schema, then persists the returned document through the normal
Draft CAS operation.  There is deliberately no "best effort" fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Final

from deerflow.workflows.contracts import CanvasDocumentV1, WorkflowSpecV1

CURRENT_WORKFLOW_SCHEMA_VERSION: Final = 1


class WorkflowSchemaMigrationError(ValueError):
    """A source or target Workflow schema has no installed exact migration."""


def _require_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowSchemaMigrationError("WORKFLOW_SCHEMA_UNSUPPORTED: Workflow document must be an object")
    return deepcopy(dict(value))


def _require_schema_version(payload: Mapping[str, object]) -> int:
    version = payload.get("schema_version")
    if type(version) is not int or version != CURRENT_WORKFLOW_SCHEMA_VERSION:
        raise WorkflowSchemaMigrationError("WORKFLOW_SCHEMA_UNSUPPORTED: no exact parser is installed for this schema version")
    return version


def parse_workflow_spec(value: object) -> WorkflowSpecV1:
    """Parse one exact supported Workflow Spec without coercion or migration."""

    payload = _require_object(value)
    _require_schema_version(payload)
    return WorkflowSpecV1.model_validate(payload)


def migrate_workflow_spec(
    value: object,
    *,
    target_schema_version: int = CURRENT_WORKFLOW_SCHEMA_VERSION,
) -> WorkflowSpecV1:
    """Migrate a Draft to one exact target version.

    V1 is currently the only installed contract, so V1-to-V1 is an explicit
    validation/round-trip rather than a silent identity cast.  Adding V2 must
    add a named source-to-target migration here before the dispatcher accepts
    it.
    """

    if type(target_schema_version) is not int or target_schema_version != CURRENT_WORKFLOW_SCHEMA_VERSION:
        raise WorkflowSchemaMigrationError("WORKFLOW_SCHEMA_UNSUPPORTED: target schema version is not installed")
    return parse_workflow_spec(value)


def round_trip_workflow_spec(value: WorkflowSpecV1) -> WorkflowSpecV1:
    """Reparse the public projection to prove omission/null round-trip safety."""

    if not isinstance(value, WorkflowSpecV1):
        raise TypeError("value must be a WorkflowSpecV1")
    return parse_workflow_spec(value.model_dump(mode="json", by_alias=True, exclude_unset=True))


def parse_canvas_document(value: object) -> CanvasDocumentV1:
    """Parse a Canvas document; React Flow session/runtime state is forbidden."""

    payload = _require_object(value)
    _require_schema_version(payload)
    return CanvasDocumentV1.model_validate(payload)


__all__ = [
    "CURRENT_WORKFLOW_SCHEMA_VERSION",
    "WorkflowSchemaMigrationError",
    "migrate_workflow_spec",
    "parse_canvas_document",
    "parse_workflow_spec",
    "round_trip_workflow_spec",
]
