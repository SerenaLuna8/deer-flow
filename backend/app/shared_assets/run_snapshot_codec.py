from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from pydantic import ValidationError

from app.shared_assets.agent_payload_checksum import (
    resolved_agent_payload_checksum_matches,
)
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedAssetSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
)

RUN_ASSET_SNAPSHOT_SCHEMA_VERSION = 1
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")


class RunAssetSnapshotInvalid(ValueError):
    pass


def encode_run_asset_snapshot(snapshot: ResolvedAssetSnapshot) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
        "kind": snapshot.kind.value,
        "scope": snapshot.scope.value,
        "asset_id": str(snapshot.asset_id),
        "version_id": str(snapshot.version_id),
        "checksum": snapshot.checksum,
        "catalog_generation": snapshot.catalog_generation,
        "dependency_version_ids": [str(value) for value in snapshot.dependency_version_ids],
    }
    if type(snapshot) is ResolvedAgentSnapshot:
        payload = snapshot.payload
        base["agent"] = {
            "slug": snapshot.slug,
            "source_key": snapshot.source_key,
            "description": payload.description,
            "agents_instructions": payload.agents_instructions,
            "soul": payload.soul,
            "identity": payload.identity,
            "user_context": payload.user_context,
            "model_ref": payload.model_ref,
            "model_settings": payload.model_settings.model_dump(mode="json"),
            "tool_groups": list(payload.tool_groups),
            "skill_refs": [{"scope": item.scope.value, "asset_id": str(item.asset_id)} for item in payload.skill_refs],
            "mcp_version_ids": [str(value) for value in payload.mcp_version_ids],
            "payload_schema_version": payload.payload_schema_version,
            "resolved_skill_version_ids": [str(value) for value in snapshot.skill_version_ids],
        }
    elif type(snapshot) is ResolvedSkillSnapshot:
        base["skill"] = {
            "files": [
                {
                    "path": item.path,
                    "media_type": item.media_type,
                    "content_base64": base64.b64encode(item.content).decode("ascii"),
                }
                for item in snapshot.files
            ],
            "secret_requirements": [{"name": item.name, "optional": item.optional} for item in snapshot.secret_requirements],
        }
    elif type(snapshot) is ResolvedMcpSnapshot:
        base["mcp"] = {
            "definition": _json_value(snapshot.definition),
            "secret_generation_ids": [str(value) for value in snapshot.secret_generation_ids],
            "secret_digest": snapshot.secret_digest,
        }
    else:
        raise RunAssetSnapshotInvalid("unsupported Run asset snapshot")
    return base


