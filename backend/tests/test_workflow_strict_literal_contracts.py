from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.workflows.catalog_contracts import (
    NodeCatalogResponseV1,
    PortDerivationV1,
    ResolvedWorkflowInstancePortsV1,
)
from app.workflows.compatibility import (
    WorkflowCompilerSnapshotContractV1,
    WorkflowRunSnapshotIdentityV1,
)
from app.workflows.contracts import WorkflowEventEnvelopeV1
from app.workflows.run_contracts import (
    WorkflowOwnerPrivateRunV1,
    WorkflowPrivateJobV1,
    WorkflowPrivateRunAuthorityV1,
    WorkflowRunAdmissionResponseV1,
    WorkflowRunContractFixtureV1,
    WorkflowRunJobAuthorityV1,
    WorkflowRunJobEpochMappingV1,
)
from app.workflows.runtime_policy import (
    WorkflowRuntimeEffectivePolicyV1,
    WorkflowRuntimePolicyV1,
    WorkflowRuntimeStoredPolicyV1,
)
from deerflow.workflows.contracts import (
    CanvasDocumentV1,
    RestrictedJsonTemplate,
    RestrictedTemplate,
    WorkflowNodeSpecBase,
    WorkflowSpecV1,
)

_SHARED_INVALID_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-run-invalid-v1.json"
_INVALID_LITERAL_ONE_VALUES = (
    *json.loads(_SHARED_INVALID_FIXTURE.read_text(encoding="utf-8"))["strict_literal_one_invalid_values"],
    1.0,
)

_STRICT_LITERAL_ONE_FIELDS: tuple[tuple[type[BaseModel], str], ...] = (
    (RestrictedTemplate, "version"),
    (RestrictedJsonTemplate, "version"),
    (WorkflowNodeSpecBase, "type_version"),
    (WorkflowSpecV1, "schema_version"),
    (CanvasDocumentV1, "schema_version"),
    (WorkflowEventEnvelopeV1, "schema_version"),
    (PortDerivationV1, "version"),
    (ResolvedWorkflowInstancePortsV1, "schema_version"),
    (NodeCatalogResponseV1, "schema_version"),
    (WorkflowRuntimePolicyV1, "schema_version"),
    (WorkflowRuntimeStoredPolicyV1, "schema_version"),
    (WorkflowRuntimeEffectivePolicyV1, "schema_version"),
    (WorkflowRunAdmissionResponseV1, "schema_version"),
    (WorkflowOwnerPrivateRunV1, "schema_version"),
    (WorkflowPrivateRunAuthorityV1, "schema_version"),
    (WorkflowPrivateJobV1, "schema_version"),
    (WorkflowRunJobEpochMappingV1, "schema_version"),
    (WorkflowRunJobAuthorityV1, "schema_version"),
    (WorkflowRunContractFixtureV1, "schema_version"),
    (WorkflowRunSnapshotIdentityV1, "schema_version"),
    (WorkflowCompilerSnapshotContractV1, "schema_version"),
)


@pytest.mark.parametrize(
    ("model", "field_name"),
    _STRICT_LITERAL_ONE_FIELDS,
    ids=lambda value: value.__name__ if isinstance(value, type) else value,
)
@pytest.mark.parametrize(
    "invalid_value",
    _INVALID_LITERAL_ONE_VALUES,
    ids=lambda value: f"{type(value).__name__}-{value!r}",
)
def test_every_literal_one_contract_rejects_non_integer_json_scalars(
    model: type[BaseModel],
    field_name: str,
    invalid_value: object,
) -> None:
    payload = json.dumps({field_name: invalid_value})

    with pytest.raises(ValidationError) as error:
        model.model_validate_json(payload)

    assert (field_name,) in {detail["loc"] for detail in error.value.errors()}
