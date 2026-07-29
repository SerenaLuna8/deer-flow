from __future__ import annotations

import asyncio
import logging
import math
import posixpath
import re
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal, Union

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.authorization import (
    PrivateRunAuthorizationBoundary,
    PrivateRunAuthorizationService,
)
from app.private_work.context import PrivateWorkContext, require_issued_private_work_context
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_admission import AdmittedPrivateRun
from app.private_work.run_repository import PrivateRunRepository
from app.private_work.snapshot_repository import RunSnapshotAssetStale, RunSnapshotRepository
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context_in_transaction
from app.shared_assets.crypto import (
    CredentialDecryptFailed,
    EncryptedEnvelope,
    decrypt_credential_payload,
)
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.models import (
    AgentModelSettings,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.skill_credential_closure import (
    SkillCredentialClosureInvalid,
    SkillCredentialClosureTarget,
    lock_skill_credential_closures,
)
from deerflow.agents.lead_agent.prompt import AgentPromptBundle
from deerflow.mcp.http_security import SecureMcpHttpClientFactory
from deerflow.mcp_definition_policy import (
    McpDefinitionPolicyError,
    McpEndpointPolicy,
    validate_project_mcp_definition,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import Skill, SkillCategory
from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool

logger = logging.getLogger(__name__)

_PRIVATE_SKILL_CLEANUP_ATTEMPTS = 3
_RUNTIME_SKILL_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 15
_DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS = 60
_MCP_CLOSE_TIMEOUT_SECONDS = 1
_MAX_MCP_TOOLS_PER_SERVER = 128
_MAX_MCP_SCHEMA_DEPTH = 12
_MAX_MCP_SCHEMA_NODES = 2_048
_MAX_MCP_SCHEMA_MAPPING_ENTRIES = 256
_MAX_MCP_SCHEMA_SEQUENCE_ITEMS = 256
_MAX_MCP_SCHEMA_STRING_LENGTH = 16_384
_MAX_MCP_SCHEMA_TOTAL_STRING_LENGTH = 262_144
_MCP_OPTIONAL_FIELD_MISSING = object()
_MCP_SCHEMA_RESERVED_FIELDS = frozenset(
    {
        "model_computed_fields",
        "model_config",
        "model_extra",
        "model_fields",
        "model_fields_set",
    }
)
_MCP_SCHEMA_FORBIDDEN_KEYS = frozenset(
    {
        "$dynamicRef",
        "$recursiveRef",
        "allOf",
        "definitions",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "pattern",
        "patternProperties",
        "propertyNames",
        "then",
        "unevaluatedProperties",
    }
)
_MCP_SECRET_SUBSTRING_MIN_LENGTH = 8


class PrivateRuntimeCleanupError(RuntimeError):
    """Stable internal error for a run-owned temporary tree left behind."""


def _validated_mcp_runtime_timeout(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 300:
        raise ValueError("invalid private MCP runtime timeout")
    return value


@dataclass(slots=True)
class _McpSchemaBudget:
    nodes: int = 0
    string_characters: int = 0


def _bounded_mcp_schema_copy(
    value: object,
    *,
    budget: _McpSchemaBudget,
    active: set[int],
    depth: int = 0,
) -> object:
    """Copy one untrusted JSON schema under strict structural limits."""

    if depth > _MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("MCP tool schema is too deep")
    budget.nodes += 1
    if budget.nodes > _MAX_MCP_SCHEMA_NODES:
        raise ValueError("MCP tool schema is too large")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP tool schema contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_MCP_SCHEMA_STRING_LENGTH:
            raise ValueError("MCP tool schema string is too long")
        budget.string_characters += len(value)
        if budget.string_characters > _MAX_MCP_SCHEMA_TOTAL_STRING_LENGTH:
            raise ValueError("MCP tool schema strings are too large")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active or len(value) > _MAX_MCP_SCHEMA_MAPPING_ENTRIES:
            raise ValueError("MCP tool schema mapping is invalid")
        active.add(identity)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_MCP_SCHEMA_STRING_LENGTH or key in _MCP_SCHEMA_FORBIDDEN_KEYS:
                    raise ValueError("MCP tool schema key is invalid")
                copied[key] = _bounded_mcp_schema_copy(
                    item,
                    budget=budget,
                    active=active,
                    depth=depth + 1,
                )
            return copied
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active or len(value) > _MAX_MCP_SCHEMA_SEQUENCE_ITEMS:
            raise ValueError("MCP tool schema sequence is invalid")
        active.add(identity)
        try:
            return [
                _bounded_mcp_schema_copy(
                    item,
                    budget=budget,
                    active=active,
                    depth=depth + 1,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    raise ValueError("MCP tool schema is not JSON")


def _decode_mcp_schema_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise ValueError("MCP tool schema reference is invalid")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _resolve_mcp_schema_refs(
    schema: Mapping[str, object],
) -> Mapping[str, object]:
    """Expand bounded local ``$defs`` references and reject recursive refs."""

    budget = _McpSchemaBudget()

    def resolve_pointer(reference: object) -> Mapping[str, object]:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise ValueError("MCP tool schema reference is invalid")
        current: object = schema
        for raw_token in reference[2:].split("/"):
            token = _decode_mcp_schema_pointer_token(raw_token)
            if not isinstance(current, Mapping) or token not in current:
                raise ValueError("MCP tool schema reference is invalid")
            current = current[token]
        if not isinstance(current, Mapping):
            raise ValueError("MCP tool schema reference is invalid")
        return current

    def expand(
        value: object,
        *,
        active_references: set[str],
        depth: int,
    ) -> object:
        if depth > _MAX_MCP_SCHEMA_DEPTH:
            raise ValueError("MCP tool schema reference is too deep")
        budget.nodes += 1
        if budget.nodes > _MAX_MCP_SCHEMA_NODES:
            raise ValueError("MCP tool schema reference expansion is too large")
        if isinstance(value, Mapping):
            if "$ref" in value:
                if set(value) != {"$ref"}:
                    raise ValueError("MCP tool schema reference is invalid")
                reference = value["$ref"]
                if not isinstance(reference, str) or reference in active_references:
                    raise ValueError("MCP tool schema reference is recursive")
                return expand(
                    resolve_pointer(reference),
                    active_references={*active_references, reference},
                    depth=depth + 1,
                )
            return {
                key: expand(
                    item,
                    active_references=active_references,
                    depth=depth + 1,
                )
                for key, item in value.items()
                if key != "$defs"
            }
        if isinstance(value, list):
            return [
                expand(
                    item,
                    active_references=active_references,
                    depth=depth + 1,
                )
                for item in value
            ]
        return value

    resolved = expand(schema, active_references=set(), depth=0)
    if not isinstance(resolved, Mapping):
        raise ValueError("MCP tool schema is invalid")
    return resolved


def _valid_mcp_schema_field_name(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= 128 and not name.startswith("_") and name not in _MCP_SCHEMA_RESERVED_FIELDS and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in name)


def _mcp_schema_literal(values: object) -> object:
    if not isinstance(values, list) or not values or len(values) > _MAX_MCP_SCHEMA_SEQUENCE_ITEMS or any(value is not None and not isinstance(value, (str, bool, int, float)) for value in values):
        raise ValueError("MCP tool enum is invalid")
    return Literal.__getitem__(tuple(values))


def _mcp_schema_union(annotations: list[object]) -> object:
    if not annotations or len(annotations) > 16:
        raise ValueError("MCP tool union is invalid")
    unique: list[object] = []
    for annotation in annotations:
        if annotation not in unique:
            unique.append(annotation)
    if len(unique) == 1:
        return unique[0]
    return Union.__getitem__(tuple(unique))


def _mcp_schema_field(
    schema: Mapping[str, object],
    *,
    required: bool,
) -> object:
    field_kwargs: dict[str, object] = {}
    description = schema.get("description")
    title = schema.get("title")
    if description is not None:
        if not isinstance(description, str):
            raise ValueError("MCP tool field description is invalid")
        field_kwargs["description"] = description
    if title is not None:
        if not isinstance(title, str):
            raise ValueError("MCP tool field title is invalid")
        field_kwargs["title"] = title

    schema_type = schema.get("type")
    if schema_type == "string":
        for source, target in (
            ("minLength", "min_length"),
            ("maxLength", "max_length"),
        ):
            value = schema.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("MCP tool string constraint is invalid")
                field_kwargs[target] = value
    elif schema_type in {"integer", "number"}:
        for source, target in (
            ("minimum", "ge"),
            ("maximum", "le"),
            ("exclusiveMinimum", "gt"),
            ("exclusiveMaximum", "lt"),
            ("multipleOf", "multiple_of"),
        ):
            value = schema.get(source)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError("MCP tool number constraint is invalid")
                field_kwargs[target] = value
    elif schema_type == "array":
        for source, target in (
            ("minItems", "min_length"),
            ("maxItems", "max_length"),
        ):
            value = schema.get(source)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("MCP tool array constraint is invalid")
                field_kwargs[target] = value

    if required:
        return Field(..., **field_kwargs)
    return Field(
        default_factory=lambda: _MCP_OPTIONAL_FIELD_MISSING,
        exclude_if=lambda value: value is _MCP_OPTIONAL_FIELD_MISSING,
        **field_kwargs,
    )


def _mcp_schema_annotation(
    schema: Mapping[str, object],
    *,
    model_name: str,
    depth: int = 0,
) -> object:
    if depth > _MAX_MCP_SCHEMA_DEPTH:
        raise ValueError("MCP tool schema is too deep")
    if "const" in schema:
        return _mcp_schema_literal([schema["const"]])
    if "enum" in schema:
        return _mcp_schema_literal(schema["enum"])

    unions = [key for key in ("anyOf", "oneOf") if key in schema]
    if len(unions) > 1:
        raise ValueError("MCP tool schema union is ambiguous")
    if unions:
        choices = schema[unions[0]]
        if not isinstance(choices, list):
            raise ValueError("MCP tool schema union is invalid")
        annotation = _mcp_schema_union(
            [
                _mcp_schema_annotation(
                    choice,
                    model_name=f"{model_name}Choice{index}",
                    depth=depth + 1,
                )
                for index, choice in enumerate(choices)
                if isinstance(choice, Mapping)
            ]
        )
        if len(choices) == 0 or any(not isinstance(choice, Mapping) for choice in choices):
            raise ValueError("MCP tool schema union is invalid")
        return annotation

    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        return _mcp_schema_union(
            [
                _mcp_schema_annotation(
                    {**schema, "type": candidate},
                    model_name=f"{model_name}Type{index}",
                    depth=depth + 1,
                )
                for index, candidate in enumerate(raw_type)
            ]
        )
    if raw_type is None and ("properties" in schema or "additionalProperties" in schema):
        raw_type = "object"
    if raw_type is None:
        return Any
    if raw_type == "string":
        return str
    if raw_type == "integer":
        return int
    if raw_type == "number":
        return float
    if raw_type == "boolean":
        return bool
    if raw_type == "null":
        return type(None)
    if raw_type == "array":
        items = schema.get("items", {})
        if not isinstance(items, Mapping):
            raise ValueError("MCP tool array items are invalid")
        return list[
            _mcp_schema_annotation(
                items,
                model_name=f"{model_name}Item",
                depth=depth + 1,
            )
        ]
    if raw_type != "object":
        raise ValueError("MCP tool schema type is invalid")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_properties = schema.get("additionalProperties", True)
    if (
        not isinstance(properties, Mapping)
        or len(properties) > _MAX_MCP_SCHEMA_MAPPING_ENTRIES
        or not isinstance(required, list)
        or len(required) != len(set(required))
        or any(not isinstance(name, str) for name in required)
        or not (isinstance(additional_properties, bool) or isinstance(additional_properties, Mapping))
    ):
        raise ValueError("MCP tool object schema is invalid")
    additional_annotation: object | None = None
    if isinstance(additional_properties, Mapping):
        additional_annotation = _mcp_schema_annotation(
            additional_properties,
            model_name=f"{model_name}AdditionalValue",
            depth=depth + 1,
        )
    property_names = set(properties)
    if not set(required).issubset(property_names):
        raise ValueError("MCP tool required fields are invalid")

    fields_by_name: dict[str, tuple[object, object]] = {}
    for index, (field_name, field_schema) in enumerate(properties.items()):
        if not _valid_mcp_schema_field_name(field_name) or not isinstance(
            field_schema,
            Mapping,
        ):
            raise ValueError("MCP tool field is invalid")
        assert isinstance(field_name, str)
        annotation = _mcp_schema_annotation(
            field_schema,
            model_name=f"{model_name}Field{index}",
            depth=depth + 1,
        )
        fields_by_name[field_name] = (
            annotation,
            _mcp_schema_field(
                field_schema,
                required=field_name in required,
            ),
        )

    if additional_annotation is not None:
        fields_by_name["__pydantic_extra__"] = (
            dict[str, additional_annotation],
            Field(init=False),
        )

    return create_model(
        model_name,
        __config__=ConfigDict(
            arbitrary_types_allowed=False,
            extra="forbid" if additional_properties is False else "allow",
        ),
        **fields_by_name,
    )


def _safe_mcp_args_model(
    schema: Mapping[object, object],
    *,
    model_name: str,
) -> type[BaseModel]:
    copied = _bounded_mcp_schema_copy(
        schema,
        budget=_McpSchemaBudget(),
        active=set(),
    )
    if not isinstance(copied, Mapping):
        raise ValueError("MCP tool schema is invalid")
    resolved = _resolve_mcp_schema_refs(copied)
    model = _mcp_schema_annotation(resolved, model_name=model_name)
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("MCP tool root schema must be an object")
    return model


def _remove_private_skill_tree(root: Path) -> None:
    """Remove one private Skill tree with bounded, retryable semantics."""

    for attempt in range(_PRIVATE_SKILL_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 == _PRIVATE_SKILL_CLEANUP_ATTEMPTS:
                raise PrivateRuntimeCleanupError("Private runtime cleanup failed") from None


def _create_private_skill_root(run_id: str, request_id: str) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:80]
    try:
        return Path(tempfile.mkdtemp(prefix=f"deerflow-private-{safe_run_id}-")).resolve()
    except OSError:
        raise PrivateWorkUnavailable(request_id) from None


@dataclass(frozen=True, slots=True)
class PrivateSkillManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    relative_root: str


@dataclass(frozen=True, slots=True)
class PrivateMcpManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    definition: dict[str, object]


@dataclass(frozen=True, slots=True, repr=False)
class PrivateAgentManifest:
    agent_asset_id: uuid.UUID
    agent_version_id: uuid.UUID
    checksum: str
    catalog_generation: int
    description: str
    payload_schema_version: int
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skills: tuple[PrivateSkillManifest, ...]
    mcps: tuple[PrivateMcpManifest, ...]
    model_settings: AgentModelSettings = AgentModelSettings()

    def __repr__(self) -> str:
        return (
            "PrivateAgentManifest("
            f"agent_asset_id={self.agent_asset_id!r}, "
            f"agent_version_id={self.agent_version_id!r}, "
            f"checksum={self.checksum!r}, "
            f"catalog_generation={self.catalog_generation!r}, "
            f"payload_schema_version={self.payload_schema_version!r}, "
            f"model_ref={self.model_ref!r}, "
            f"tool_groups={self.tool_groups!r}, "
            f"skill_count={len(self.skills)!r}, "
            f"mcp_count={len(self.mcps)!r})"
        )


class _EmptyMcpArgs(BaseModel):
    pass


@dataclass(frozen=True, slots=True)
class _DiscoveredMcpTool:
    version_id: uuid.UUID
    name: str
    description: str
    args_schema: type[BaseModel]
    routing: dict[str, object] | None = None


def _safe_copy(value: object) -> object:
    """Copy only the resolver's JSON-like, secret-free MCP definition.

    Credential schema *field names* such as ``client_secret`` and ``key_id``
    describe required input; they are not credential material.  The M3
    resolver owns the plaintext boundary and deliberately excludes envelopes
    and decrypted payloads from this definition, so this copy validates shape
    instead of guessing secrecy from key names.
    """

    if isinstance(value, Mapping):
        return {str(key): _safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_safe_copy(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise RunSnapshotAssetStale


def _private_agent_manifest(
    agent: ResolvedAgentSnapshot,
    *,
    skills: tuple[PrivateSkillManifest, ...],
    mcps: tuple[PrivateMcpManifest, ...],
) -> PrivateAgentManifest:
    """Build the secret-free runtime manifest from one exact snapshot."""

    return PrivateAgentManifest(
        agent_asset_id=agent.asset_id,
        agent_version_id=agent.version_id,
        checksum=agent.checksum,
        catalog_generation=agent.catalog_generation,
        description=agent.payload.description,
        payload_schema_version=agent.payload.payload_schema_version,
        agents_instructions=agent.payload.agents_instructions,
        soul=agent.payload.soul,
        identity=agent.payload.identity,
        user_context=agent.payload.user_context,
        model_ref=agent.payload.model_ref,
        model_settings=agent.payload.model_settings,
        tool_groups=agent.payload.tool_groups,
        skills=skills,
        mcps=mcps,
    )


def _write_skill_tree(
    root: Path,
    skill_snapshots: tuple[ResolvedSkillSnapshot, ...],
) -> tuple[tuple[PrivateSkillManifest, ...], tuple[Skill, ...]]:
    manifests: list[PrivateSkillManifest] = []
    skills: list[Skill] = []
    runtime_names: set[str] = set()
    staging_root = root / ".staging"
    staging_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    for snapshot in skill_snapshots:
        staged_skill_root = staging_root / snapshot.asset_id.hex
        staged_skill_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        for archive_file in snapshot.files:
            relative = Path(archive_file.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise RunSnapshotAssetStale
            destination = (staged_skill_root / relative).resolve()
            if staged_skill_root.resolve() not in destination.parents:
                raise RunSnapshotAssetStale
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(archive_file.content)
            destination.chmod(0o600)
        parsed = parse_skill_file(
            staged_skill_root / "SKILL.md",
            SkillCategory.CUSTOM,
            Path(snapshot.asset_id.hex),
        )
        if parsed is None or _RUNTIME_SKILL_NAME.fullmatch(parsed.name) is None or parsed.name in runtime_names:
            raise RunSnapshotAssetStale
        runtime_names.add(parsed.name)
        category = SkillCategory.PUBLIC if snapshot.scope is AssetScope.SYSTEM else SkillCategory.CUSTOM
        relative_root = parsed.name if category is SkillCategory.PUBLIC else snapshot.asset_id.hex
        skill_root = root / category.value / relative_root
        skill_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if skill_root.exists():
            raise RunSnapshotAssetStale
        staged_skill_root.rename(skill_root)
        manifests.append(
            PrivateSkillManifest(
                asset_id=snapshot.asset_id,
                version_id=snapshot.version_id,
                relative_root=relative_root,
            )
        )
        skills.append(
            replace(
                parsed,
                skill_dir=skill_root,
                skill_file=skill_root / "SKILL.md",
                relative_path=Path(relative_root),
                category=category,
                enabled=True,
                runtime_read_only=True,
            )
        )
    staging_root.rmdir()
    return tuple(manifests), tuple(skills)


class PrivateAgentRuntime:
    """Run-owned exact assets.  Its repr and public manifest are secret-free."""

    __slots__ = (
        "_authorization_boundary",
        "_closed",
        "_closing",
        "_context",
        "_discovery_timeout_seconds",
        "_endpoint_policy",
        "_http_client_factory",
        "_mcp_snapshots",
        "_mcp_tools",
        "_resolver",
        "_run_id",
        "_session_factory",
        "_tool_call_timeout_seconds",
        "safe_manifest",
        "skill_root",
        "skills",
    )

    def __init__(
        self,
        *,
        context: PrivateWorkContext,
        run_id: str,
        resolver: ProjectAssetResolver,
        session_factory: async_sessionmaker[AsyncSession],
        safe_manifest: PrivateAgentManifest,
        skill_root: Path,
        skills: tuple[Skill, ...],
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...],
        authorization_boundary: object,
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._context = context
        self._run_id = run_id
        self._resolver = resolver
        self._session_factory = session_factory
        self._mcp_snapshots = mcp_snapshots
        self._authorization_boundary = authorization_boundary
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        self._tool_call_timeout_seconds = _validated_mcp_runtime_timeout(tool_call_timeout_seconds)
        self._closed = False
        self._closing = False
        self.safe_manifest = safe_manifest
        self.skill_root = skill_root
        self.skills = skills
        self._mcp_tools: tuple[StructuredTool, ...] = ()

    def __repr__(self) -> str:
        return f"PrivateAgentRuntime(run_id={self._run_id!r}, agent_version_id={self.agent_version_id!r}, closed={self._closed!r})"

    @property
    def agent_version_id(self) -> uuid.UUID:
        return self.safe_manifest.agent_version_id

    @property
    def model_ref(self) -> str:
        return self.safe_manifest.model_ref

    @property
    def model_settings(self) -> AgentModelSettings:
        return self.safe_manifest.model_settings

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def soul(self) -> str:
        return self.safe_manifest.soul

    @property
    def prompt_bundle(self) -> AgentPromptBundle:
        return AgentPromptBundle(
            payload_schema_version=self.safe_manifest.payload_schema_version,
            agents_instructions=self.safe_manifest.agents_instructions,
            soul=self.safe_manifest.soul,
            identity=self.safe_manifest.identity,
            user_context=self.safe_manifest.user_context,
        )

    @property
    def tool_groups(self) -> tuple[str, ...]:
        return self.safe_manifest.tool_groups

    @property
    def mcp_definitions(self) -> tuple[PrivateMcpManifest, ...]:
        return self.safe_manifest.mcps

    @property
    def mcp_tools(self) -> tuple[object, ...]:
        return self._mcp_tools

    async def materialize_skill_scoped_secrets(
        self,
        container_path: str,
        requested: object,
    ) -> dict[str, dict[str, str]]:
        """Revalidate and decrypt one short-lived sandbox-command carrier."""

        if self._closed or getattr(self, "_closing", False) or not isinstance(container_path, str) or not isinstance(requested, Mapping):
            raise PrivateWorkAssetStale(self._context.request_id)
        skill_by_path = {
            posixpath.normpath(skill.get_container_file_path(container_path)): (manifest, skill)
            for manifest, skill in zip(
                self.safe_manifest.skills,
                self.skills,
                strict=True,
            )
        }
        requested_by_path: dict[str, frozenset[str]] = {}
        for raw_path, raw_names in requested.items():
            if not isinstance(raw_path, str) or not isinstance(raw_names, frozenset) or any(not isinstance(name, str) or not name for name in raw_names):
                raise PrivateWorkAssetStale(self._context.request_id)
            path = posixpath.normpath(raw_path)
            pair = skill_by_path.get(path)
            if pair is None:
                raise PrivateWorkAssetStale(self._context.request_id)
            _manifest, skill = pair
            declared = {requirement.name for requirement in skill.required_secrets}
            if raw_names != declared:
                raise PrivateWorkAssetStale(self._context.request_id)
            requested_by_path[path] = raw_names
        if not requested_by_path:
            return {}
        requested_manifests = tuple(manifest for path, (manifest, _skill) in skill_by_path.items() if path in requested_by_path)
        requested_version_ids = {manifest.version_id for manifest in requested_manifests}
        repository = RunSnapshotRepository(
            self._session_factory,
            endpoint_policy=self._endpoint_policy,
        )
        values_by_version: dict[uuid.UUID, dict[str, str]] = {manifest.version_id: {} for manifest in requested_manifests}
        try:
            async with self._session_factory() as session, session.begin():
                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._run_id,
                    lock=False,
                )
                if not active:
                    raise AuthorizationRevoked
                await resolve_project_context_in_transaction(
                    session,
                    self._context.user_id,
                    self._context.project_id,
                    self._context.request_id,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=self._context.resource_scope,
                    run_id=self._run_id,
                    lock=True,
                )
                if run is None or run.status not in {"pending", "running"}:
                    raise RunSnapshotAssetStale
                assets = await repository.list_assets_in_session(
                    session,
                    self._context,
                    self._run_id,
                    lock=True,
                )
                skill_assets = tuple(asset for asset in assets if (asset.asset_kind == AssetKind.SKILL.value and asset.version_id in requested_version_ids))
                if tuple((asset.asset_id, asset.version_id) for asset in skill_assets) != tuple((manifest.asset_id, manifest.version_id) for manifest in requested_manifests):
                    raise RunSnapshotAssetStale
                persisted = tuple(
                    sorted(
                        (
                            item
                            for item in await repository.list_skill_credentials_in_session(
                                session,
                                self._context,
                                self._run_id,
                                lock=True,
                            )
                            if item.skill_version_id in requested_version_ids
                        ),
                        key=lambda item: (
                            item.skill_version_id.int,
                            item.secret_name,
                            item.skill_credential_binding_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                current = await repository.current_skill_credentials_in_session(
                    session,
                    self._context,
                    skill_assets,
                )
                if current != persisted:
                    raise RunSnapshotAssetStale
                try:
                    closures = await lock_skill_credential_closures(
                        session,
                        self._context.project_id,
                        tuple(
                            SkillCredentialClosureTarget(
                                skill_id=manifest.asset_id,
                                skill_version_id=manifest.version_id,
                            )
                            for manifest in requested_manifests
                        ),
                        load_envelopes=True,
                        require_required=True,
                    )
                except SkillCredentialClosureInvalid:
                    raise RunSnapshotAssetStale from None
                credential_keyring: CredentialKeyring | None = None
                for manifest in requested_manifests:
                    skill_path = next(path for path, (candidate, _skill) in skill_by_path.items() if candidate.version_id == manifest.version_id)
                    requested_names = requested_by_path.get(
                        skill_path,
                        frozenset(),
                    )
                    for material in closures[manifest.version_id].materials:
                        if material.env_name not in requested_names:
                            continue
                        envelope = material.envelope
                        if envelope is None:
                            raise RunSnapshotAssetStale
                        if credential_keyring is None:
                            try:
                                credential_keyring = CredentialKeyring.from_environment()
                            except CredentialKeyringInvalid:
                                raise AssetStorageUnavailable(self._context.request_id) from None
                        payload: dict[str, object] | None = None
                        try:
                            payload = await asyncio.to_thread(
                                decrypt_credential_payload,
                                EncryptedEnvelope(
                                    key_id=envelope.key_id,
                                    nonce=bytes(envelope.nonce),
                                    ciphertext=bytes(envelope.ciphertext),
                                ),
                                AssetScope.PROJECT,
                                self._context.project_id,
                                material.credential_version_id,
                                credential_keyring,
                            )
                            env = payload.get(material.credential_field_group)
                            value = env.get(material.credential_field_name) if isinstance(env, Mapping) else None
                            if not isinstance(value, str):
                                raise RunSnapshotAssetStale
                            values_by_version[manifest.version_id][material.env_name] = value
                        finally:
                            if isinstance(payload, dict):
                                for group in payload.values():
                                    if isinstance(group, dict):
                                        group.clear()
                                payload.clear()
            result = {path: dict(values_by_version[manifest.version_id]) for path, (manifest, _skill) in skill_by_path.items() if path in requested_by_path}
            return result
        except (
            RunSnapshotAssetStale,
            AssetResolutionUnavailable,
            AssetValidationFailed,
            AssetForbidden,
            SkillCredentialClosureInvalid,
        ):
            raise PrivateWorkAssetStale(self._context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except CredentialDecryptFailed:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(self._context.request_id) from None
        except AuthorizationRevoked:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        finally:
            for values in values_by_version.values():
                values.clear()
            values_by_version.clear()

    def set_authorization_boundary(self, boundary: object) -> None:
        self._authorization_boundary = boundary

    async def discover_mcp_tools(self) -> None:
        """Copy remote schemas into run-local proxies, never remote tool objects."""

        schemas: list[_DiscoveredMcpTool] = []
        for snapshot in self._mcp_snapshots:
            discovered = await self.invoke_with_mcp_material(
                snapshot.version_id,
                lambda definition, material, version_id=snapshot.version_id: self._discover_exact_mcp(
                    version_id,
                    definition,
                    material,
                    authorization_boundary=self._authorization_boundary,
                    http_client_factory=self._http_client_factory,
                    discovery_timeout_seconds=self._discovery_timeout_seconds,
                ),
            )
            schemas.extend(discovered)
        self._mcp_tools = tuple(self._proxy_tool(schema) for schema in schemas)

    def _proxy_tool(self, schema: _DiscoveredMcpTool) -> StructuredTool:
        async def invoke(**arguments):
            return await self.invoke_with_mcp_material(
                schema.version_id,
                lambda definition, material: self._invoke_exact_mcp(
                    schema.version_id,
                    definition,
                    material,
                    schema.name,
                    arguments,
                    authorization_boundary=self._authorization_boundary,
                    http_client_factory=self._http_client_factory,
                    discovery_timeout_seconds=self._discovery_timeout_seconds,
                    tool_call_timeout_seconds=self._tool_call_timeout_seconds,
                ),
            )

        proxy = StructuredTool.from_function(
            coroutine=invoke,
            name=schema.name,
            description=schema.description,
            args_schema=schema.args_schema,
            metadata={"deerflow_private_mcp": True},
        )
        tag_mcp_tool(proxy)
        if schema.routing is not None:
            tag_mcp_routing(proxy, schema.routing)
        return proxy

    @staticmethod
    def _material_values(
        material: Mapping[str, Mapping[str, object]],
    ) -> tuple[str | bytes | bool | int | float, ...]:
        values: list[str | bytes | bool | int | float] = []

        def collect(value: object) -> None:
            if isinstance(value, Mapping):
                for item in value.values():
                    collect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif isinstance(value, (str, bytes)) and value:
                values.append(value)
            elif isinstance(value, (bool, int, float)):
                values.append(value)

        collect(material)
        return tuple(values)

    @classmethod
    def _assert_mcp_result_secret_free(
        cls,
        result: object,
        material: Mapping[str, Mapping[str, object]],
        *,
        extra_forbidden_values: tuple[
            str | bytes | bool | int | float,
            ...,
        ] = (),
    ) -> None:
        forbidden = (*cls._material_values(material), *extra_forbidden_values)
        cls._assert_value_secret_free(
            result,
            forbidden,
            PrivateWorkUnavailable,
        )

    @staticmethod
    def _scalar_contains_secret(
        value: str | bytes | bool | int | float,
        forbidden: tuple[str | bytes | bool | int | float, ...],
    ) -> bool:
        for secret in forbidden:
            if isinstance(value, str) and isinstance(secret, str):
                if value == secret or (len(secret) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and secret in value):
                    return True
            elif isinstance(value, bytes) and isinstance(secret, bytes):
                if value == secret or (len(secret) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and secret in value):
                    return True
            elif isinstance(value, str) and isinstance(secret, bytes):
                decoded = secret.decode("utf-8", errors="ignore")
                if decoded and (value == decoded or (len(decoded) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and decoded in value)):
                    return True
            elif isinstance(value, bytes) and isinstance(secret, str):
                encoded = secret.encode()
                if encoded and (value == encoded or (len(encoded) >= _MCP_SECRET_SUBSTRING_MIN_LENGTH and encoded in value)):
                    return True
            elif type(value) is type(secret) and value == secret:
                return True
        return False

    @classmethod
    def _assert_value_secret_free(
        cls,
        result: object,
        forbidden: tuple[str | bytes | bool | int | float, ...],
        error_type: type[PrivateWorkError],
    ) -> None:
        seen: set[int] = set()

        def inspect_value(value: object) -> None:
            if value is None:
                return
            if isinstance(value, (str, bytes, bool, int, float)):
                if cls._scalar_contains_secret(value, forbidden):
                    raise error_type("unknown")
                return
            identity = id(value)
            if identity in seen:
                raise error_type("unknown")
            seen.add(identity)
            if isinstance(value, Mapping):
                for key, item in value.items():
                    inspect_value(key)
                    inspect_value(item)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    inspect_value(item)
                return
            if isinstance(value, BaseModel):
                inspect_value(value.model_dump(mode="python"))
                return
            if is_dataclass(value) and not isinstance(value, type):
                inspect_value({field.name: getattr(value, field.name) for field in fields(value)})
                return
            raise error_type("unknown")

        inspect_value(result)

    @staticmethod
    async def _with_one_shot_mcp_tools(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        operation: Callable[
            [
                tuple[object, ...],
                list[str],
            ],
            Awaitable[Any],
        ],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        operation_timeout_seconds: int | None = None,
    ) -> Any:
        discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        if operation_timeout_seconds is not None:
            operation_timeout_seconds = _validated_mcp_runtime_timeout(operation_timeout_seconds)
        client = None
        merged_config = None
        derived_secrets: list[str] = []
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            from deerflow.mcp.client import build_servers_config
            from deerflow.mcp.config import ExtensionsConfig, McpServerConfig
            from deerflow.mcp.oauth import OAuthTokenManager
            from deerflow.mcp.tools import (
                _catalog_mcp_definition,
                _catalog_oauth_configs,
                _merge_catalog_mcp_secrets,
            )

            server_name = f"project_{version_id.hex[:16]}"
            raw = _catalog_mcp_definition(definition)
            server = McpServerConfig.model_validate(raw)
            extensions = ExtensionsConfig(mcpServers={server_name: server})
            server_config = build_servers_config(extensions)[server_name]
            merged_config = _merge_catalog_mcp_secrets(server_config, material)
            if merged_config.get("transport") in {"http", "sse"} and http_client_factory is not None:
                merged_config["httpx_client_factory"] = http_client_factory

            def remember_authorization(authorization: str | None) -> None:
                if not authorization:
                    return
                candidates = (authorization, authorization.partition(" ")[2])
                for candidate in candidates:
                    if candidate and candidate not in derived_secrets:
                        derived_secrets.append(candidate)

            try:
                async with asyncio.timeout(discovery_timeout_seconds):
                    if authorization_boundary is not None:
                        await authorization_boundary.before_mcp_call()
                    tool_interceptors: list[object] = []
                    catalog_oauth = _catalog_oauth_configs(
                        extensions,
                        {server_name: material},
                    )
                    if catalog_oauth:
                        token_manager = OAuthTokenManager(catalog_oauth)
                        authorization = await token_manager.get_authorization_header(server_name)
                        remember_authorization(authorization)
                        if authorization:
                            headers = dict(merged_config.get("headers") or {})
                            headers["Authorization"] = authorization
                            merged_config["headers"] = headers

                        async def catalog_oauth_interceptor(
                            request: Any,
                            handler: Any,
                        ) -> Any:
                            refreshed = await token_manager.get_authorization_header(request.server_name)
                            remember_authorization(refreshed)
                            if not refreshed:
                                return await handler(request)
                            headers = dict(request.headers or {})
                            headers["Authorization"] = refreshed
                            return await handler(request.override(headers=headers))

                        tool_interceptors.append(catalog_oauth_interceptor)
                    client_kwargs: dict[str, object] = {
                        "tool_name_prefix": True,
                    }
                    if tool_interceptors:
                        client_kwargs["tool_interceptors"] = tool_interceptors
                    client = MultiServerMCPClient(
                        {server_name: merged_config},
                        **client_kwargs,
                    )
                    remote_tools = tuple(await client.get_tools(server_name=server_name))
            except TimeoutError:
                raise PrivateWorkUnavailable("unknown") from None
            if operation_timeout_seconds is None:
                return await operation(remote_tools, derived_secrets)
            try:
                async with asyncio.timeout(operation_timeout_seconds):
                    return await operation(remote_tools, derived_secrets)
            except TimeoutError:
                raise PrivateWorkUnavailable("unknown") from None
        finally:
            if client is not None:
                close = getattr(client, "aclose", None)
                if callable(close):
                    try:
                        async with asyncio.timeout(_MCP_CLOSE_TIMEOUT_SECONDS):
                            await close()
                    except Exception:
                        pass
            merged_config = None
            client = None
            derived_secrets.clear()

    @classmethod
    async def _discover_exact_mcp(
        cls,
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int | None = None,
    ) -> tuple[_DiscoveredMcpTool, ...]:
        forbidden_values = cls._material_values(material)

        async def copy_schemas(
            remote_tools: tuple[object, ...],
            derived_secrets: list[str] | None = None,
        ) -> tuple[_DiscoveredMcpTool, ...]:
            if len(remote_tools) > _MAX_MCP_TOOLS_PER_SERVER:
                raise PrivateWorkAssetStale("unknown")
            from deerflow.mcp.config import (
                McpServerConfig,
                resolve_effective_mcp_routing,
            )
            from deerflow.mcp.tools import _catalog_mcp_definition

            try:
                server_config = McpServerConfig.model_validate(_catalog_mcp_definition(definition))
            except Exception:
                raise PrivateWorkAssetStale("unknown") from None
            server_prefix = f"project_{version_id.hex[:16]}_"
            copied: list[_DiscoveredMcpTool] = []
            for index, remote in enumerate(remote_tools):
                name = str(getattr(remote, "name", ""))
                description = str(getattr(remote, "description", ""))
                args_schema = getattr(remote, "args_schema", None)
                if args_schema is None:
                    get_schema = getattr(remote, "get_input_schema", None)
                    args_schema = get_schema() if callable(get_schema) else _EmptyMcpArgs
                try:
                    if isinstance(args_schema, Mapping):
                        args_schema = _safe_mcp_args_model(
                            args_schema,
                            model_name=(f"PrivateMcpArgs{version_id.hex}{index}"),
                        )
                    if not name or len(name) > 255 or len(description) > 20_000 or not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
                        raise ValueError
                    original_name = name[len(server_prefix) :] if name.startswith(server_prefix) else name
                    routing = resolve_effective_mcp_routing(
                        server_config,
                        original_name,
                    )
                except Exception:
                    raise PrivateWorkAssetStale("unknown")
                cls._assert_value_secret_free(
                    (
                        name,
                        description,
                        args_schema.model_json_schema(),
                        routing,
                    ),
                    (*forbidden_values, *(derived_secrets or ())),
                    PrivateWorkAssetStale,
                )
                copied.append(
                    _DiscoveredMcpTool(
                        version_id=version_id,
                        name=name,
                        description=description,
                        args_schema=args_schema,
                        routing=(dict(routing) if routing.get("mode") != "off" else None),
                    )
                )
            return tuple(copied)

        if authorization_boundary is None and http_client_factory is None and discovery_timeout_seconds is None:
            return await cls._with_one_shot_mcp_tools(
                version_id,
                definition,
                material,
                copy_schemas,
            )
        effective_timeout = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS if discovery_timeout_seconds is None else discovery_timeout_seconds
        return await cls._with_one_shot_mcp_tools(
            version_id,
            definition,
            material,
            copy_schemas,
            authorization_boundary,
            http_client_factory=http_client_factory,
            discovery_timeout_seconds=effective_timeout,
            operation_timeout_seconds=effective_timeout,
        )

    @staticmethod
    async def _invoke_exact_mcp(
        version_id: uuid.UUID,
        definition: Mapping[str, object],
        material: Mapping[str, Mapping[str, object]],
        tool_name: str,
        arguments: Mapping[str, object],
        authorization_boundary: object | None = None,
        *,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
    ) -> Any:
        try:

            async def call_selected(
                discovered: tuple[object, ...],
                derived_secrets: list[str] | None = None,
            ) -> Any:
                selected = next((tool for tool in discovered if getattr(tool, "name", None) == tool_name), None)
                if selected is None:
                    raise PrivateWorkAssetStale("unknown")
                if authorization_boundary is not None:
                    dispatch_check = getattr(
                        authorization_boundary,
                        "before_mcp_tool_dispatch",
                        None,
                    )
                    if callable(dispatch_check):
                        await dispatch_check()
                    else:
                        await authorization_boundary.before_mcp_call()
                result = await selected.ainvoke(dict(arguments))
                PrivateAgentRuntime._assert_mcp_result_secret_free(
                    result,
                    material,
                    extra_forbidden_values=tuple(derived_secrets or ()),
                )
                return result

            if authorization_boundary is None:
                return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                    version_id,
                    definition,
                    material,
                    call_selected,
                    http_client_factory=http_client_factory,
                    discovery_timeout_seconds=discovery_timeout_seconds,
                    operation_timeout_seconds=tool_call_timeout_seconds,
                )
            return await PrivateAgentRuntime._with_one_shot_mcp_tools(
                version_id,
                definition,
                material,
                call_selected,
                authorization_boundary,
                http_client_factory=http_client_factory,
                discovery_timeout_seconds=discovery_timeout_seconds,
                operation_timeout_seconds=tool_call_timeout_seconds,
            )
        except PrivateWorkError:
            raise
        except AuthorizationRevoked:
            raise
        except Exception:
            raise PrivateWorkUnavailable("unknown") from None

    def _validate_project_mcp_snapshot(
        self,
        snapshot: ResolvedMcpSnapshot,
    ) -> None:
        if snapshot.scope is not AssetScope.PROJECT:
            return
        definition = snapshot.definition
        oauth = definition.get("oauth", {})
        if not isinstance(oauth, Mapping) or oauth:
            raise McpDefinitionPolicyError
        slots = definition.get("credential_slots", ())
        if not isinstance(slots, (list, tuple)):
            raise McpDefinitionPolicyError
        credential_slot_schemas: list[Mapping[object, object]] = []
        for slot in slots:
            if not isinstance(slot, Mapping):
                raise McpDefinitionPolicyError
            payload_schema = slot.get("payload_schema", {})
            if not isinstance(payload_schema, Mapping) or "env" in payload_schema or "oauth" in payload_schema:
                raise McpDefinitionPolicyError
            credential_slot_schemas.append(payload_schema)
        validate_project_mcp_definition(
            transport=definition.get("transport"),
            url=definition.get("url"),
            env=definition.get("env", {}),  # type: ignore[arg-type]
            headers=definition.get("headers", {}),  # type: ignore[arg-type]
            oauth=oauth,
            credential_slot_schemas=tuple(credential_slot_schemas),
            endpoint_policy=self._endpoint_policy,
        )
        if self._http_client_factory is None:
            raise McpDefinitionPolicyError

    @staticmethod
    def _validate_project_mcp_material(
        snapshot: ResolvedMcpSnapshot,
        material: Mapping[str, Mapping[str, object]],
    ) -> None:
        if snapshot.scope is not AssetScope.PROJECT:
            return
        for payload in material.values():
            if not isinstance(payload, Mapping) or set(payload) - {"headers"} or not isinstance(payload.get("headers", {}), Mapping):
                raise McpDefinitionPolicyError

    async def invoke_with_mcp_material(
        self,
        mcp_version_id: uuid.UUID,
        operation: Callable[[Mapping[str, object], Mapping[str, Mapping[str, object]]], Awaitable[Any]],
    ) -> Any:
        """Materialize plaintext into one local MCP call and release it at return."""

        if self._closed:
            raise PrivateWorkAssetStale(self._context.request_id)
        snapshot = next((item for item in self._mcp_snapshots if item.version_id == mcp_version_id), None)
        if snapshot is None:
            raise PrivateWorkAssetStale(self._context.request_id)
        try:
            self._validate_project_mcp_snapshot(snapshot)
        except McpDefinitionPolicyError:
            raise PrivateWorkAssetStale(self._context.request_id) from None
        if self._authorization_boundary is not None:
            await self._authorization_boundary.before_mcp_call()
        materialized = await self._materialize_mcp_call(snapshot)
        try:
            try:
                self._validate_project_mcp_material(
                    snapshot,
                    materialized.by_slot,
                )
                return await operation(snapshot.definition, materialized.by_slot)
            finally:
                del materialized
        except (
            AssetResolutionUnavailable,
            AssetValidationFailed,
            AssetForbidden,
            McpDefinitionPolicyError,
        ):
            raise PrivateWorkAssetStale(self._context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(self._context.request_id) from None
        except AuthorizationRevoked:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def _materialize_mcp_call(self, snapshot: ResolvedMcpSnapshot):
        """Compare and decrypt the exact closure in one caller-owned transaction."""

        repository = RunSnapshotRepository(
            self._session_factory,
            endpoint_policy=self._endpoint_policy,
        )
        try:
            async with self._session_factory() as session, session.begin():
                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._run_id,
                    lock=False,
                )
                if not active:
                    raise AuthorizationRevoked
                current = await resolve_project_context_in_transaction(
                    session,
                    self._context.user_id,
                    self._context.project_id,
                    self._context.request_id,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=self._context.resource_scope,
                    run_id=self._run_id,
                    lock=True,
                )
                if run is None or run.status not in {"pending", "running"}:
                    raise RunSnapshotAssetStale
                assets = await repository.list_assets_in_session(
                    session,
                    self._context,
                    self._run_id,
                    lock=True,
                )
                matching_assets = tuple(asset for asset in assets if asset.asset_kind == AssetKind.MCP.value and asset.version_id == snapshot.version_id)
                if len(matching_assets) != 1:
                    raise RunSnapshotAssetStale
                asset = matching_assets[0]
                if asset.asset_id != snapshot.asset_id or asset.asset_scope != snapshot.scope.value or asset.payload_checksum != snapshot.checksum:
                    raise RunSnapshotAssetStale
                persisted = tuple(
                    sorted(
                        (
                            grant
                            for grant in await repository.list_mcp_grants_in_session(
                                session,
                                self._context,
                                self._run_id,
                                lock=True,
                            )
                            if grant.mcp_version_id == snapshot.version_id
                        ),
                        key=lambda item: (
                            item.mcp_version_id.int,
                            item.credential_slot_id.int,
                            item.credential_grant_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                materialized = await self._resolver.materialize_mcp_secrets_in_session(
                    session,
                    current,
                    snapshot,
                    expected_grants=tuple(
                        (
                            grant.credential_slot_id,
                            grant.credential_grant_id,
                            grant.credential_version_id,
                        )
                        for grant in persisted
                    ),
                )
                return materialized
        except (
            RunSnapshotAssetStale,
            AssetResolutionUnavailable,
            AssetValidationFailed,
            AssetForbidden,
        ):
            raise PrivateWorkAssetStale(self._context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(self._context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(self._context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aclose(self) -> None:
        if getattr(self, "_closed", False):
            return
        if getattr(self, "_closing", False):
            raise PrivateRuntimeCleanupError("Private runtime cleanup is already in progress")
        self._closing = True
        try:
            await asyncio.to_thread(
                _remove_private_skill_tree,
                self.skill_root,
            )
        except Exception:
            self._closing = False
            raise
        self._closed = True
        self._closing = False


class PrivateAssetRuntime:
    """Build run-scoped Agent/Skill/MCP state from persisted exact IDs only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        resolver: ProjectAssetResolver | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
        snapshots: RunSnapshotRepository | None = None,
        endpoint_policy: McpEndpointPolicy | None = None,
        http_client_factory: SecureMcpHttpClientFactory | None = None,
        discovery_timeout_seconds: int = _DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: int = _DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or ProjectAssetResolver(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()
        self._snapshots = snapshots or RunSnapshotRepository(
            session_factory,
            endpoint_policy=endpoint_policy,
        )
        self._endpoint_policy = endpoint_policy
        self._http_client_factory = http_client_factory
        self._discovery_timeout_seconds = _validated_mcp_runtime_timeout(discovery_timeout_seconds)
        self._tool_call_timeout_seconds = _validated_mcp_runtime_timeout(tool_call_timeout_seconds)

    async def materialize(
        self,
        context: PrivateWorkContext,
        admitted: AdmittedPrivateRun,
        *,
        authorization_boundary: object | None = None,
    ) -> PrivateAgentRuntime:
        context = require_issued_private_work_context(context)
        if type(admitted) is not AdmittedPrivateRun:
            raise PrivateWorkNotFound(context.request_id)
        authorization_boundary = authorization_boundary or PrivateRunAuthorizationBoundary(
            self._session_factory,
            project_id=admitted.run.project_id,
            owner_user_id=admitted.run.owner_user_id,
            run_id=admitted.run.run_id,
        )
        before_snapshot_read = getattr(
            authorization_boundary,
            "before_checkpoint_read",
            None,
        )
        if callable(before_snapshot_read):
            await before_snapshot_read()
        skill_snapshots: tuple[ResolvedSkillSnapshot, ...]
        mcp_snapshots: tuple[ResolvedMcpSnapshot, ...]
        try:
            async with self._session_factory() as session, session.begin():
                current = await self._revalidator.require(
                    session,
                    context,
                    Capability.PRIVATE_WORK_CREATE,
                    Capability.SHARED_ASSETS_EXECUTE,
                    lock=True,
                )
                run = await PrivateRunRepository(session).get(
                    scope=context.resource_scope,
                    run_id=admitted.run.run_id,
                    lock=True,
                )
                execution_job_id = getattr(
                    authorization_boundary,
                    "execution_job_id",
                    None,
                )
                executable_status = run is not None and (run.status == "pending" or (run.status == "running" and run.job_id == admitted.job.job_id and execution_job_id == admitted.job.job_id))
                if run is None or run.thread_id != admitted.thread_id or not executable_status:
                    raise PrivateWorkNotFound(context.request_id)
                assets = await self._snapshots.list_assets_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                grants = await self._snapshots.list_mcp_grants_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                skill_credentials = await self._snapshots.list_skill_credentials_in_session(
                    session,
                    context,
                    run.run_id,
                    lock=True,
                )
                if not assets or assets[0].asset_kind != AssetKind.AGENT.value:
                    raise RunSnapshotAssetStale
                persisted_generation = assets[0].catalog_generation
                if any(asset.catalog_generation != persisted_generation for asset in assets):
                    raise RunSnapshotAssetStale

                resolved: list[ResolvedAgentSnapshot | ResolvedSkillSnapshot | ResolvedMcpSnapshot] = []
                for asset in assets:
                    try:
                        kind = AssetKind(asset.asset_kind)
                    except ValueError:
                        raise RunSnapshotAssetStale from None
                    snapshot = await self._resolver.resolve_project_asset_snapshot_in_session(
                        session,
                        current,
                        AssetSelection(kind, asset.asset_id, asset.version_id),
                    )
                    if snapshot.kind is not kind or snapshot.scope.value != asset.asset_scope or snapshot.asset_id != asset.asset_id or snapshot.version_id != asset.version_id or snapshot.checksum != asset.payload_checksum:
                        raise RunSnapshotAssetStale
                    resolved.append(
                        replace(
                            snapshot,
                            catalog_generation=persisted_generation,
                        )
                    )

                agent = resolved[0]
                if type(agent) is not ResolvedAgentSnapshot:
                    raise RunSnapshotAssetStale
                expected_versions = (*agent.payload.skill_version_ids, *agent.payload.mcp_version_ids)
                if tuple(asset.version_id for asset in assets[1:]) != expected_versions:
                    raise RunSnapshotAssetStale
                skill_snapshots = tuple(item for item in resolved[1:] if type(item) is ResolvedSkillSnapshot)
                mcp_snapshots = tuple(item for item in resolved[1:] if type(item) is ResolvedMcpSnapshot)
                if len(skill_snapshots) != len(agent.payload.skill_version_ids) or len(mcp_snapshots) != len(agent.payload.mcp_version_ids):
                    raise RunSnapshotAssetStale
                current_grants = await self._snapshots.current_mcp_grants_in_session(
                    session,
                    context,
                    tuple(asset for asset in assets if asset.asset_kind == AssetKind.MCP.value),
                )
                persisted_grants = tuple(
                    sorted(
                        grants,
                        key=lambda item: (
                            item.mcp_version_id.int,
                            item.credential_slot_id.int,
                            item.credential_grant_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                if current_grants != persisted_grants:
                    raise RunSnapshotAssetStale
                skill_assets = tuple(asset for asset in assets if asset.asset_kind == AssetKind.SKILL.value)
                current_skill_credentials = await self._snapshots.current_skill_credentials_in_session(
                    session,
                    context,
                    skill_assets,
                )
                persisted_skill_credentials = tuple(
                    sorted(
                        skill_credentials,
                        key=lambda item: (
                            item.skill_version_id.int,
                            item.secret_name,
                            item.skill_credential_binding_id.int,
                            item.credential_version_id.int,
                        ),
                    )
                )
                if current_skill_credentials != persisted_skill_credentials:
                    raise RunSnapshotAssetStale
        except (RunSnapshotAssetStale, AssetResolutionUnavailable, AssetValidationFailed, AssetForbidden):
            raise PrivateWorkAssetStale(context.request_id) from None
        except AssetStorageUnavailable:
            raise PrivateWorkUnavailable(context.request_id) from None
        except PrivateWorkError as error:
            raise type(error)(context.request_id) from None
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        root = _create_private_skill_root(admitted.run.run_id, context.request_id)
        try:
            root.chmod(0o700)
            skill_manifests, skills = await asyncio.to_thread(_write_skill_tree, root, skill_snapshots)
            mcp_manifests = tuple(
                PrivateMcpManifest(
                    asset_id=snapshot.asset_id,
                    version_id=snapshot.version_id,
                    definition=_safe_copy(snapshot.definition),  # type: ignore[arg-type]
                )
                for snapshot in mcp_snapshots
            )
            safe_manifest = _private_agent_manifest(
                agent,
                skills=skill_manifests,
                mcps=mcp_manifests,
            )
            runtime = PrivateAgentRuntime(
                context=context,
                run_id=admitted.run.run_id,
                resolver=self._resolver,
                session_factory=self._session_factory,
                safe_manifest=safe_manifest,
                skill_root=root,
                skills=skills,
                mcp_snapshots=mcp_snapshots,
                authorization_boundary=authorization_boundary,
                endpoint_policy=self._endpoint_policy,
                http_client_factory=self._http_client_factory,
                discovery_timeout_seconds=self._discovery_timeout_seconds,
                tool_call_timeout_seconds=self._tool_call_timeout_seconds,
            )
            await runtime.discover_mcp_tools()
            return runtime
        except Exception as error:
            try:
                await asyncio.to_thread(_remove_private_skill_tree, root)
            except PrivateRuntimeCleanupError:
                logger.warning("Private runtime cleanup failed after materialization")
            if isinstance(error, PrivateWorkError):
                raise type(error)(context.request_id) from None
            if isinstance(error, AuthorizationRevoked):
                raise
            if isinstance(
                error,
                (
                    RunSnapshotAssetStale,
                    AssetResolutionUnavailable,
                    AssetValidationFailed,
                    AssetForbidden,
                ),
            ):
                raise PrivateWorkAssetStale(context.request_id) from None
            raise PrivateWorkUnavailable(context.request_id) from None
