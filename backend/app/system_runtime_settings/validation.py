"""Canonical, bounded and secret-rejecting runtime-policy validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    AuthPolicyValue,
    AutomationsPolicyValue,
    MemoryDocumentPolicy,
    QuotaPolicyValue,
    RuntimePolicySection,
    RuntimePolicyValue,
)
from app.system_runtime_settings.schema_codec import (
    canonical_policy_value_v2,
    canonical_policy_value_v3,
    canonical_policy_value_v4,
    canonical_policy_value_v5,
)

LEGACY_RUNTIME_POLICY_SCHEMA_VERSION = 2
INTERMEDIATE_RUNTIME_POLICY_SCHEMA_VERSION = 3
PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION = 4
SINGLE_LIMIT_RUNTIME_POLICY_SCHEMA_VERSION = 5
RUNTIME_POLICY_SCHEMA_VERSION = 6
MAX_RUNTIME_POLICY_BYTES = 32 * 1024
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|authorization|cookie|secret|private[_-]?key|nonce|ciphertext|storage[_-]?locator)(?:$|[_-])"
)
_SECRET_KEY_SUFFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "clientsecret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "privatekey",
    "nonce",
    "ciphertext",
    "storagelocator",
)
_SECRET_VALUES = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_URL = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


class RuntimePolicyInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("Runtime policy invalid")


@dataclass(frozen=True, slots=True)
class CanonicalRuntimePolicy:
    schema_version: int
    value: dict[str, object]
    checksum: str


def _secret_like_text(value: str) -> bool:
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        return True
    if any(pattern.search(value) for pattern in _SECRET_VALUES):
        return True
    for match in _URL.finditer(value):
        try:
            parsed = urlsplit(match.group(0).rstrip(".,;:"))
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                return True
        except ValueError:
            return True
    return False


def _reject_secret_material(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower()) if type(key) is str else ""
            if type(key) is not str or _SECRET_KEY.search(key) or normalized_key in {"secret", "token"} or any(normalized_key.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES):
                raise RuntimePolicyInvalid
            _reject_secret_material(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_material(item)
    elif isinstance(value, str) and _secret_like_text(value):
        raise RuntimePolicyInvalid


def parse_policy_value(
    section: RuntimePolicySection | str,
    value: RuntimePolicyValue | Mapping[str, object],
) -> RuntimePolicyValue:
    try:
        parsed_section = RuntimePolicySection(section)
        raw: object = (
            value.model_dump(mode="python")
            if isinstance(
                value,
                (
                    AgentRuntimePolicyValue,
                    AuthPolicyValue,
                    AutomationsPolicyValue,
                    MemoryDocumentPolicy,
                    QuotaPolicyValue,
                ),
            )
            else value
        )
        _reject_secret_material(raw)
        if parsed_section is RuntimePolicySection.AGENT_RUNTIME:
            parsed: RuntimePolicyValue = AgentRuntimePolicyValue.model_validate(raw)
        elif parsed_section is RuntimePolicySection.AUTH:
            parsed = AuthPolicyValue.model_validate(raw)
        elif parsed_section is RuntimePolicySection.AUTOMATIONS:
            parsed = AutomationsPolicyValue.model_validate(raw)
        elif parsed_section is RuntimePolicySection.MEMORY_DOCUMENT:
            parsed = MemoryDocumentPolicy.model_validate(raw)
        elif parsed_section is RuntimePolicySection.QUOTAS:
            parsed = QuotaPolicyValue.model_validate(raw)
        else:
            raise RuntimePolicyInvalid
        normalized = parsed.model_dump(mode="json")
        _reject_secret_material(normalized)
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > MAX_RUNTIME_POLICY_BYTES:
            raise RuntimePolicyInvalid
        return parsed
    except RuntimePolicyInvalid:
        raise
    except (TypeError, ValueError, ValidationError):
        raise RuntimePolicyInvalid from None


def canonical_policy_payload(
    section: RuntimePolicySection | str,
    value: RuntimePolicyValue | Mapping[str, object],
) -> CanonicalRuntimePolicy:
    parsed = parse_policy_value(section, value)
    if isinstance(parsed, AgentRuntimePolicyValue):
        identical_calls = parsed.loop_detection.identical_calls
        if identical_calls.window_size < identical_calls.hard_limit:
            raise RuntimePolicyInvalid
    normalized = parsed.model_dump(mode="json")
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return CanonicalRuntimePolicy(
        schema_version=RUNTIME_POLICY_SCHEMA_VERSION,
        value=normalized,
        checksum=hashlib.sha256(encoded).hexdigest(),
    )


def canonical_policy_payload_for_schema(
    section: RuntimePolicySection | str,
    value: RuntimePolicyValue | Mapping[str, object],
    *,
    schema_version: int,
) -> CanonicalRuntimePolicy:
    """Canonicalize a stored payload using its declared schema version.

    Runtime-policy rows are immutable and may already be frozen into Runs.
    Schemas v2 through v5 therefore remain readable through version-owned codecs.
    Schema v2 predates ``vision_bridge``; decoding supplies the safe
    ``model_name=None`` default while checksum verification still uses each
    legacy schema's exact JSON shape.
    """

    if schema_version == RUNTIME_POLICY_SCHEMA_VERSION:
        return canonical_policy_payload(section, value)
    if schema_version in {
        LEGACY_RUNTIME_POLICY_SCHEMA_VERSION,
        INTERMEDIATE_RUNTIME_POLICY_SCHEMA_VERSION,
        PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION,
        SINGLE_LIMIT_RUNTIME_POLICY_SCHEMA_VERSION,
    }:
        try:
            parsed_section = RuntimePolicySection(section)
            raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
            _reject_secret_material(raw)
            canonicalizer = {
                LEGACY_RUNTIME_POLICY_SCHEMA_VERSION: canonical_policy_value_v2,
                INTERMEDIATE_RUNTIME_POLICY_SCHEMA_VERSION: canonical_policy_value_v3,
                PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION: canonical_policy_value_v4,
                SINGLE_LIMIT_RUNTIME_POLICY_SCHEMA_VERSION: canonical_policy_value_v5,
            }[schema_version]
            normalized = canonicalizer(parsed_section, raw)
            _reject_secret_material(normalized)
            encoded = json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            if len(encoded) > MAX_RUNTIME_POLICY_BYTES:
                raise RuntimePolicyInvalid
            return CanonicalRuntimePolicy(
                schema_version=schema_version,
                value=normalized,
                checksum=hashlib.sha256(encoded).hexdigest(),
            )
        except RuntimePolicyInvalid:
            raise
        except (TypeError, ValueError, ValidationError):
            raise RuntimePolicyInvalid from None
    raise RuntimePolicyInvalid


def _decode_legacy_agent_runtime(
    value: Mapping[str, object],
) -> AgentRuntimePolicyValue:
    upgraded = AgentRuntimePolicyValue().model_dump(mode="python")
    for key, item in value.items():
        if key not in {"loop_detection", "subagents"}:
            upgraded[key] = deepcopy(item)

    legacy_loop = value["loop_detection"]
    legacy_subagents = value["subagents"]
    if not isinstance(legacy_loop, Mapping) or not isinstance(legacy_subagents, Mapping):
        raise RuntimePolicyInvalid
    overrides = legacy_loop["tool_freq_overrides"]
    if not isinstance(overrides, Mapping):
        raise RuntimePolicyInvalid
    hard_limits = [legacy_loop["tool_freq_hard_limit"]]
    for limit in overrides.values():
        if not isinstance(limit, Mapping):
            raise RuntimePolicyInvalid
        hard_limits.append(limit["hard_limit"])
    upgraded["loop_detection"] = {
        "enabled": legacy_loop["enabled"],
        "identical_calls": {
            "warn_threshold": legacy_loop["warn_threshold"],
            "hard_limit": legacy_loop["hard_limit"],
            "window_size": legacy_loop["window_size"],
        },
    }
    hard_limit = max(hard_limits)
    upgraded["internal_tool_call_limits"] = {
        "lead_per_run": hard_limit,
        "subagent_per_task": hard_limit,
    }
    upgraded["subagents"] = {
        "max_concurrent": 3,
        "max_total_per_run_by_workload": {
            "interactive": legacy_subagents["max_total_per_run"],
            "research": 9,
        },
    }
    parsed = parse_policy_value(RuntimePolicySection.AGENT_RUNTIME, upgraded)
    if not isinstance(parsed, AgentRuntimePolicyValue):
        raise RuntimePolicyInvalid
    return parsed


def _decode_v4_agent_runtime(
    value: Mapping[str, object],
) -> AgentRuntimePolicyValue:
    upgraded = AgentRuntimePolicyValue().model_dump(mode="python")
    for key, item in value.items():
        if key != "tool_call_budget":
            upgraded[key] = deepcopy(item)

    budget = value["tool_call_budget"]
    if not isinstance(budget, Mapping):
        raise RuntimePolicyInvalid
    profiles = budget["profiles"]
    if not isinstance(profiles, Mapping):
        raise RuntimePolicyInvalid
    hard_limits: list[object] = []
    for profile in profiles.values():
        if not isinstance(profile, Mapping):
            raise RuntimePolicyInvalid
        for role in profile.values():
            if not isinstance(role, Mapping):
                raise RuntimePolicyInvalid
            default = role["default"]
            tools = role["tools"]
            if not isinstance(default, Mapping) or not isinstance(tools, Mapping):
                raise RuntimePolicyInvalid
            hard_limits.append(default["hard_limit"])
            for limit in tools.values():
                if not isinstance(limit, Mapping):
                    raise RuntimePolicyInvalid
                hard_limits.append(limit["hard_limit"])
    hard_limit = max(hard_limits)
    upgraded["internal_tool_call_limits"] = {
        "lead_per_run": hard_limit,
        "subagent_per_task": hard_limit,
    }
    parsed = parse_policy_value(RuntimePolicySection.AGENT_RUNTIME, upgraded)
    if not isinstance(parsed, AgentRuntimePolicyValue):
        raise RuntimePolicyInvalid
    return parsed


def _decode_v5_agent_runtime(
    value: Mapping[str, object],
) -> AgentRuntimePolicyValue:
    upgraded = AgentRuntimePolicyValue().model_dump(mode="python")
    for key, item in value.items():
        if key != "internal_tool_call_limit":
            upgraded[key] = deepcopy(item)
    hard_limit = value["internal_tool_call_limit"]
    upgraded["internal_tool_call_limits"] = {
        "lead_per_run": hard_limit,
        "subagent_per_task": hard_limit,
    }
    parsed = parse_policy_value(RuntimePolicySection.AGENT_RUNTIME, upgraded)
    if not isinstance(parsed, AgentRuntimePolicyValue):
        raise RuntimePolicyInvalid
    return parsed


def decode_policy_value_for_schema(
    section: RuntimePolicySection | str,
    value: RuntimePolicyValue | Mapping[str, object],
    *,
    schema_version: int,
) -> RuntimePolicyValue:
    """Validate one exact stored schema and decode it into the current model."""

    canonical = canonical_policy_payload_for_schema(
        section,
        value,
        schema_version=schema_version,
    )
    try:
        parsed_section = RuntimePolicySection(section)
        if (
            schema_version
            in {
                LEGACY_RUNTIME_POLICY_SCHEMA_VERSION,
                INTERMEDIATE_RUNTIME_POLICY_SCHEMA_VERSION,
            }
            and parsed_section is RuntimePolicySection.AGENT_RUNTIME
        ):
            return _decode_legacy_agent_runtime(canonical.value)
        if schema_version == PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION and parsed_section is RuntimePolicySection.AGENT_RUNTIME:
            return _decode_v4_agent_runtime(canonical.value)
        if schema_version == SINGLE_LIMIT_RUNTIME_POLICY_SCHEMA_VERSION and parsed_section is RuntimePolicySection.AGENT_RUNTIME:
            return _decode_v5_agent_runtime(canonical.value)
        return parse_policy_value(parsed_section, canonical.value)
    except RuntimePolicyInvalid:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RuntimePolicyInvalid from None


__all__ = [
    "CanonicalRuntimePolicy",
    "MAX_RUNTIME_POLICY_BYTES",
    "LEGACY_RUNTIME_POLICY_SCHEMA_VERSION",
    "INTERMEDIATE_RUNTIME_POLICY_SCHEMA_VERSION",
    "PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION",
    "RUNTIME_POLICY_SCHEMA_VERSION",
    "SINGLE_LIMIT_RUNTIME_POLICY_SCHEMA_VERSION",
    "RuntimePolicyInvalid",
    "canonical_policy_payload",
    "canonical_policy_payload_for_schema",
    "decode_policy_value_for_schema",
    "parse_policy_value",
]
