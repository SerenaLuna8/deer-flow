"""Canonical integrity contract for immutable Agent payload schemas."""

from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import ValidationError

from app.shared_assets.models import AgentModelSettings, AgentPayload

_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3})


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


def agent_payload_checksum(
    payload: AgentPayload,
    *,
    payload_schema_version: int | None = None,
) -> str:
    """Return the persisted SHA-256 contract for Agent payload schema v1-v3.

    The explicit version argument exists for authoring paths that upgrade an
    older in-memory payload to the current schema before persistence. Runtime
    readers omit it and therefore bind the digest to the payload's own stored
    schema version.
    """

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
            payload.skill_version_ids,
            payload.mcp_version_ids,
        )
    ):
        raise ValueError("invalid Agent payload collection")
    tool_groups = payload.tool_groups
    skill_version_ids = payload.skill_version_ids
    mcp_version_ids = payload.mcp_version_ids
    if any(not isinstance(value, str) for value in tool_groups) or any(not isinstance(value, uuid.UUID) for values in (skill_version_ids, mcp_version_ids) for value in values):
        raise ValueError("invalid Agent payload collection")

    model_settings, model_settings_document = _canonical_model_settings(payload.model_settings)
    if schema_version in (1, 2) and not model_settings.is_empty:
        raise ValueError("Agent model settings require payload schema version 3")

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

    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


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
