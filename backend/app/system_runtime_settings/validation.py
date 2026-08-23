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
)

LEGACY_RUNTIME_POLICY_SCHEMA_VERSION = 2
PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION = 3
RUNTIME_POLICY_SCHEMA_VERSION = 4
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
    Schemas v2 and v3 therefore remain readable through version-owned codecs.
    Schema v2 predates ``vision_bridge``; decoding supplies the safe
    ``model_name=None`` default while checksum verification still uses each
    legacy schema's exact JSON shape.
    """

    if schema_version == RUNTIME_POLICY_SCHEMA_VERSION:
        return canonical_policy_payload(section, value)
    if schema_version == PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION:
        try:
            parsed_section = RuntimePolicySection(section)
            raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
            _reject_secret_material(raw)
            normalized = canonical_policy_value_v3(parsed_section, raw)
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
                schema_version=PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION,
                value=normalized,
                checksum=hashlib.sha256(encoded).hexdigest(),
            )
        except RuntimePolicyInvalid:
            raise
        except (TypeError, ValueError, ValidationError):
            raise RuntimePolicyInvalid from None
    if schema_version != LEGACY_RUNTIME_POLICY_SCHEMA_VERSION:
        raise RuntimePolicyInvalid
    try:
        parsed_section = RuntimePolicySection(section)
        raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value)
        _reject_secret_material(raw)
        normalized = canonical_policy_value_v2(parsed_section, raw)
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
            schema_version=LEGACY_RUNTIME_POLICY_SCHEMA_VERSION,
            value=normalized,
            checksum=hashlib.sha256(encoded).hexdigest(),
        )
    except RuntimePolicyInvalid:
        raise
    except (TypeError, ValueError, ValidationError):
        raise RuntimePolicyInvalid from None


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
    role_budget = {
        "default": {
            "warn": legacy_loop["tool_freq_warn"],
            "hard_limit": legacy_loop["tool_freq_hard_limit"],
        },
        "tools": {name: deepcopy(limit) for name, limit in overrides.items() if name != "task"},
    }
    upgraded["loop_detection"] = {
        "enabled": legacy_loop["enabled"],
        "identical_calls": {
            "warn_threshold": legacy_loop["warn_threshold"],
            "hard_limit": legacy_loop["hard_limit"],
            "window_size": legacy_loop["window_size"],
        },
    }
    budget = upgraded["tool_call_budget"]
    if not isinstance(budget, dict):
        raise RuntimePolicyInvalid
    profiles = budget["profiles"]
    if not isinstance(profiles, dict):
        raise RuntimePolicyInvalid
    profiles["interactive"] = {
        "lead": deepcopy(role_budget),
        "subagent": deepcopy(role_budget),
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
                PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION,
            }
            and parsed_section is RuntimePolicySection.AGENT_RUNTIME
        ):
            return _decode_legacy_agent_runtime(canonical.value)
        return parse_policy_value(parsed_section, canonical.value)
    except RuntimePolicyInvalid:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RuntimePolicyInvalid from None


__all__ = [
    "CanonicalRuntimePolicy",
    "MAX_RUNTIME_POLICY_BYTES",
    "LEGACY_RUNTIME_POLICY_SCHEMA_VERSION",
    "PREVIOUS_RUNTIME_POLICY_SCHEMA_VERSION",
    "RUNTIME_POLICY_SCHEMA_VERSION",
    "RuntimePolicyInvalid",
    "canonical_policy_payload",
    "canonical_policy_payload_for_schema",
    "decode_policy_value_for_schema",
    "parse_policy_value",
]
