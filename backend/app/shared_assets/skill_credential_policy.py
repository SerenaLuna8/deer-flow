from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.shared_assets.errors import (
    SkillCredentialBindingInvalid,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)

_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_SKILL_CREDENTIAL_BINDINGS = 256
MAX_SKILL_CREDENTIAL_BINDING_NAME_LENGTH = 255


class _CredentialRecord(Protocol):
    id: uuid.UUID
    scope: str
    status: str
    is_delete: bool
    current_version_id: uuid.UUID | None


class _CredentialVersionRecord(Protocol):
    id: uuid.UUID
    credential_id: uuid.UUID
    status: str
    payload_schema: Mapping[str, object]


class SkillCredentialRecord(Protocol):
    credential: _CredentialRecord
    version: _CredentialVersionRecord


@dataclass(frozen=True)
class SkillCredentialBindingInput:
    name: str
    credential_version_id: uuid.UUID
    source_env_field_name: str | None = None

    def __post_init__(self) -> None:
        # Programmatic callers from the previous exact-name contract remain a
        # safe compatibility path. Public HTTP authoring requires the source
        # field explicitly and therefore never relies on this default.
        if self.source_env_field_name is None:
            object.__setattr__(self, "source_env_field_name", self.name)


def parse_secret_requirements(
    raw: object,
    *,
    request_id: str,
) -> tuple[tuple[str, bool], ...]:
    if not isinstance(raw, list):
        raise SkillCredentialBindingInvalid(request_id)
    requirements: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"name", "optional"} or not isinstance(item.get("name"), str) or not isinstance(item.get("optional", False), bool):
            raise SkillCredentialBindingInvalid(request_id)
        name = item["name"]
        optional = item.get("optional", False)
        if _ENV_NAME_PATTERN.fullmatch(name) is None or name in seen:
            raise SkillCredentialBindingInvalid(request_id)
        seen.add(name)
        requirements.append((name, optional))
    return tuple(requirements)


def normalize_binding_inputs(
    bindings: Sequence[SkillCredentialBindingInput],
    *,
    request_id: str,
) -> tuple[SkillCredentialBindingInput, ...]:
    try:
        normalized = tuple(bindings)
    except TypeError:
        raise SkillCredentialBindingInvalid(request_id) from None
    if len(normalized) > MAX_SKILL_CREDENTIAL_BINDINGS:
        raise SkillCredentialBindingInvalid(request_id)
    seen: set[str] = set()
    for item in normalized:
        if (
            not isinstance(item, SkillCredentialBindingInput)
            or not isinstance(item.name, str)
            or len(item.name) > MAX_SKILL_CREDENTIAL_BINDING_NAME_LENGTH
            or _ENV_NAME_PATTERN.fullmatch(item.name) is None
            or not isinstance(item.credential_version_id, uuid.UUID)
            or not isinstance(item.source_env_field_name, str)
            or not item.source_env_field_name
            or len(item.source_env_field_name) > MAX_SKILL_CREDENTIAL_BINDING_NAME_LENGTH
            or item.name in seen
        ):
            raise SkillCredentialBindingInvalid(request_id)
        seen.add(item.name)
    return normalized


def credential_is_eligible(
    record: SkillCredentialRecord,
    source_env_field_name: str,
) -> bool:
    env = record.version.payload_schema.get("env")
    return (
        bool(source_env_field_name)
        and len(source_env_field_name) <= MAX_SKILL_CREDENTIAL_BINDING_NAME_LENGTH
        and record.credential.scope == "project"
        and record.credential.status == "active"
        and not record.credential.is_delete
        and record.credential.current_version_id == record.version.id
        and record.version.status == "active"
        and isinstance(env, list)
        and all(isinstance(item, str) for item in env)
        and source_env_field_name in env
    )


def validate_selected_credential(
    record: SkillCredentialRecord,
    source_env_field_name: str,
    *,
    active_envelope: bool,
    request_id: str,
) -> None:
    if record.credential.scope != "project" or record.credential.status != "active" or record.credential.is_delete or record.credential.current_version_id != record.version.id or record.version.status != "active" or not active_envelope:
        raise SkillCredentialSelectionStale(request_id)
    env = record.version.payload_schema.get("env")
    if not isinstance(env, list) or not all(isinstance(item, str) for item in env) or source_env_field_name not in env:
        raise SkillCredentialBindingInvalid(request_id)


def require_declared_binding_names(
    requirements: Sequence[tuple[str, bool]],
    bindings: Sequence[SkillCredentialBindingInput],
    *,
    request_id: str,
) -> None:
    declared = {name for name, _optional in requirements}
    if any(item.name not in declared for item in bindings):
        raise SkillCredentialBindingInvalid(request_id)


def require_complete_bindings(
    requirements: Sequence[tuple[str, bool]],
    *,
    configured_names: frozenset[str],
    request_id: str,
) -> None:
    if any(not optional and name not in configured_names for name, optional in requirements):
        raise SkillCredentialBindingsIncomplete(request_id)
