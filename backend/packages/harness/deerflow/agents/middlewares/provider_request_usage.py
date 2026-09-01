"""Compatibility façade for Provider-request profile, measurement, and guard contracts."""

from __future__ import annotations

from deerflow.agents.middlewares.provider_request_guard import (
    FinalProviderRequestGuard,
    ProviderDispatchOutcomeAmbiguous,
    ProviderRequestEvidenceObserver,
)
from deerflow.agents.middlewares.provider_request_measurement import (
    measure_profile_context,
    measure_profile_snapshot_context,
)
from deerflow.agents.middlewares.provider_request_profile import (
    _MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE,  # noqa: F401
    _PROVIDER_VISUAL_MAX_TOKENS_PER_IMAGE,  # noqa: F401
    PROVIDER_REQUEST_ERROR_CONTRACT,
    PROVIDER_REQUEST_ESTIMATOR_REVISION,
    ContextCapacityExceeded,
    ProviderRequestComponent,
    ProviderRequestComponentSnapshot,  # noqa: F401
    ProviderRequestContextMeasurement,
    ProviderRequestMaterialMeasurement,
    ProviderRequestMeasurementSnapshot,
    ProviderRequestProfile,
    ProviderRequestProfileDrift,
    ProviderRequestProfileSnapshot,
    ProviderRequestUsageUnsupported,
    ProviderToolSchemaFact,
    build_provider_request_profile,
    build_provider_request_profile_snapshot_from_facts,
    canonicalize_full_tools,
    collect_custom_middleware_request_contract,
    collect_middleware_system_prompts,
    collect_middleware_tools,
    contains_visual_material,
    declared_visual_max_tokens_per_image,
    provider_request_closure_identity,
    provider_request_runtime_policy_compatibility_identity,
    provider_request_runtime_policy_identity,
    provider_tool_schema_fact,
    resolve_provider_adapter,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.models.provider_outcome import ProviderNoResponseProvenError

__all__ = [
    "PROVIDER_REQUEST_ERROR_CONTRACT",
    "PROVIDER_REQUEST_ESTIMATOR_REVISION",
    "PROVIDER_REQUEST_MEASUREMENT_STATE_KEY",
    "PROVIDER_REQUEST_PROFILE_STATE_KEY",
    "ContextCapacityExceeded",
    "FinalProviderRequestGuard",
    "ProviderDispatchOutcomeAmbiguous",
    "ProviderNoResponseProvenError",
    "ProviderRequestEvidenceObserver",
    "ProviderRequestComponent",
    "ProviderRequestContextMeasurement",
    "ProviderRequestMaterialMeasurement",
    "ProviderRequestMeasurementSnapshot",
    "ProviderRequestProfile",
    "ProviderRequestProfileDrift",
    "ProviderRequestProfileSnapshot",
    "ProviderToolSchemaFact",
    "ProviderRequestUsageUnsupported",
    "build_provider_request_profile",
    "build_provider_request_profile_snapshot_from_facts",
    "canonicalize_full_tools",
    "collect_custom_middleware_request_contract",
    "collect_middleware_tools",
    "collect_middleware_system_prompts",
    "contains_visual_material",
    "declared_visual_max_tokens_per_image",
    "measure_profile_context",
    "measure_profile_snapshot_context",
    "provider_request_closure_identity",
    "provider_tool_schema_fact",
    "provider_request_runtime_policy_identity",
    "provider_request_runtime_policy_compatibility_identity",
    "resolve_provider_adapter",
]
