"""Single-transaction PostgreSQL bootstrap for packaged system assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.bootstrap.catalog import (
    BootstrapCatalog,
    BootstrapCatalogError,
    BootstrapEntry,
    catalog_digest,
    catalog_payload,
    load_bootstrap_catalog,
)
from app.shared_assets.bootstrap.skill_archive import load_skill_archive
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import AgentPayload, SkillArchiveFile
from app.shared_assets.skill_service import (
    _analyze_skill_files,
    normalize_skill_files,
)
from deerflow.persistence.projects import ProjectMembershipRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user import UserRow

BUILTIN_ASSET_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")
BUILTIN_ASSET_EMAIL = "builtin-assets@deerflow.invalid"
_ID_NAMESPACE = uuid.UUID("6f6622dd-a1f5-5799-a2f7-d9f793ea8d2e")


class BootstrapConflict(RuntimeError):
    """Existing PostgreSQL state conflicts with the packaged catalog."""


class _AgentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    soul: str = Field(min_length=1)
    model_ref: str = Field(min_length=1, max_length=255)
    tool_groups: tuple[str, ...] = ()
    skill_source_keys: tuple[str, ...] = ()
    mcp_source_keys: tuple[str, ...] = ()


class _McpSlotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=63)
    purpose: str = ""
    payload_schema: dict[str, object]
    required: bool = True


class _McpPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, object] = Field(default_factory=dict)
    routing: dict[str, object] = Field(default_factory=dict)
    tool_overrides: dict[str, object] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, gt=0)
    credential_slots: tuple[_McpSlotPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    digest: str
    counts: Mapping[str, int]
    created: int


def _stable_id(value: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, value)


def _version_id(entry: BootstrapEntry) -> uuid.UUID:
    return _stable_id(f"{entry.source_key}:version:{entry.version}")


def _decode_json(model: type[BaseModel], content: bytes):
    try:
        return model.model_validate_json(content)
    except (ValidationError, UnicodeDecodeError, ValueError) as error:
        raise BootstrapCatalogError("bootstrap payload is invalid") from error


def _agent_checksum(payload: AgentPayload) -> str:
    canonical = json.dumps(
        {
            "description": payload.description,
            "mcp_version_ids": [str(value) for value in payload.mcp_version_ids],
            "model_ref": payload.model_ref,
            "skill_version_ids": [str(value) for value in payload.skill_version_ids],
            "soul": payload.soul,
            "tool_groups": list(payload.tool_groups),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _validated_skill_preview(
    entry: BootstrapEntry,
    archive_files: tuple[SkillArchiveFile, ...],
):
    try:
        preview = _analyze_skill_files(
            archive_files,
            entry.source_key,
        )
    except AssetValidationFailed as error:
        raise BootstrapCatalogError("bootstrap Skill archive is invalid") from error
    if preview.frontmatter.get("name") != entry.slug:
        raise BootstrapCatalogError("bootstrap Skill name does not match manifest")
    if not preview.description.strip():
        raise BootstrapCatalogError("bootstrap Skill description is invalid")
    return preview


async def _ensure_builtin_principal(session: AsyncSession) -> None:
    principal_id = str(BUILTIN_ASSET_USER_ID)
    row = await session.get(UserRow, principal_id, with_for_update=True)
    if row is None:
        session.add(
            UserRow(
                id=principal_id,
                email=BUILTIN_ASSET_EMAIL,
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
    elif row.email != BUILTIN_ASSET_EMAIL or row.password_hash is not None or row.oauth_provider is not None or row.oauth_id is not None or row.system_role != "user" or row.needs_setup:
        raise BootstrapConflict("builtin asset principal conflicts with canonical identity")
    membership = (await session.execute(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == principal_id).limit(1))).scalar_one_or_none()
    if membership is not None:
        raise BootstrapConflict("builtin asset principal cannot have project membership")

    # Principal-integrity verification intentionally includes logically deleted
    # Credentials: deletion must not hide a forbidden builtin-principal link.
    credential_actor_columns = (
        (CredentialRow, (CredentialRow.created_by_user_id, CredentialRow.revoked_by_user_id)),
        (
            CredentialVersionRow,
            (CredentialVersionRow.created_by_user_id, CredentialVersionRow.revoked_by_user_id),
        ),
        (CredentialEnvelopeRow, (CredentialEnvelopeRow.created_by_user_id,)),
        (
            CredentialGrantRow,
            (CredentialGrantRow.created_by_user_id, CredentialGrantRow.revoked_by_user_id),
        ),
    )
    for model, columns in credential_actor_columns:
        reference = (await session.execute(select(model).where(or_(*(column == principal_id for column in columns))).limit(1))).scalar_one_or_none()
        if reference is not None:
            raise BootstrapConflict("builtin asset principal cannot reference credentials")

    binding_actor_columns = (
        ProjectSystemAgentBindingRow,
        ProjectSystemSkillBindingRow,
        ProjectSystemMcpBindingRow,
    )
    for model in binding_actor_columns:
        reference = (
            await session.execute(
                select(model)
                .where(
                    or_(
                        model.created_by_user_id == principal_id,
                        model.updated_by_user_id == principal_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if reference is not None:
            raise BootstrapConflict("builtin asset principal cannot reference project bindings")


async def _existing_asset(session: AsyncSession, entry: BootstrapEntry):
    model = {"agent": AgentRow, "skill": SkillRow, "mcp": McpServerRow}[entry.kind]
    return (await session.execute(select(model).where(model.source_key == entry.source_key).with_for_update())).scalar_one_or_none()


def _validate_asset_row(row, entry: BootstrapEntry) -> None:
    if (
        row.id != _stable_id(entry.source_key)
        or row.scope != "system"
        or row.project_id is not None
        or row.slug != entry.slug
        or row.display_name != entry.display_name
        or row.status != "active"
        or row.version != 1
        or row.source_key != entry.source_key
        or row.created_by_user_id != str(BUILTIN_ASSET_USER_ID)
    ):
        raise BootstrapConflict("existing system asset conflicts with canonical manifest")


def _strict_value_equal(actual: object, expected: object) -> bool:
    if isinstance(actual, Mapping) or isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
            return False
        return actual.keys() == expected.keys() and all(_strict_value_equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) or isinstance(expected, list):
        if not isinstance(actual, list) or not isinstance(expected, list):
            return False
        return len(actual) == len(expected) and all(_strict_value_equal(actual_item, expected_item) for actual_item, expected_item in zip(actual, expected, strict=True))
    if isinstance(actual, (bool, int, float)) or isinstance(expected, (bool, int, float)):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _matches(row: object, **expected: object) -> bool:
    return all(_strict_value_equal(getattr(row, name), value) for name, value in expected.items())


async def _seed_skill(session: AsyncSession, catalog: BootstrapCatalog, entry: BootstrapEntry) -> bool:
    payload = catalog_payload(catalog, entry)
    archive_files = (
        load_skill_archive(payload)
        if entry.payload_format == "skill_archive_v1"
        else (
            SkillArchiveFile(
                path="SKILL.md",
                content=payload,
                media_type="text/markdown",
            ),
        )
    )
    try:
        archive_files = normalize_skill_files(
            archive_files,
            request_id=entry.source_key,
        )
    except AssetValidationFailed as error:
        raise BootstrapCatalogError("bootstrap Skill archive is invalid") from error
    preview = await asyncio.to_thread(
        _validated_skill_preview,
        entry,
        archive_files,
    )
    frontmatter = dict(preview.frontmatter)
    description = preview.description
    compatibility = preview.compatibility
    requirements = [
        {
            "name": requirement.name,
            "optional": requirement.optional,
        }
        for requirement in preview.secret_requirements
    ]
    checksum = preview.checksum
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    if asset is not None:
        _validate_asset_row(asset, entry)
        version = await session.get(SkillVersionRow, version_id)
        if version is None or not _matches(
            version,
            skill_id=asset_id,
            version_number=entry.version,
            workflow_status="published",
            description=description,
            frontmatter=frontmatter,
            compatibility=compatibility,
            secret_requirements=requirements,
            scan_decision=preview.scan_decision,
            scan_summary=dict(preview.scan_summary),
            supersedes_version_id=None,
            payload_checksum=checksum,
            submitted_at=None,
            reviewed_at=None,
            reviewed_by_user_id=None,
            review_note=None,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
        ):
            raise BootstrapConflict("existing system Skill conflicts with canonical payload")
        persisted_files = (await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version_id).order_by(SkillVersionFileRow.path))).scalars().all()
        expected_files = [
            (
                version_id,
                file.path,
                file.media_type,
                len(file.content),
                hashlib.sha256(file.content).hexdigest(),
                file.content,
            )
            for file in archive_files
        ]
        actual_files = [
            (
                file.skill_version_id,
                file.path,
                file.media_type,
                file.size_bytes,
                file.sha256,
                bytes(file.content),
            )
            for file in persisted_files
        ]
        if asset.current_published_version_id != version_id or actual_files != expected_files:
            raise BootstrapConflict("existing system Skill files conflict with canonical payload")
        return False

    asset = SkillRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=entry.slug,
        display_name=entry.display_name,
        status="active",
        current_published_version_id=None,
        version=1,
        source_key=entry.source_key,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    version = SkillVersionRow(
        id=version_id,
        skill_id=asset_id,
        version_number=entry.version,
        workflow_status="draft",
        description=description,
        frontmatter=frontmatter,
        compatibility=compatibility,
        secret_requirements=requirements,
        scan_decision=preview.scan_decision,
        scan_summary=dict(preview.scan_summary),
        supersedes_version_id=None,
        payload_checksum=checksum,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    session.add_all([asset, version])
    await session.flush()
    session.add_all(
        [
            SkillVersionFileRow(
                skill_version_id=version_id,
                path=file.path,
                media_type=file.media_type,
                size_bytes=len(file.content),
                sha256=hashlib.sha256(file.content).hexdigest(),
                content=file.content,
            )
            for file in archive_files
        ]
    )
    await session.flush()
    version.workflow_status = "published"
    await session.flush()
    asset.current_published_version_id = version_id
    await session.flush()
    return True


def _resolved_dependency_ids(
    entries_by_source: Mapping[str, BootstrapEntry],
    source_keys: tuple[str, ...],
    expected_kind: str,
) -> tuple[uuid.UUID, ...]:
    resolved: list[uuid.UUID] = []
    for source_key in source_keys:
        dependency = entries_by_source.get(source_key)
        if dependency is None or dependency.kind != expected_kind:
            raise BootstrapCatalogError("bootstrap dependency is missing or has the wrong kind")
        resolved.append(_version_id(dependency))
    if len(resolved) != len(set(resolved)):
        raise BootstrapCatalogError("bootstrap dependencies must be unique")
    return tuple(resolved)


async def _seed_agent(session: AsyncSession, catalog: BootstrapCatalog, entry: BootstrapEntry) -> bool:
    raw = _decode_json(_AgentPayload, catalog_payload(catalog, entry))
    entries = {item.source_key: item for item in catalog.entries}
    payload = AgentPayload(
        description=raw.description,
        soul=raw.soul,
        model_ref=raw.model_ref,
        tool_groups=raw.tool_groups,
        skill_version_ids=_resolved_dependency_ids(entries, raw.skill_source_keys, "skill"),
        mcp_version_ids=_resolved_dependency_ids(entries, raw.mcp_source_keys, "mcp"),
    )
    checksum = _agent_checksum(payload)
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    if asset is not None:
        _validate_asset_row(asset, entry)
        version = await session.get(AgentVersionRow, version_id)
        if version is None or not _matches(
            version,
            agent_id=asset_id,
            version_number=entry.version,
            workflow_status="published",
            description=payload.description,
            soul=payload.soul,
            model_ref=payload.model_ref,
            tool_groups=list(payload.tool_groups),
            supersedes_version_id=None,
            payload_checksum=checksum,
            submitted_at=None,
            reviewed_at=None,
            reviewed_by_user_id=None,
            review_note=None,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
        ):
            raise BootstrapConflict("existing system Agent conflicts with canonical payload")
        skill_refs = (
            (await session.execute(select(AgentVersionSkillRefRow).where(AgentVersionSkillRefRow.agent_version_id == version_id).order_by(AgentVersionSkillRefRow.sort_order, AgentVersionSkillRefRow.skill_version_id))).scalars().all()
        )
        mcp_refs = (await session.execute(select(AgentVersionMcpRefRow).where(AgentVersionMcpRefRow.agent_version_id == version_id).order_by(AgentVersionMcpRefRow.sort_order, AgentVersionMcpRefRow.mcp_server_version_id))).scalars().all()
        actual_skills = [(row.agent_version_id, row.skill_version_id, row.sort_order) for row in skill_refs]
        expected_skills = [(version_id, skill_version_id, index) for index, skill_version_id in enumerate(payload.skill_version_ids)]
        actual_mcps = [(row.agent_version_id, row.mcp_server_version_id, row.sort_order) for row in mcp_refs]
        expected_mcps = [(version_id, mcp_version_id, index) for index, mcp_version_id in enumerate(payload.mcp_version_ids)]
        if asset.current_published_version_id != version_id or actual_skills != expected_skills or actual_mcps != expected_mcps:
            raise BootstrapConflict("existing system Agent references conflict with canonical payload")
        return False

    asset = AgentRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=entry.slug,
        display_name=entry.display_name,
        status="active",
        current_published_version_id=None,
        version=1,
        source_key=entry.source_key,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=entry.version,
        workflow_status="draft",
        description=payload.description,
        soul=payload.soul,
        model_ref=payload.model_ref,
        tool_groups=list(payload.tool_groups),
        supersedes_version_id=None,
        payload_checksum=checksum,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    session.add_all([asset, version])
    await session.flush()
    session.add_all(
        [
            AgentVersionSkillRefRow(
                agent_version_id=version_id,
                skill_version_id=skill_version_id,
                sort_order=index,
            )
            for index, skill_version_id in enumerate(payload.skill_version_ids)
        ]
    )
    session.add_all(
        [
            AgentVersionMcpRefRow(
                agent_version_id=version_id,
                mcp_server_version_id=mcp_version_id,
                sort_order=index,
            )
            for index, mcp_version_id in enumerate(payload.mcp_version_ids)
        ]
    )
    await session.flush()
    version.workflow_status = "published"
    await session.flush()
    asset.current_published_version_id = version_id
    await session.flush()
    return True


def _mcp_checksum(raw: _McpPayload) -> str:
    canonical = {
        "args": list(raw.args),
        "command": raw.command,
        "credential_slots": [slot.model_dump(mode="json") for slot in raw.credential_slots],
        "description": raw.description,
        "env": raw.env,
        "headers": raw.headers,
        "oauth": raw.oauth,
        "routing": raw.routing,
        "timeout_seconds": raw.timeout_seconds,
        "tool_overrides": raw.tool_overrides,
        "transport": raw.transport,
        "url": raw.url,
    }
    encoded = json.dumps(canonical, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _seed_mcp(session: AsyncSession, catalog: BootstrapCatalog, entry: BootstrapEntry) -> bool:
    raw = _decode_json(_McpPayload, catalog_payload(catalog, entry))
    checksum = _mcp_checksum(raw)
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    if asset is not None:
        _validate_asset_row(asset, entry)
        version = await session.get(McpServerVersionRow, version_id)
        if version is None or not _matches(
            version,
            mcp_server_id=asset_id,
            version_number=entry.version,
            workflow_status="published",
            description=raw.description,
            transport=raw.transport,
            command=raw.command,
            args=list(raw.args),
            url=raw.url,
            non_secret_env=raw.env,
            non_secret_headers=raw.headers,
            oauth_metadata=raw.oauth,
            routing=raw.routing,
            tool_overrides=raw.tool_overrides,
            timeout_seconds=raw.timeout_seconds,
            supersedes_version_id=None,
            payload_checksum=checksum,
            submitted_at=None,
            reviewed_at=None,
            reviewed_by_user_id=None,
            review_note=None,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
        ):
            raise BootstrapConflict("existing system MCP conflicts with canonical payload")
        slots = (await session.execute(select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == version_id).order_by(McpCredentialSlotRow.name))).scalars().all()
        expected_slots = sorted(raw.credential_slots, key=lambda slot: slot.name)
        if asset.current_published_version_id != version_id or len(slots) != len(expected_slots):
            raise BootstrapConflict("existing system MCP slots conflict with canonical payload")
        for slot, expected in zip(slots, expected_slots, strict=True):
            if not _matches(
                slot,
                id=_stable_id(f"{entry.source_key}:version:{entry.version}:slot:{expected.name}"),
                mcp_server_version_id=version_id,
                name=expected.name,
                purpose=expected.purpose,
                payload_schema=expected.payload_schema,
                required=expected.required,
            ):
                raise BootstrapConflict("existing system MCP slots conflict with canonical payload")
        return False

    asset = McpServerRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=entry.slug,
        display_name=entry.display_name,
        status="active",
        current_published_version_id=None,
        version=1,
        source_key=entry.source_key,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    version = McpServerVersionRow(
        id=version_id,
        mcp_server_id=asset_id,
        version_number=entry.version,
        workflow_status="draft",
        description=raw.description,
        transport=raw.transport,
        command=raw.command,
        args=list(raw.args),
        url=raw.url,
        non_secret_env=raw.env,
        non_secret_headers=raw.headers,
        oauth_metadata=raw.oauth,
        routing=raw.routing,
        tool_overrides=raw.tool_overrides,
        timeout_seconds=raw.timeout_seconds,
        supersedes_version_id=None,
        payload_checksum=checksum,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    )
    session.add_all([asset, version])
    await session.flush()
    session.add_all(
        [
            McpCredentialSlotRow(
                id=_stable_id(f"{entry.source_key}:version:{entry.version}:slot:{slot.name}"),
                mcp_server_version_id=version_id,
                name=slot.name,
                purpose=slot.purpose,
                payload_schema=slot.payload_schema,
                required=slot.required,
            )
            for slot in raw.credential_slots
        ]
    )
    await session.flush()
    version.workflow_status = "published"
    await session.flush()
    asset.current_published_version_id = version_id
    await session.flush()
    return True


async def bootstrap_system_assets(session_factory: Callable[[], AsyncSession]) -> BootstrapResult:
    """Seed canonical system assets atomically and return a public summary."""

    catalog = load_bootstrap_catalog()
    counts = Counter(entry.kind for entry in catalog.entries)
    created = 0
    seeders = {"skill": _seed_skill, "mcp": _seed_mcp, "agent": _seed_agent}
    try:
        async with session_factory() as session, session.begin():
            await CatalogStateRepository(session).ensure_and_lock()
            await _ensure_builtin_principal(session)
            for entry in sorted(catalog.entries, key=lambda item: ({"skill": 0, "mcp": 1, "agent": 2}[item.kind], item.source_key)):
                created += int(await seeders[entry.kind](session, catalog, entry))
    except BootstrapConflict:
        raise
    except IntegrityError as error:
        raise BootstrapConflict("existing PostgreSQL state conflicts with canonical bootstrap") from error

    result_counts = MappingProxyType({kind: counts.get(kind, 0) for kind in ("agent", "skill", "mcp")})
    return BootstrapResult(digest=catalog_digest(catalog), counts=result_counts, created=created)


__all__ = [
    "BUILTIN_ASSET_EMAIL",
    "BUILTIN_ASSET_USER_ID",
    "BootstrapConflict",
    "BootstrapResult",
    "bootstrap_system_assets",
]
