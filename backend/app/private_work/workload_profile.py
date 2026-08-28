from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type RunWorkloadProfileName = Literal["interactive", "research"]

RUN_WORKLOAD_PROFILE_KWARG = "__run_workload_profile"

_PROFILE_NAMES = frozenset({"interactive", "research"})
_PROFILE_KEYS = frozenset({"name"})


class RunWorkloadProfileUnsupported(Exception):
    """Internal fail-closed workload-profile validation marker."""


def _validate_profile_name(value: object) -> None:
    if type(value) is not str or value not in _PROFILE_NAMES:
        raise TypeError("workload profile name is invalid")


def _require_supported_policy_schema_version(value: object) -> int:
    if type(value) is not int or value != 1:
        raise RunWorkloadProfileUnsupported
    return value


@dataclass(frozen=True, slots=True)
class RequestedRunWorkloadProfile:
    """Strict, non-authoritative workload selection awaiting admission."""

    name: RunWorkloadProfileName = "interactive"

    def __post_init__(self) -> None:
        _validate_profile_name(self.name)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name}


@dataclass(frozen=True, slots=True)
class EffectiveRunWorkloadProfile:
    """Exact, server-admitted workload profile consumed by the Worker."""

    name: RunWorkloadProfileName

    def __post_init__(self) -> None:
        _validate_profile_name(self.name)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name}


def persisted_run_workload_profile(
    requested: RequestedRunWorkloadProfile,
    effective: EffectiveRunWorkloadProfile,
) -> dict[str, object]:
    if type(requested) is not RequestedRunWorkloadProfile or type(effective) is not EffectiveRunWorkloadProfile:
        raise TypeError("exact workload profile types are required")
    return {
        "requested": requested.as_dict(),
        "effective": effective.as_dict(),
    }


def parse_persisted_run_workload_profile(
    value: object,
) -> tuple[RequestedRunWorkloadProfile, EffectiveRunWorkloadProfile]:
    if not isinstance(value, Mapping) or set(value) != {"requested", "effective"}:
        raise RunWorkloadProfileUnsupported
    requested_value = value.get("requested")
    effective_value = value.get("effective")
    if not isinstance(requested_value, Mapping) or set(requested_value) != _PROFILE_KEYS or not isinstance(effective_value, Mapping) or set(effective_value) != _PROFILE_KEYS:
        raise RunWorkloadProfileUnsupported
    try:
        requested = RequestedRunWorkloadProfile(name=requested_value.get("name"))
        effective = EffectiveRunWorkloadProfile(name=effective_value.get("name"))
    except TypeError:
        raise RunWorkloadProfileUnsupported from None
    return requested, effective


def effective_run_workload_profile_from_kwargs(
    kwargs: Mapping[str, object],
    *,
    policy_schema_version: object,
) -> EffectiveRunWorkloadProfile:
    if not isinstance(kwargs, Mapping):
        raise RunWorkloadProfileUnsupported
    _require_supported_policy_schema_version(policy_schema_version)
    if RUN_WORKLOAD_PROFILE_KWARG not in kwargs:
        raise RunWorkloadProfileUnsupported
    return parse_persisted_run_workload_profile(kwargs[RUN_WORKLOAD_PROFILE_KWARG])[1]


def resolve_admitted_run_workload_profile(
    *,
    requested: RequestedRunWorkloadProfile,
    policy_schema_version: object,
    inherited_effective: EffectiveRunWorkloadProfile | None = None,
) -> EffectiveRunWorkloadProfile:
    if type(requested) is not RequestedRunWorkloadProfile or (inherited_effective is not None and type(inherited_effective) is not EffectiveRunWorkloadProfile):
        raise RunWorkloadProfileUnsupported
    _require_supported_policy_schema_version(policy_schema_version)
    return inherited_effective or EffectiveRunWorkloadProfile(
        name=requested.name,
    )


def freeze_admitted_run_workload_profile(
    kwargs: Mapping[str, object],
    *,
    requested: RequestedRunWorkloadProfile,
    policy_schema_version: object,
    inherited_effective: EffectiveRunWorkloadProfile | None = None,
) -> tuple[EffectiveRunWorkloadProfile, dict[str, object]]:
    if not isinstance(kwargs, Mapping) or RUN_WORKLOAD_PROFILE_KWARG in kwargs:
        raise RunWorkloadProfileUnsupported
    schema_version = _require_supported_policy_schema_version(
        policy_schema_version,
    )
    effective = resolve_admitted_run_workload_profile(
        requested=requested,
        policy_schema_version=schema_version,
        inherited_effective=inherited_effective,
    )
    frozen_kwargs = dict(kwargs)
    frozen_kwargs[RUN_WORKLOAD_PROFILE_KWARG] = persisted_run_workload_profile(requested, effective)
    return effective, frozen_kwargs


__all__ = [
    "EffectiveRunWorkloadProfile",
    "RUN_WORKLOAD_PROFILE_KWARG",
    "RequestedRunWorkloadProfile",
    "RunWorkloadProfileName",
    "RunWorkloadProfileUnsupported",
    "effective_run_workload_profile_from_kwargs",
    "freeze_admitted_run_workload_profile",
    "parse_persisted_run_workload_profile",
    "persisted_run_workload_profile",
    "resolve_admitted_run_workload_profile",
]
