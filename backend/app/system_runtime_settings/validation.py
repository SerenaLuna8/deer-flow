"""Canonical, bounded and secret-rejecting runtime-policy validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    AuthPolicyValue,
    QuotaPolicyValue,
    RuntimePolicySection,
    RuntimePolicyValue,
)

RUNTIME_POLICY_SCHEMA_VERSION = 1
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
        raw: object = value.model_dump(mode="python") if isinstance(value, (AgentRuntimePolicyValue, AuthPolicyValue, QuotaPolicyValue)) else value
        _reject_secret_material(raw)
        if parsed_section is RuntimePolicySection.AGENT_RUNTIME:
            parsed: RuntimePolicyValue = AgentRuntimePolicyValue.model_validate(raw)
        elif parsed_section is RuntimePolicySection.AUTH:
            parsed = AuthPolicyValue.model_validate(raw)
        else:
            parsed = QuotaPolicyValue.model_validate(raw)
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


__all__ = [
    "CanonicalRuntimePolicy",
    "MAX_RUNTIME_POLICY_BYTES",
    "RUNTIME_POLICY_SCHEMA_VERSION",
    "RuntimePolicyInvalid",
    "canonical_policy_payload",
    "parse_policy_value",
]