def decode_run_asset_snapshot(value: Mapping[str, object]) -> ResolvedAssetSnapshot:
    try:
        if set(value) != {
            "schema_version",
            "kind",
            "scope",
            "asset_id",
            "version_id",
            "checksum",
            "catalog_generation",
            "dependency_version_ids",
            AssetKind(str(value["kind"])).value,
        }:
            raise RunAssetSnapshotInvalid("Run asset snapshot shape is invalid")
        if value["schema_version"] != RUN_ASSET_SNAPSHOT_SCHEMA_VERSION:
            raise RunAssetSnapshotInvalid("Run asset snapshot schema is unsupported")
        kind = AssetKind(str(value["kind"]))
        scope = AssetScope(str(value["scope"]))
        asset_id = uuid.UUID(str(value["asset_id"]))
        version_id = uuid.UUID(str(value["version_id"]))
        checksum = str(value["checksum"])
        generation = value["catalog_generation"]
        if _CHECKSUM.fullmatch(checksum) is None or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise RunAssetSnapshotInvalid("Run asset snapshot metadata is invalid")
        dependency_ids = _uuids(value["dependency_version_ids"])
        common = dict(
            kind=kind,
            scope=scope,
            asset_id=asset_id,
            version_id=version_id,
            checksum=checksum,
            catalog_generation=generation,
            dependency_version_ids=dependency_ids,
        )
        if kind is AssetKind.AGENT:
            raw = _mapping(value["agent"])
            if set(raw) != {
                "description",
                "agents_instructions",
                "soul",
                "identity",
                "user_context",
                "model_ref",
                "model_settings",
                "tool_groups",
                "skill_refs",
                "mcp_version_ids",
                "payload_schema_version",
                "resolved_skill_version_ids",
                "slug",
                "source_key",
            }:
                raise RunAssetSnapshotInvalid("Run Agent snapshot shape is invalid")
            schema_version = raw["payload_schema_version"]
            if not isinstance(schema_version, int) or isinstance(schema_version, bool):
                raise RunAssetSnapshotInvalid("Run Agent schema is invalid")
            skill_refs = tuple(
                SkillAssetRef(
                    AssetScope(str(item["scope"])),
                    uuid.UUID(str(item["asset_id"])),
                )
                for item in _mapping_sequence(raw["skill_refs"], {"scope", "asset_id"})
            )
            payload = AgentPayload(
                description=_string(raw["description"]),
                agents_instructions=_string(raw["agents_instructions"]),
                soul=_string(raw["soul"]),
                identity=_string(raw["identity"]),
                user_context=_string(raw["user_context"]),
                model_ref=_string(raw["model_ref"]),
                model_settings=AgentModelSettings.model_validate(_mapping(raw["model_settings"])),
                tool_groups=_strings(raw["tool_groups"]),
                skill_refs=skill_refs,
                mcp_version_ids=_uuids(raw["mcp_version_ids"]),
                payload_schema_version=schema_version,
            )
            resolved_skill_ids = _uuids(raw["resolved_skill_version_ids"])
            if not resolved_agent_payload_checksum_matches(
                payload,
                checksum,
                skill_version_ids=resolved_skill_ids,
            ):
                raise RunAssetSnapshotInvalid("Run Agent snapshot checksum is invalid")
            return ResolvedAgentSnapshot(
                **common,
                payload=payload,
                skill_version_ids=resolved_skill_ids,
                slug=_string(raw["slug"]),
                source_key=(None if raw["source_key"] is None else _string(raw["source_key"])),
            )
        if kind is AssetKind.SKILL:
            raw = _mapping(value["skill"])
            if set(raw) != {"files", "secret_requirements"}:
                raise RunAssetSnapshotInvalid("Run Skill snapshot shape is invalid")
            files = tuple(
                SkillArchiveFile(
                    path=_string(item["path"]),
                    media_type=_string(item["media_type"]),
                    content=base64.b64decode(
                        _string(item["content_base64"]),
                        validate=True,
                    ),
                )
                for item in _mapping_sequence(
                    raw["files"],
                    {"path", "media_type", "content_base64"},
                )
            )
            if _skill_checksum(files) != checksum:
                raise RunAssetSnapshotInvalid("Run Skill snapshot checksum is invalid")
            requirements: list[SkillSecretRequirementSnapshot] = []
            raw_requirements = raw["secret_requirements"]
            if isinstance(raw_requirements, list) and all(isinstance(item, str) for item in raw_requirements):
                # An unpublished intermediate snapshot shape omitted the
                # optional bit. Preserve admitted references without turning an
                # unknown optional target into a new required execution gate.
                requirements.extend(SkillSecretRequirementSnapshot(item, True) for item in raw_requirements)
            else:
                for item in _mapping_sequence(
                    raw_requirements,
                    {"name", "optional"},
                ):
                    if not isinstance(item["optional"], bool):
                        raise RunAssetSnapshotInvalid(
                            "Run Skill secret requirement is invalid",
                        )
                    requirements.append(
                        SkillSecretRequirementSnapshot(
                            name=_string(item["name"]),
                            optional=item["optional"],
                        )
                    )
            return ResolvedSkillSnapshot(
                **common,
                files=files,
                secret_requirements=tuple(requirements),
            )
        raw = _mapping(value["mcp"])
        if set(raw) != {"definition", "secret_generation_ids", "secret_digest"}:
            raise RunAssetSnapshotInvalid("Run MCP snapshot shape is invalid")
        definition = _mapping(raw["definition"])
        if _mcp_checksum(definition) != checksum:
            raise RunAssetSnapshotInvalid("Run MCP snapshot checksum is invalid")
        return ResolvedMcpSnapshot(
            **common,
            definition=_freeze_json(definition),
            secret_generation_ids=_uuids(raw["secret_generation_ids"]),
            secret_digest=_digest(raw["secret_digest"]),
        )
    except RunAssetSnapshotInvalid:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise RunAssetSnapshotInvalid("Run asset snapshot is invalid") from error


