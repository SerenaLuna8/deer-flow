"""Canonical integrity contract for immutable Agent payload schemas."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from pydantic import ValidationError

from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetScope,
    SkillAssetRef,
)

_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")


def _canonical_model_settings(
    value: object,
) -> tuple[AgentModelSettings, dict[str, object]]:
    if not isinstance(value, AgentModelSettings):
        raise ValueError("invalid Agent model settings")
    try:
        settings = AgentModelSettings.model_validate(value)
    except (TypeError, ValidationError, ValueError):
        raise ValueError("invalid Agent model settings") from None
    return settings, settings.model_dump(exclude_none=True)


def _validated_payload_parts(
    payload: AgentPayload,
    *,
    payload_schema_version: int | None,
) -> tuple[
    int,
    tuple[str, ...],
    tuple[SkillAssetRef, ...],
    tuple[uuid.UUID, ...],
    dict[str, object],
]:
    if not isinstance(payload, AgentPayload):
        raise ValueError("invalid Agent payload")
    schema_version = payload.payload_schema_version if payload_schema_version is None else payload_schema_version
    if type(schema_version) is not int or schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError("unsupported Agent payload schema version")
    if not all(
        isinstance(value, str)
        for value in (
            payload.description,
            payload.agents_instructions,
            payload.soul,
            payload.identity,
            payload.user_context,
            payload.model_ref,
        )
    ):
        raise ValueError("invalid Agent payload text")
    if not all(
        isinstance(values, tuple)
        for values in (
            payload.tool_groups,
            payload.skill_refs,
            payload.mcp_version_ids,
        )
    ):
        raise ValueError("invalid Agent payload collection")
    tool_groups = payload.tool_groups
    skill_refs = payload.skill_refs
    mcp_version_ids = payload.mcp_version_ids
    if (
        any(not isinstance(value, str) for value in tool_groups)
        or any(not isinstance(value, uuid.UUID) for value in mcp_version_ids)
        or any(not isinstance(value, SkillAssetRef) or not isinstance(value.asset_id, uuid.UUID) or not isinstance(value.scope, AssetScope) for value in skill_refs)
    ):
        raise ValueError("invalid Agent payload collection")

    model_settings, model_settings_document = _canonical_model_settings(payload.model_settings)
    if schema_version in (1, 2) and not model_settings.is_empty:
        raise ValueError("Agent model settings require payload schema version 3")
    return (
        schema_version,
        tool_groups,
        skill_refs,
        mcp_version_ids,
        model_settings_document,
    )


def _checksum(document: dict[str, object]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def agent_payload_checksum(
    payload: AgentPayload,
    *,
    payload_schema_version: int | None = None,
) -> str:
    """Return the persisted SHA-256 contract for Agent payload schema v1-v4.

    The explicit version argument exists for authoring paths that upgrade an
    older in-memory payload to the current schema before persistence. Runtime
    readers omit it and therefore bind the digest to the payload's own stored
    schema version.
    """

    (
        schema_version,
        tool_groups,
        skill_refs,
        mcp_version_ids,
        model_settings_document,
    ) = _validated_payload_parts(
        payload,
        payload_schema_version=payload_schema_version,
    )

    document: dict[str, object] = {
        "description": payload.description,
        "mcp_version_ids": [str(value) for value in mcp_version_ids],
        "model_ref": payload.model_ref,
        "skill_refs": [{"asset_id": str(value.asset_id), "scope": value.scope.value} for value in skill_refs],
        "soul": payload.soul,
        "tool_groups": list(tool_groups),
    }
    if schema_version in (2, 3, 4):
        document.update(
            {
                "agents_instructions": payload.agents_instructions,
                "identity": payload.identity,
                "user_context": payload.user_context,
            }
        )
    if schema_version in (3, 4):
        document["model_settings"] = model_settings_document

    return _checksum(document)


def legacy_agent_payload_checksum(
    payload: AgentPayload,
    skill_version_ids: tuple[uuid.UUID, ...],
) -> str:
    """Recompute an immutable pre-Current Agent digest from frozen Skill IDs."""

    (
        schema_version,
        tool_groups,
        skill_refs,
        mcp_version_ids,
        model_settings_document,
    ) = _validated_payload_parts(
        payload,
        payload_schema_version=None,
    )
    if schema_version not in (1, 2, 3):
        raise ValueError("legacy Agent checksum requires payload schema v1-v3")
    if not isinstance(skill_version_ids, tuple) or any(not isinstance(value, uuid.UUID) for value in skill_version_ids) or len(skill_refs) != len(skill_version_ids):
        raise ValueError("invalid legacy Agent Skill versions")
    document: dict[str, object] = {
        "description": payload.description,
        "mcp_version_ids": [str(value) for value in mcp_version_ids],
        "model_ref": payload.model_ref,
        "skill_version_ids": [str(value) for value in skill_version_ids],
        "soul": payload.soul,
        "tool_groups": list(tool_groups),
    }
    if schema_version in (2, 3):
        document.update(
            {
                "agents_instructions": payload.agents_instructions,
                "identity": payload.identity,
                "user_context": payload.user_context,
            }
        )
    if schema_version == 3:
        document["model_settings"] = model_settings_document
    return _checksum(document)


def resolved_agent_payload_checksum_matches(
    payload: AgentPayload,
    expected_checksum: object,
    *,
    skill_version_ids: tuple[uuid.UUID, ...],
) -> bool:
    """Verify a resolved Agent using the digest contract it was persisted with."""

    if not isinstance(expected_checksum, str):
        return False
    try:
        actual = legacy_agent_payload_checksum(payload, skill_version_ids) if payload.payload_schema_version in (1, 2, 3) else agent_payload_checksum(payload)
    except (TypeError, ValueError):
        return False
    return actual == expected_checksum


def agent_payload_checksum_matches(
    payload: AgentPayload,
    expected_checksum: object,
    *,
    payload_schema_version: int | None = None,
) -> bool:
    """Fail closed when either the payload or stored digest is malformed."""

    if not isinstance(expected_checksum, str):
        return False
    try:
        actual = agent_payload_checksum(
            payload,
            payload_schema_version=payload_schema_version,
        )
    except (TypeError, ValueError):
        return False
    return actual == expected_checksum


def persisted_agent_payload_checksum_matches(
    payload: AgentPayload,
    expected_checksum: object,
) -> bool:
    """Verify current payloads and honor migration-attested legacy digests.

    Schema v1-v3 digests covered exact Skill Version IDs. The lifecycle
    migration validates those original digests before replacing operational
    refs with Skill Asset IDs, then preserves them as immutable audit identity.
    Fresh authoring only writes schema v4, whose digest remains recomputable.
    """

    if payload.payload_schema_version in (1, 2, 3):
        return isinstance(expected_checksum, str) and _CHECKSUM.fullmatch(expected_checksum) is not None
    return agent_payload_checksum_matches(payload, expected_checksum)
