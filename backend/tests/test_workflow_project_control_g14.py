from __future__ import annotations

import json
import uuid
from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.final_schema import FinalSchemaRequired, FinalSchemaState
from app.system_runtime_settings import workflow_runtime as workflow_runtime_module
from app.system_runtime_settings.errors import SystemRuntimePolicyUnavailable
from app.system_runtime_settings.models import (
    LockedWorkflowRuntimePolicy,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.workflow_runtime import (
    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
    WorkflowRuntimeConvergence,
    WorkflowRuntimeFacetReadinessV1,
    create_workflow_runtime_facet_readiness,
)
from app.workflows.catalog_contracts import (
    FIRST_BATCH_NODE_REGISTRY_V1,
    NodeAvailability,
    NodeCatalogResponseV1,
    WorkflowCatalogCapabilityProjectionV1,
    build_project_node_catalog_v1,
    node_catalog_response_public_projection_v1,
)
from app.workflows.contracts import (
    WorkflowControlPlaneReadyV1,
    WorkflowDisabledV1,
    WorkflowPolicyUnavailableV1,
    WorkflowSchemaUnavailableV1,
)
from app.workflows.errors import WorkflowUnavailable
from app.workflows.project_control_service import WorkflowProjectControlService
from app.workflows.runtime_policy import (
    WorkflowRuntimePolicyV1,
    workflow_runtime_policy_checksum,
)
from deerflow.workflows import WORKFLOW_NODE_KINDS


def _policy(
    *,
    enabled: bool,
    admission_enabled: bool = False,
    code_enabled: bool = False,
    http_enabled: bool = False,
    http_write_enabled: bool = True,
    http_profile_digest: str = "d" * 64,
    allowed_types: tuple[str, ...] = WORKFLOW_NODE_KINDS,
) -> WorkflowRuntimePolicyV1:
    payload = default_policy_value(RuntimePolicySection.WORKFLOW_RUNTIME).model_dump(mode="json")
    payload["enabled"] = enabled
    payload["admission_enabled"] = admission_enabled
    payload["catalog"]["allowed_type_versions"] = [  # type: ignore[index]
        {"type": node_type, "versions": [1]} for node_type in allowed_types
    ]
    if code_enabled:
        payload["execution_limits"]["max_code_activations"] = 16  # type: ignore[index]
        payload["code"].update(  # type: ignore[union-attr]
            {
                "enabled": True,
                "provider_adapter_key": "aio_isolated_code_v1",
                "execution_profile_id": "isolated-python-312",
                "image_digest": f"sha256:{'c' * 64}",
                "isolation_profile": "deny-all-v1",
            }
        )
    if http_enabled:
        payload["execution_limits"]["max_http_calls"] = 16  # type: ignore[index]
        payload["http"].update(  # type: ignore[union-attr]
            {
                "enabled": True,
                "write_enabled": http_write_enabled,
                "egress_profile_id": "controlled-egress-v1",
                "egress_profile_digest": http_profile_digest,
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
    return WorkflowRuntimePolicyV1.model_validate(payload)


def _locked(policy: WorkflowRuntimePolicyV1, *, revision: int = 7) -> LockedWorkflowRuntimePolicy:
    return LockedWorkflowRuntimePolicy.create(
        policy_version_id=uuid.UUID("53f5a2b9-1c63-43ec-92d4-2aa799f18857"),
        revision=revision,
        schema_version=1,
        payload_checksum=workflow_runtime_policy_checksum(policy),
        value=policy,
    )


def _facets(
    *,
    generic: bool | None = None,
    code: bool = False,
    http: bool = False,
) -> WorkflowRuntimeFacetReadinessV1:
    if generic is None:
        generic = code or http
    return create_workflow_runtime_facet_readiness(
        generic_ready=generic,
        code_ready=code,
        http_ready=http,
    )


def _entry(response: NodeCatalogResponseV1, node_type: str):
    return next(entry for entry in response.entries if entry.definition.type == node_type)


def test_catalog_response_requires_the_exact_canonical_nine_entries_and_closed_reasons() -> None:
    enabled = {
        "definition": FIRST_BATCH_NODE_REGISTRY_V1[0].model_dump(mode="json", by_alias=True),
        "availability": {"state": "enabled"},
        "public_limits": {"max_timeout_ms": 30_000},
    }
    with pytest.raises(ValidationError):
        NodeCatalogResponseV1.model_validate(
            {
                "schema_version": 1,
                "catalog_generation": "a" * 64,
                "availability_generation": "b" * 64,
                "entries": [enabled],
            }
        )

    for unknown in (
        "WORKFLOW_RUNTIME_PENDING",
        "WORKFLOW_PROVIDER_AIO_UNAVAILABLE",
        "WORKFLOW_CODE_SANDBOX_UNAVAILABLE",
    ):
        with pytest.raises(ValidationError):
            NodeAvailability.model_validate({"state": "disabled", "reason_code": unknown})


@pytest.mark.parametrize(
    ("node_type", "expected_reason"),
    [
        ("start", "WORKFLOW_DISABLED"),
        ("llm", "WORKFLOW_DISABLED"),
        ("condition", "WORKFLOW_DISABLED"),
        ("transform", "WORKFLOW_DISABLED"),
        ("variable_aggregate", "WORKFLOW_DISABLED"),
        ("loop", "WORKFLOW_DISABLED"),
        ("http_request", "WORKFLOW_DISABLED"),
        ("python_code", "WORKFLOW_DISABLED"),
        ("end", "WORKFLOW_DISABLED"),
    ],
)
def test_product_disabled_has_highest_catalog_priority(
    node_type: str,
    expected_reason: str,
) -> None:
    catalog = build_project_node_catalog_v1(
        locked=_locked(
            _policy(
                enabled=False,
                allowed_types=(),
            )
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=False,
            http_use=False,
        ),
        facets=_facets(),
    )

    assert len(catalog.entries) == 9
    assert [entry.definition.type for entry in catalog.entries] == list(WORKFLOW_NODE_KINDS)
    assert _entry(catalog, node_type).availability.reason_code == expected_reason
    http_authoring = _entry(catalog, "http_request").http_authoring
    assert http_authoring is not None
    assert http_authoring.endpoints == ()


def test_catalog_priority_is_capability_then_allowlist_then_subpolicy_then_profile() -> None:
    missing_capability = build_project_node_catalog_v1(
        locked=_locked(
            _policy(
                enabled=True,
                allowed_types=tuple(node_type for node_type in WORKFLOW_NODE_KINDS if node_type not in {"python_code", "http_request"}),
            )
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=False,
            http_use=False,
        ),
        facets=_facets(code=True, http=True),
    )
    assert _entry(missing_capability, "python_code").availability.reason_code == "WORKFLOW_NODE_CAPABILITY_REQUIRED"
    assert _entry(missing_capability, "http_request").availability.reason_code == "WORKFLOW_NODE_CAPABILITY_REQUIRED"

    not_allowed = build_project_node_catalog_v1(
        locked=_locked(
            _policy(
                enabled=True,
                allowed_types=tuple(node_type for node_type in WORKFLOW_NODE_KINDS if node_type not in {"start", "python_code", "http_request"}),
            )
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=True, http=True),
    )
    assert _entry(not_allowed, "start").availability.reason_code == "WORKFLOW_NODE_NOT_ALLOWED"
    assert _entry(not_allowed, "python_code").availability.reason_code == "WORKFLOW_NODE_NOT_ALLOWED"
    assert _entry(not_allowed, "http_request").availability.reason_code == "WORKFLOW_NODE_NOT_ALLOWED"

    disabled_subpolicy = build_project_node_catalog_v1(
        locked=_locked(_policy(enabled=True)),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=True, http=True),
    )
    assert _entry(disabled_subpolicy, "python_code").availability.reason_code == "WORKFLOW_CODE_DISABLED"
    assert _entry(disabled_subpolicy, "http_request").availability.reason_code == "WORKFLOW_HTTP_DISABLED"
    assert all(_entry(disabled_subpolicy, node_type).availability.state == "enabled" for node_type in WORKFLOW_NODE_KINDS if node_type not in {"python_code", "http_request"})

    unavailable_profile = build_project_node_catalog_v1(
        locked=_locked(
            _policy(
                enabled=True,
                code_enabled=True,
                http_enabled=True,
            )
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=False, http=False),
    )
    assert _entry(unavailable_profile, "python_code").availability.reason_code == "WORKFLOW_CODE_PROFILE_UNAVAILABLE"
    assert _entry(unavailable_profile, "http_request").availability.reason_code == "WORKFLOW_HTTP_PROFILE_UNAVAILABLE"


def test_catalog_enables_exact_nine_and_projects_only_public_limits() -> None:
    policy = _policy(
        enabled=True,
        admission_enabled=True,
        code_enabled=True,
        http_enabled=True,
    )
    catalog = build_project_node_catalog_v1(
        locked=_locked(policy),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(generic=True, code=True, http=True),
    )

    assert len(catalog.entries) == 9
    assert all(entry.availability.state == "enabled" for entry in catalog.entries)
    assert all(entry.public_limits is not None and entry.public_limits.max_timeout_ms == policy.execution_limits.max_node_timeout_ms for entry in catalog.entries if entry.definition.type not in {"python_code", "http_request"})
    code = _entry(catalog, "python_code").public_limits
    assert code is not None
    assert code.max_source_bytes == policy.code.hard_limits.max_source_bytes
    assert code.max_timeout_ms == min(
        policy.execution_limits.max_node_timeout_ms,
        policy.code.hard_limits.wall_timeout_ms,
    )
    http = _entry(catalog, "http_request").public_limits
    assert http is not None
    assert http.max_http_request_bytes == policy.execution_limits.max_http_request_bytes
    assert http.max_http_response_bytes == policy.execution_limits.max_http_response_bytes
    assert http.max_timeout_ms == min(
        policy.execution_limits.max_node_timeout_ms,
        policy.http.transport.total_timeout_ms,
    )
    http_authoring = _entry(catalog, "http_request").http_authoring
    assert http_authoring is not None
    assert http_authoring.model_dump(mode="json") == {
        "endpoints": [
            {
                "id": "public-api",
                "origin": "https://api.example.com",
                "allowed_methods": ["GET", "POST"],
                "write_idempotency": "server_derived_key",
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
    assert all(entry.http_authoring is None for entry in catalog.entries if entry.definition.type != "http_request")
    loop = _entry(catalog, "loop").public_limits
    assert loop is not None
    assert loop.max_iterations == policy.graph_limits.max_loop_iterations
    aggregate = _entry(catalog, "variable_aggregate").public_limits
    assert aggregate is not None
    assert aggregate.max_aggregate_groups == policy.graph_limits.max_aggregate_groups
    assert aggregate.max_aggregate_candidates == policy.graph_limits.max_aggregate_candidates

    serialized = json.dumps(
        catalog.model_dump(mode="json", by_alias=True),
        sort_keys=True,
    )
    for private in (
        str(_locked(policy).policy_version_id),
        policy.code.execution_profile_id,
        policy.code.image_digest,
        policy.code.isolation_profile,
        policy.http.egress_profile_id,
        policy.http.egress_profile_digest,
        policy.http.endpoint_policies[0].idempotency_header,
    ):
        assert private is not None
        assert private not in serialized


def test_catalog_filters_platform_disabled_writes_from_http_authoring() -> None:
    policy = _policy(
        enabled=True,
        http_enabled=True,
        http_write_enabled=False,
    )

    catalog = build_project_node_catalog_v1(
        locked=_locked(policy),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(generic=True, code=True, http=True),
    )

    http_authoring = _entry(catalog, "http_request").http_authoring
    assert http_authoring is not None
    assert http_authoring.model_dump(mode="json") == {
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


def test_catalog_public_projection_round_trips_through_response_model_validation() -> None:
    catalog = build_project_node_catalog_v1(
        locked=_locked(
            _policy(
                enabled=True,
                code_enabled=True,
                http_enabled=True,
            )
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=True, http=True),
    )

    public_projection = node_catalog_response_public_projection_v1(catalog)

    assert NodeCatalogResponseV1.model_validate(public_projection) == catalog
    projected_entries = public_projection["entries"]
    assert type(projected_entries) is list
    assert all("reason_code" not in entry["availability"] for entry in projected_entries)
    assert all(all(value is not None for value in entry["public_limits"].values()) for entry in projected_entries)
    control_port = projected_entries[0]["definition"]["output_ports"][0]
    assert "value_type" in control_port
    assert control_port["value_type"] is None
    serialized = json.dumps(public_projection, sort_keys=True)
    assert '"schema_ref": null' not in serialized


def test_catalog_authority_cannot_drift_by_mutating_entries_or_availability() -> None:
    catalog = build_project_node_catalog_v1(
        locked=_locked(_policy(enabled=True)),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(),
    )
    original = node_catalog_response_public_projection_v1(catalog)

    assert type(catalog.entries) is tuple
    with pytest.raises(AttributeError):
        catalog.entries.pop()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        catalog.entries[0] = catalog.entries[-1]  # type: ignore[index]
    with pytest.raises(ValidationError):
        catalog.entries[0].availability.state = "disabled"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        catalog.entries[0].availability.reason_code = "WORKFLOW_DISABLED"  # type: ignore[misc]
    with pytest.raises(TypeError):
        dict.__setitem__(
            catalog.entries[0].definition.config_schema,
            "x-mutated",
            True,
        )
    with pytest.raises(TypeError):
        list.clear(catalog.entries[0].definition.output_ports)
    with pytest.raises(TypeError):
        list.append(
            _entry(catalog, "python_code").definition.required_capabilities,
            "workflow.http.use",
        )
    data_port = next(port for entry in catalog.entries for port in (*entry.definition.input_ports, *entry.definition.output_ports) if port.value_type is not None)
    with pytest.raises(ValidationError):
        data_port.value_type.kind = "json"  # type: ignore[misc]

    assert node_catalog_response_public_projection_v1(catalog) == original
    assert type(original["entries"]) is list


def test_availability_generation_changes_only_with_safe_availability_inputs() -> None:
    locked = _locked(_policy(enabled=True, code_enabled=True, http_enabled=True))
    first = build_project_node_catalog_v1(
        locked=locked,
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=False, http=True),
    )
    restored = build_project_node_catalog_v1(
        locked=locked,
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=True, http=True),
    )
    revoked = build_project_node_catalog_v1(
        locked=locked,
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=False,
            http_use=True,
        ),
        facets=_facets(code=True, http=True),
    )
    same_availability_new_policy_identity = build_project_node_catalog_v1(
        locked=_locked(
            _policy(enabled=True, code_enabled=True, http_enabled=True),
            revision=8,
        ),
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
        facets=_facets(code=False, http=True),
    )

    assert first.catalog_generation == restored.catalog_generation == revoked.catalog_generation
    assert first.catalog_generation != same_availability_new_policy_identity.catalog_generation
    assert first.availability_generation == same_availability_new_policy_identity.availability_generation
    assert (
        len(
            {
                first.availability_generation,
                restored.availability_generation,
                revoked.availability_generation,
            }
        )
        == 3
    )


class _SchemaProbe:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.calls = 0

    async def require_ready(self, _session: object) -> FinalSchemaState:
        self.calls += 1
        state = FinalSchemaState(
            revision="full_schema_v12" if self.ready else "full_schema_v10",
            missing_relations=() if self.ready else ("workflow_runs",),
            ready=self.ready,
        )
        if not self.ready:
            raise FinalSchemaRequired(state)
        return state


class _PolicyReader:
    def __init__(
        self,
        locked: LockedWorkflowRuntimePolicy | None,
    ) -> None:
        self.locked = locked
        self.calls = 0

    async def read_current(self, _session: object) -> LockedWorkflowRuntimePolicy:
        self.calls += 1
        if self.locked is None:
            raise SystemRuntimePolicyUnavailable
        return self.locked


class _FacetReader:
    def __init__(self, facets: WorkflowRuntimeFacetReadinessV1) -> None:
        self.facets = facets
        self.calls = 0

    async def read_facets_in_session(
        self,
        _session: object,
        _locked: LockedWorkflowRuntimePolicy,
    ) -> WorkflowRuntimeFacetReadinessV1:
        self.calls += 1
        return self.facets


@pytest.mark.anyio
async def test_project_readiness_schema_has_priority_over_policy_and_worker() -> None:
    schema = _SchemaProbe(ready=False)
    policy = _PolicyReader(None)
    facets = _FacetReader(_facets(generic=True, code=True, http=True))
    service = WorkflowProjectControlService(
        schema_probe=schema,
        policy_reader=policy,
        convergence=facets,
    )

    readiness = await service.read_readiness(
        object(),
        request_id="g14-schema-first",
    )

    assert type(readiness) is WorkflowSchemaUnavailableV1
    assert policy.calls == 0
    assert facets.calls == 0
    with pytest.raises(WorkflowUnavailable):
        await service.read_node_catalog(
            object(),
            request_id="g14-schema-first",
            capabilities=WorkflowCatalogCapabilityProjectionV1(
                code_use=False,
                http_use=False,
            ),
        )


@pytest.mark.anyio
async def test_project_readiness_four_state_matrix_and_generic_admission_facet() -> None:
    cases = (
        (
            None,
            _facets(),
            WorkflowPolicyUnavailableV1,
            False,
        ),
        (
            _locked(_policy(enabled=False)),
            _facets(generic=True),
            WorkflowDisabledV1,
            False,
        ),
        (
            _locked(_policy(enabled=True, admission_enabled=False)),
            _facets(generic=True),
            WorkflowControlPlaneReadyV1,
            False,
        ),
        (
            _locked(_policy(enabled=True, admission_enabled=True)),
            _facets(generic=False),
            WorkflowControlPlaneReadyV1,
            False,
        ),
        (
            _locked(_policy(enabled=True, admission_enabled=True)),
            _facets(generic=True),
            WorkflowControlPlaneReadyV1,
            True,
        ),
    )
    for index, (locked, facet_state, expected_type, admission_ready) in enumerate(cases):
        service = WorkflowProjectControlService(
            schema_probe=_SchemaProbe(ready=True),
            policy_reader=_PolicyReader(locked),
            convergence=_FacetReader(facet_state),
        )
        readiness = await service.read_readiness(
            object(),
            request_id=f"g14-readiness-{index}",
        )
        assert type(readiness) is expected_type
        assert readiness.admission_ready is admission_ready


@pytest.mark.anyio
async def test_project_catalog_reuses_schema_policy_order_and_never_returns_a_partial_catalog() -> None:
    policy = _locked(_policy(enabled=True))
    service = WorkflowProjectControlService(
        schema_probe=_SchemaProbe(ready=True),
        policy_reader=_PolicyReader(policy),
        convergence=_FacetReader(_facets()),
    )
    response = await service.read_node_catalog(
        object(),
        request_id="g14-catalog",
        capabilities=WorkflowCatalogCapabilityProjectionV1(
            code_use=True,
            http_use=True,
        ),
    )
    assert len(response.entries) == 9

    unavailable = WorkflowProjectControlService(
        schema_probe=_SchemaProbe(ready=True),
        policy_reader=_PolicyReader(None),
        convergence=_FacetReader(_facets()),
    )
    with pytest.raises(WorkflowUnavailable) as error:
        await unavailable.read_node_catalog(
            object(),
            request_id="g14-policy-unavailable",
            capabilities=WorkflowCatalogCapabilityProjectionV1(
                code_use=True,
                http_use=True,
            ),
        )
    assert error.value.request_id == "g14-policy-unavailable"


def test_server_capability_projection_is_strict_boolean_only() -> None:
    assert WorkflowCatalogCapabilityProjectionV1(
        code_use=True,
        http_use=False,
    ).model_dump() == {"code_use": True, "http_use": False}
    for payload in (
        {"code_use": 1, "http_use": False},
        {"code_use": True, "http_use": False, "role": "admin"},
        {"code_use": True},
    ):
        with pytest.raises(ValidationError):
            WorkflowCatalogCapabilityProjectionV1.model_validate(deepcopy(payload))


def test_specialized_facet_readiness_requires_generic_runtime_readiness() -> None:
    for code, http in ((True, False), (False, True), (True, True)):
        with pytest.raises(ValidationError):
            create_workflow_runtime_facet_readiness(
                generic_ready=False,
                code_ready=code,
                http_ready=http,
            )


@pytest.mark.anyio
async def test_profile_resolver_failures_are_contained_as_closed_facets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResolverFailure(Exception):
        pass

    def fail_profile_resolution(_policy: WorkflowRuntimePolicyV1) -> str:
        raise ResolverFailure

    convergence = WorkflowRuntimeConvergence(
        code_profile_digest_resolver=fail_profile_resolution,
        http_profile_digest_resolver=fail_profile_resolution,
    )

    async def candidates(
        _session: AsyncSession,
        *,
        desired: object,
    ) -> tuple[frozenset[str], ...]:
        assert desired is not None
        return (frozenset({WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1}),)

    monkeypatch.setattr(
        convergence,
        "_fresh_exact_worker_profiles",
        candidates,
    )
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
    session = AsyncSession()
    try:
        async with session.begin():
            facets = await convergence.read_facets_in_session(
                session,
                _locked(
                    _policy(
                        enabled=True,
                        code_enabled=True,
                        http_enabled=True,
                    )
                ),
            )
    finally:
        await session.close()

    assert facets.generic_ready is True
    assert facets.code_ready is False
    assert facets.http_ready is False


@pytest.mark.parametrize(
    "shared_digest",
    [
        WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
        "0" * 64,
        "e" * 64,
    ],
)
@pytest.mark.anyio
async def test_specialized_profile_domains_cannot_alias_each_other_or_reserved_digests(
    monkeypatch: pytest.MonkeyPatch,
    shared_digest: str,
) -> None:
    convergence = WorkflowRuntimeConvergence(
        code_profile_digest_resolver=lambda _policy: shared_digest,
        http_profile_digest_resolver=lambda _policy: shared_digest,
    )

    async def generic_only(
        _session: AsyncSession,
        *,
        desired: object,
    ) -> tuple[frozenset[str], ...]:
        assert desired is not None
        return (
            frozenset(
                {
                    WORKFLOW_GENERIC_RUNTIME_PROFILE_DIGEST_V1,
                    shared_digest,
                }
            ),
        )

    monkeypatch.setattr(
        convergence,
        "_fresh_exact_worker_profiles",
        generic_only,
    )
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
    session = AsyncSession()
    try:
        async with session.begin():
            facets = await convergence.read_facets_in_session(
                session,
                _locked(
                    _policy(
                        enabled=True,
                        code_enabled=True,
                        http_enabled=True,
                        http_profile_digest=shared_digest,
                    )
                ),
            )
    finally:
        await session.close()

    assert facets.generic_ready is True
    assert facets.code_ready is False
    assert facets.http_ready is False