def _skill_checksum(files: Sequence[SkillArchiveFile]) -> str:
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": hashlib.sha256(item.content).hexdigest(),
                "size_bytes": len(item.content),
            }
            for item in files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _mcp_checksum(definition: Mapping[str, object]) -> str:
    expected = {
        "args",
        "command",
        "secret_slots",
        "description",
        "env",
        "headers",
        "oauth",
        "routing",
        "timeout_seconds",
        "tool_overrides",
        "transport",
        "url",
    }
    if set(definition) != expected:
        raise RunAssetSnapshotInvalid("Run MCP definition shape is invalid")
    slots = _mapping_sequence(
        definition["secret_slots"],
        {"name", "payload_schema", "purpose", "required"},
    )
    canonical = {
        "args": list(_strings(definition["args"])),
        "command": definition["command"],
        "secret_slots": [
            {
                "name": _string(slot["name"]),
                "payload_schema": _mapping(slot["payload_schema"]),
                "purpose": _string(slot["purpose"]),
                "required": slot["required"],
            }
            for slot in slots
        ],
        "description": _string(definition["description"]),
        "env": _mapping(definition["env"]),
        "headers": _mapping(definition["headers"]),
        "oauth": _mapping(definition["oauth"]),
        "routing": _mapping(definition["routing"]),
        "timeout_seconds": definition["timeout_seconds"],
        "tool_overrides": _mapping(definition["tool_overrides"]),
        "transport": _string(definition["transport"]),
        "url": definition["url"],
    }
    if (
        (canonical["command"] is not None and not isinstance(canonical["command"], str))
        or (canonical["url"] is not None and not isinstance(canonical["url"], str))
        or not isinstance(canonical["timeout_seconds"], int)
        or isinstance(canonical["timeout_seconds"], bool)
        or any(not isinstance(slot["required"], bool) for slot in canonical["secret_slots"])
    ):
        raise RunAssetSnapshotInvalid("Run MCP definition is invalid")
    encoded = json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise RunAssetSnapshotInvalid("Run snapshot contains a non-JSON value")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_freeze_json(item) for item in value)
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RunAssetSnapshotInvalid("Run snapshot object is invalid")
    return value


def _mapping_sequence(
    value: object,
    keys: set[str],
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise RunAssetSnapshotInvalid("Run snapshot list is invalid")
    result = tuple(_mapping(item) for item in value)
    if any(set(item) != keys for item in result):
        raise RunAssetSnapshotInvalid("Run snapshot list item is invalid")
    return result


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise RunAssetSnapshotInvalid("Run snapshot string is invalid")
    return value


def _digest(value: object) -> str:
    result = _string(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise RunAssetSnapshotInvalid("Run snapshot digest is invalid")
    return result


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RunAssetSnapshotInvalid("Run snapshot string list is invalid")
    return tuple(value)


def _uuids(value: object) -> tuple[uuid.UUID, ...]:
    if not isinstance(value, list):
        raise RunAssetSnapshotInvalid("Run snapshot UUID list is invalid")
    return tuple(uuid.UUID(str(item)) for item in value)
