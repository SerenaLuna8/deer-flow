"""Single-transaction PostgreSQL bootstrap for packaged system assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditProcess, AuditProcessContext
from app.bootstrap_identities import (
    BUILTIN_ASSET_EMAIL,
    BUILTIN_ASSET_USER_ID,
    BUILTIN_ASSET_USERNAME,
)
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.bootstrap.catalog import (
    BootstrapCatalog,
    BootstrapCatalogError,
    BootstrapEntry,
    catalog_digest,
    catalog_payload,
    load_bootstrap_catalog,
)
from app.shared_assets.bootstrap.skill_archive import load_skill_archive
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import (
    AgentPayload,
    AssetScope,
    SkillArchiveFile,
    SkillAssetRef,
)
from app.shared_assets.skill_repository import assemble_and_seal_skill_version
from app.shared_assets.skill_service import (
    _analyze_skill_files,
    normalize_skill_files,
)
from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from deerflow.persistence.projects import ProjectMembershipRow
from deerflow.persistence.shared_assets import (
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
    SystemAssetUpgradeAuditRow,
)
from deerflow.persistence.user import UserRow

_ID_NAMESPACE = uuid.UUID("6f6622dd-a1f5-5799-a2f7-d9f793ea8d2e")
_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_4153_5345
_RETIRED_SYSTEM_MCP_SOURCE_KEYS = frozenset({"builtin:mcp:deerflow-docs"})


class BootstrapConflict(RuntimeError):
    """Existing PostgreSQL state conflicts with the packaged catalog."""


class _AgentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    description: str = ""
    soul: str = Field(min_length=1)
    model_ref: str = Field(min_length=7, max_length=36)
    tool_groups: tuple[str, ...] = ()
    skill_source_keys: tuple[str, ...] = ()
    mcp_source_keys: tuple[str, ...] = ()

    @field_validator("model_ref")
    @classmethod
    def validate_model_ref(cls, value: str) -> str:
        if value != DEFAULT_MODEL_REF and exact_model_ref(value) is None:
            raise ValueError("model_ref must be default or a canonical UUID")
        return value


class _McpSlotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=63)
    purpose: str = ""
    payload_schema: dict[str, object]
    required: bool = True


class _McpPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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
    secret_slots: tuple[_McpSlotPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    digest: str
    counts: Mapping[str, int]
    applied_changes: int

    @property
    def created(self) -> int:
        """Backward-compatible truthy count for callers that only need change state."""

        return self.applied_changes


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
    return agent_payload_checksum(payload, payload_schema_version=4)


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
                username=BUILTIN_ASSET_USERNAME,
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
    elif row.email != BUILTIN_ASSET_EMAIL or row.username != BUILTIN_ASSET_USERNAME or row.password_hash is not None or row.oauth_provider is not None or row.oauth_id is not None or row.system_role != "user" or row.needs_setup:
        raise BootstrapConflict("builtin asset principal conflicts with canonical identity")
    membership = (await session.execute(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == principal_id).limit(1))).scalar_one_or_none()
    if membership is not None:
        raise BootstrapConflict("builtin asset principal cannot have project membership")

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


async def _lock_existing_canonical_assets(
    session: AsyncSession,
    catalog: BootstrapCatalog,
) -> None:
    """Lock every existing canonical asset before catalog-triggering writes.

    Project creation locks System Skills by asset id before its binding trigger
    bumps the catalog generation. Acquiring the same rows here, in the same
    order and before any catalog-triggering mutation, prevents a lock-order
    cycle during an operator-driven release upgrade.
    """

    sources_by_kind: dict[str, set[str]] = {
        "skill": set(),
        "mcp": set(),
        "agent": set(),
    }
    for entry in catalog.entries:
        sources_by_kind[entry.kind].add(entry.source_key)
    # Agent binding mutations lock their target Agent before validating and
    # locking Skill/MCP dependencies, so bootstrap uses the same global order.
    for kind in ("agent", "skill", "mcp"):
        model = {"skill": SkillRow, "mcp": McpServerRow, "agent": AgentRow}[kind]
        source_keys = sources_by_kind[kind]
        if not source_keys:
            continue
        await session.execute(select(model.id).where(model.source_key.in_(source_keys)).order_by(model.id).with_for_update(of=model, nowait=True))


async def _retire_removed_system_mcps(session: AsyncSession, catalog: BootstrapCatalog) -> None:
    """Archive packaged system MCPs that the current catalog no longer ships."""

    catalog_keys = {entry.source_key for entry in catalog.entries}
    retired_keys = tuple(sorted(_RETIRED_SYSTEM_MCP_SOURCE_KEYS - catalog_keys))
    if not retired_keys:
        return
    rows = (
        (
            await session.execute(
                select(McpServerRow)
                .where(
                    McpServerRow.scope == "system",
                    McpServerRow.project_id.is_(None),
                    McpServerRow.source_key.in_(retired_keys),
                )
                .order_by(McpServerRow.id)
                .with_for_update(of=McpServerRow)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    for asset in rows:
        asset.status = "archived"
        await session.execute(
            update(ProjectSystemMcpBindingRow)
            .where(
                ProjectSystemMcpBindingRow.system_mcp_server_id == asset.id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
            )
            .values(
                enabled=False,
                version=ProjectSystemMcpBindingRow.version + 1,
            )
        )
    await session.flush()


async def _assert_exact_version_history(
    session: AsyncSession,
    version_model,
    parent_column,
    asset_id: uuid.UUID,
    expected_entries: tuple[BootstrapEntry, ...],
) -> None:
    """Reject child versions not owned by the authenticated catalog history."""

    actual_rows = (await session.execute(select(version_model.id, version_model.version_number).where(parent_column == asset_id).order_by(version_model.version_number, version_model.id).with_for_update(of=version_model))).all()
    actual = [tuple(row) for row in actual_rows]
    expected = sorted(
        ((_version_id(entry), entry.version) for entry in expected_entries),
        key=lambda item: (item[1], item[0]),
    )
    if actual != expected:
        raise BootstrapConflict("existing system asset release history conflicts with canonical manifest")


def _validate_asset_row(
    row,
    entry: BootstrapEntry,
    *,
    expected_revision: int | None = None,
) -> None:
    revision = getattr(row, "revision", getattr(row, "version", None))
    if (
        row.id != _stable_id(entry.source_key)
        or row.scope != "system"
        or row.project_id is not None
        or row.slug != entry.slug
        or row.display_name != entry.display_name
        or row.status != "active"
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or (expected_revision is not None and revision != expected_revision)
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
    if entry.version != 1:
        raise BootstrapCatalogError("System Skills require exactly one v1")
    payload = catalog_payload(catalog, entry)
    archive_files = load_skill_archive(payload) if entry.payload_format == "skill_archive_v1" else (SkillArchiveFile("SKILL.md", payload, "text/markdown"),)
    try:
        archive_files = normalize_skill_files(
            archive_files,
            request_id=entry.source_key,
        )
    except AssetValidationFailed as error:
        raise BootstrapCatalogError("bootstrap Skill archive is invalid") from error
    preview = await asyncio.to_thread(_validated_skill_preview, entry, archive_files)
    requirements = [
        {
            "name": item.name,
            "target_env": item.target_env,
            "optional": item.optional,
        }
        for item in preview.secret_requirements
    ]
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    expected_files = tuple(sorted(archive_files, key=lambda item: item.path))
    if asset is None:
        asset = SkillRow(
            id=asset_id,
            scope="system",
            project_id=None,
            slug=entry.slug,
            display_name=entry.display_name,
            status="active",
            current_version_id=None,
            revision=1,
            source_key=entry.source_key,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
        )
        version = SkillVersionRow(
            id=version_id,
            skill_id=asset_id,
            version_number=1,
            description=preview.description,
            frontmatter=dict(preview.frontmatter),
            compatibility=preview.compatibility,
            secret_requirements=requirements,
            # Legacy non-null database columns. Skill admission no longer
            # computes or consumes static-scan metadata.
            scan_decision="allow",
            scan_summary={},
            supersedes_version_id=None,
            payload_checksum=preview.checksum,
            file_count=len(preview.file_views),
            content_size_bytes=sum(item.size_bytes for item in preview.file_views),
            files_sealed=False,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
        )
        session.add_all([asset, version])
        await session.flush()
        try:
            await assemble_and_seal_skill_version(
                session,
                version,
                _skill_file_rows(version_id, expected_files),
                request_id=entry.source_key,
            )
        except AssetValidationFailed as error:
            raise BootstrapConflict("packaged System Skill file facts failed persistence verification") from error
        asset.current_version_id = version_id
        await session.flush()
        return True

    _validate_asset_row(asset, entry)
    versions = tuple((await session.execute(select(SkillVersionRow).where(SkillVersionRow.skill_id == asset.id).with_for_update(of=SkillVersionRow))).scalars().all())
    if len(versions) != 1 or versions[0].version_number != 1 or asset.current_version_id != versions[0].id:
        raise BootstrapConflict("existing System Skill is not a single Current v1")
    version = versions[0]
    if uuid.UUID(str(version.id)) != version_id:
        raise BootstrapConflict("existing System Skill Current v1 identity is not canonical")
    persisted_files = tuple((await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == version_id).order_by(SkillVersionFileRow.path).with_for_update(of=SkillVersionFileRow))).scalars().all())
    exact = _matches(
        version,
        skill_id=asset_id,
        version_number=1,
        description=preview.description,
        frontmatter=dict(preview.frontmatter),
        compatibility=preview.compatibility,
        secret_requirements=requirements,
        supersedes_version_id=None,
        payload_checksum=preview.checksum,
        file_count=len(preview.file_views),
        content_size_bytes=sum(item.size_bytes for item in preview.file_views),
        files_sealed=True,
        created_by_user_id=str(BUILTIN_ASSET_USER_ID),
    ) and _skill_files_match(version_id, persisted_files, expected_files)
    if version.payload_checksum == preview.checksum:
        if not exact:
            raise BootstrapConflict("existing System Skill checksum has drifted content")
        return False
    raise BootstrapConflict("System Skill v1 is immutable; publish changed content under a new identity")


def _skill_file_rows(
    version_id: uuid.UUID,
    files: tuple[SkillArchiveFile, ...],
) -> tuple[SkillVersionFileRow, ...]:
    return tuple(
        SkillVersionFileRow(
            skill_version_id=version_id,
            path=item.path,
            media_type=item.media_type,
            size_bytes=len(item.content),
            sha256=hashlib.sha256(item.content).hexdigest(),
            content=item.content,
        )
        for item in files
    )


def _skill_files_match(
    version_id: uuid.UUID,
    actual: tuple[SkillVersionFileRow, ...],
    expected: tuple[SkillArchiveFile, ...],
) -> bool:
    return tuple(
        (
            row.skill_version_id,
            row.path,
            row.media_type,
            row.size_bytes,
            row.sha256,
            bytes(row.content),
        )
        for row in sorted(actual, key=lambda item: item.path)
    ) == tuple(
        (
            version_id,
            item.path,
            item.media_type,
            len(item.content),
            hashlib.sha256(item.content).hexdigest(),
            item.content,
        )
        for item in sorted(expected, key=lambda item: item.path)
    )


def _resolved_dependency_ids(
    entries_by_release: Mapping[tuple[str, int], BootstrapEntry],
    source_keys: tuple[str, ...],
    expected_kind: str,
) -> tuple[uuid.UUID, ...]:
    resolved: list[uuid.UUID] = []
    for source_key in source_keys:
        dependency = entries_by_release.get((source_key, 1))
        if dependency is None or dependency.kind != expected_kind:
            raise BootstrapCatalogError("bootstrap dependency is missing or has the wrong kind")
        resolved.append(_version_id(dependency))
    if len(resolved) != len(set(resolved)):
        raise BootstrapCatalogError("bootstrap dependencies must be unique")
    return tuple(resolved)


def _resolved_skill_refs(
    entries_by_release: Mapping[tuple[str, int], BootstrapEntry],
    source_keys: tuple[str, ...],
) -> tuple[SkillAssetRef, ...]:
    refs: list[SkillAssetRef] = []
    for source_key in source_keys:
        dependency = entries_by_release.get((source_key, 1))
        if dependency is None or dependency.kind != "skill":
            raise BootstrapCatalogError("System Agent Skill dependency is missing or not System scope")
        refs.append(
            SkillAssetRef(
                scope=AssetScope.SYSTEM,
                asset_id=_stable_id(dependency.source_key),
            )
        )
    if len(refs) != len(set(refs)):
        raise BootstrapCatalogError("bootstrap dependencies must be unique")
    return tuple(refs)


async def _seed_agent(session: AsyncSession, catalog: BootstrapCatalog, entry: BootstrapEntry) -> bool:
    if entry.version != 1:
        raise BootstrapCatalogError("System Agents require exactly one v1")
    raw = _decode_json(_AgentPayload, catalog_payload(catalog, entry))
    entries = {(item.source_key, item.version): item for item in catalog.entries}
    payload = AgentPayload(
        description=raw.description,
        soul=raw.soul,
        model_ref=raw.model_ref,
        tool_groups=raw.tool_groups,
        skill_refs=_resolved_skill_refs(entries, raw.skill_source_keys),
        mcp_version_ids=_resolved_dependency_ids(entries, raw.mcp_source_keys, "mcp"),
        payload_schema_version=4,
    )
    checksum = _agent_checksum(payload)
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    if asset is None:
        expected_skill_refs = _expected_agent_skill_refs(asset_id, payload)
        expected_mcp_refs = _expected_agent_mcp_refs(asset_id, payload)
        asset = AgentRow(
            id=asset_id,
            scope="system",
            project_id=None,
            slug=entry.slug,
            display_name=entry.display_name,
            status="active",
            definition_id=version_id,
            description=payload.description,
            agents_instructions=payload.agents_instructions,
            soul=payload.soul,
            identity=payload.identity,
            user_context=payload.user_context,
            model_ref=payload.model_ref,
            model_settings={},
            tool_groups=list(payload.tool_groups),
            payload_checksum=checksum,
            payload_schema_version=4,
            revision=1,
            source_key=entry.source_key,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
            updated_by_user_id=str(BUILTIN_ASSET_USER_ID),
        )
        session.add(asset)
        await session.flush()
        session.add_all(_agent_skill_ref_rows(expected_skill_refs))
        session.add_all(_agent_mcp_ref_rows(expected_mcp_refs))
        await session.flush()
        return True

    _validate_asset_row(asset, entry)
    if uuid.UUID(str(asset.definition_id)) != version_id:
        raise BootstrapConflict("existing System Agent Definition identity is not canonical")
    expected_skill_refs = _expected_agent_skill_refs(asset_id, payload)
    expected_mcp_refs = _expected_agent_mcp_refs(asset_id, payload)
    skill_refs = tuple((await session.execute(select(AgentSkillRefRow).where(AgentSkillRefRow.agent_id == asset_id).order_by(AgentSkillRefRow.sort_order).with_for_update(of=AgentSkillRefRow))).scalars().all())
    mcp_refs = tuple((await session.execute(select(AgentMcpRefRow).where(AgentMcpRefRow.agent_id == asset_id).order_by(AgentMcpRefRow.sort_order).with_for_update(of=AgentMcpRefRow))).scalars().all())
    exact = (
        _matches(
            asset,
            definition_id=version_id,
            description=payload.description,
            agents_instructions=payload.agents_instructions,
            soul=payload.soul,
            identity=payload.identity,
            user_context=payload.user_context,
            model_ref=payload.model_ref,
            model_settings={},
            tool_groups=list(payload.tool_groups),
            payload_checksum=checksum,
            payload_schema_version=4,
            created_by_user_id=str(BUILTIN_ASSET_USER_ID),
            updated_by_user_id=str(BUILTIN_ASSET_USER_ID),
        )
        and tuple((row.agent_id, row.skill_asset_scope, row.skill_asset_id, row.sort_order) for row in skill_refs) == expected_skill_refs
        and tuple((row.agent_id, row.mcp_server_version_id, row.sort_order) for row in mcp_refs) == expected_mcp_refs
    )
    if asset.payload_checksum == checksum:
        if not exact:
            raise BootstrapConflict("existing System Agent checksum has drifted content")
        return False

    before_checksum = asset.payload_checksum
    asset.description = payload.description
    asset.agents_instructions = payload.agents_instructions
    asset.soul = payload.soul
    asset.identity = payload.identity
    asset.user_context = payload.user_context
    asset.model_ref = payload.model_ref
    asset.model_settings = {}
    asset.tool_groups = list(payload.tool_groups)
    asset.payload_checksum = checksum
    asset.payload_schema_version = 4
    asset.updated_by_user_id = str(BUILTIN_ASSET_USER_ID)
    # The first ref DELETE may autoflush this mapped payload mutation. Advance
    # the aggregate revision before that boundary so the Schema V1 Definition
    # trigger observes one complete System upgrade transition.
    asset.revision += 1
    await session.execute(
        delete(AgentSkillRefRow).where(
            AgentSkillRefRow.agent_id == asset_id,
        )
    )
    await session.execute(
        delete(AgentMcpRefRow).where(
            AgentMcpRefRow.agent_id == asset_id,
        )
    )
    session.add_all(_agent_skill_ref_rows(expected_skill_refs))
    session.add_all(_agent_mcp_ref_rows(expected_mcp_refs))
    await session.flush()
    await _record_system_upgrade(
        session,
        kind="agent",
        asset_id=asset_id,
        version_id=version_id,
        before_checksum=before_checksum,
        after_checksum=checksum,
        package_digest=catalog_digest(catalog),
    )
    return True


def _agent_skill_ref_rows(
    refs: tuple[tuple[uuid.UUID, str, uuid.UUID, int], ...],
) -> tuple[AgentSkillRefRow, ...]:
    return tuple(
        AgentSkillRefRow(
            agent_id=agent_id,
            skill_asset_scope=scope,
            skill_asset_id=asset_id,
            sort_order=sort_order,
        )
        for agent_id, scope, asset_id, sort_order in refs
    )


def _expected_agent_skill_refs(
    agent_id: uuid.UUID,
    payload: AgentPayload,
) -> tuple[tuple[uuid.UUID, str, uuid.UUID, int], ...]:
    return tuple((agent_id, ref.scope.value, ref.asset_id, index) for index, ref in enumerate(payload.skill_refs))


def _expected_agent_mcp_refs(
    agent_id: uuid.UUID,
    payload: AgentPayload,
) -> tuple[tuple[uuid.UUID, uuid.UUID, int], ...]:
    return tuple((agent_id, mcp_id, index) for index, mcp_id in enumerate(payload.mcp_version_ids))


def _agent_mcp_ref_rows(
    refs: tuple[tuple[uuid.UUID, uuid.UUID, int], ...],
) -> tuple[AgentMcpRefRow, ...]:
    return tuple(
        AgentMcpRefRow(
            agent_id=agent_id,
            mcp_server_version_id=mcp_id,
            sort_order=sort_order,
        )
        for agent_id, mcp_id, sort_order in refs
    )


async def _record_system_upgrade(
    session: AsyncSession,
    *,
    kind: str,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    before_checksum: str,
    after_checksum: str,
    package_digest: str,
) -> None:
    operator = await session.scalar(select(text("current_user")))
    if not isinstance(operator, str) or not operator:
        raise BootstrapConflict("database operator identity is unavailable")
    session.add(
        SystemAssetUpgradeAuditRow(
            asset_kind=kind,
            asset_id=asset_id,
            version_id=version_id,
            before_checksum=before_checksum,
            after_checksum=after_checksum,
            package_digest=package_digest,
            operator_identity=operator,
        )
    )
    await session.flush()


def _mcp_checksum(raw: _McpPayload) -> str:
    canonical = {
        "args": list(raw.args),
        "command": raw.command,
        "secret_slots": [slot.model_dump(mode="json") for slot in raw.secret_slots],
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


async def _invalidate_project_mcp_secrets(
    session: AsyncSession,
    *,
    mcp_server_id: uuid.UUID,
    mcp_server_version_id: uuid.UUID,
    governance_sink: DurableSharedAssetGovernanceEventSink | None = None,
    process_context: AuditProcessContext | None = None,
) -> None:
    states = tuple(
        (
            await session.execute(
                select(ProjectMcpSecretStateRow)
                .where(
                    ProjectMcpSecretStateRow.mcp_server_id == mcp_server_id,
                    ProjectMcpSecretStateRow.mcp_server_version_id == mcp_server_version_id,
                )
                .order_by(
                    ProjectMcpSecretStateRow.project_id,
                    ProjectMcpSecretStateRow.slot_id,
                )
                .with_for_update(of=ProjectMcpSecretStateRow)
            )
        )
        .scalars()
        .all()
    )
    configured_states = tuple(state for state in states if state.current_generation_id is not None)
    if configured_states:
        if governance_sink is None or process_context is None:
            raise BootstrapConflict("System MCP Project secret invalidation requires operator audit authority")
        if process_context.process is not AuditProcess.OPERATOR:
            raise BootstrapConflict("System MCP Project secret invalidation requires operator audit authority")
    elif (governance_sink is None) != (process_context is None):
        raise BootstrapConflict("System MCP Project secret invalidation audit authority is incomplete")

    slot_names = {
        slot.id: slot.name
        for slot in (
            (
                await session.execute(
                    select(McpSecretSlotRow).where(
                        McpSecretSlotRow.mcp_server_version_id == mcp_server_version_id,
                        McpSecretSlotRow.id.in_(tuple(state.slot_id for state in configured_states)),
                    )
                )
            )
            .scalars()
            .all()
            if configured_states
            else ()
        )
    }
    if len(slot_names) != len({state.slot_id for state in configured_states}):
        raise BootstrapConflict("System MCP Project secret slot is inconsistent")

    actor_id = str(BUILTIN_ASSET_USER_ID)
    for state in states:
        generation_id = state.current_generation_id
        if generation_id is not None:
            generation = (
                await session.execute(
                    select(ProjectMcpSecretGenerationRow)
                    .where(
                        ProjectMcpSecretGenerationRow.id == generation_id,
                        ProjectMcpSecretGenerationRow.project_id == state.project_id,
                        ProjectMcpSecretGenerationRow.mcp_server_id == state.mcp_server_id,
                        ProjectMcpSecretGenerationRow.mcp_server_version_id == state.mcp_server_version_id,
                        ProjectMcpSecretGenerationRow.slot_id == state.slot_id,
                    )
                    .with_for_update(of=ProjectMcpSecretGenerationRow)
                )
            ).scalar_one_or_none()
            if generation is None:
                raise BootstrapConflict("System MCP Project secret state is inconsistent")
            revision = int(state.revision) + 1
            session.add(
                ProjectMcpSecretTombstoneRow(
                    project_id=state.project_id,
                    mcp_server_id=state.mcp_server_id,
                    mcp_server_version_id=state.mcp_server_version_id,
                    slot_id=state.slot_id,
                    destroyed_generation_id=generation.id,
                    revision=revision,
                    envelope_digest=generation.envelope_digest,
                    reason="definition_change",
                    destroyed_by_user_id=actor_id,
                )
            )
            state.current_generation_id = None
            state.revision = revision
            state.updated_by_user_id = actor_id
            state.updated_at = datetime.now(UTC)
            await session.flush()
            await session.delete(generation)
            await session.flush()
            assert governance_sink is not None
            assert process_context is not None
            await governance_sink.append_process(
                session,
                process_context=process_context,
                project_id=state.project_id,
                asset_id=state.mcp_server_id,
                version_id=state.mcp_server_version_id,
                action="mcp.secret.invalidate",
                request_id="system-mcp-definition-invalidation",
                asset_kind="mcp",
                secret_metadata={
                    "version_id": state.mcp_server_version_id,
                    "slot_id": state.slot_id,
                    "secret_name": slot_names[state.slot_id],
                    "generation_id": generation.id,
                    "revision": int(state.revision),
                    "result": "invalidated",
                    "reason": "definition_change",
                    "readiness": "unready",
                },
            )
        await session.delete(state)
    await session.flush()


def _mcp_slot_rows(
    entry: BootstrapEntry,
    version_id: uuid.UUID,
    slots: tuple[_McpSlotPayload, ...],
) -> tuple[McpSecretSlotRow, ...]:
    return tuple(
        McpSecretSlotRow(
            id=_stable_id(f"{entry.source_key}:version:{entry.version}:slot:{slot.name}"),
            mcp_server_version_id=version_id,
            name=slot.name,
            purpose=slot.purpose,
            payload_schema=slot.payload_schema,
            required=slot.required,
        )
        for slot in slots
    )


async def _seed_mcp(
    session: AsyncSession,
    catalog: BootstrapCatalog,
    entry: BootstrapEntry,
    *,
    governance_sink: DurableSharedAssetGovernanceEventSink | None = None,
    process_context: AuditProcessContext | None = None,
) -> bool:
    raw = _decode_json(_McpPayload, catalog_payload(catalog, entry))
    checksum = _mcp_checksum(raw)
    asset_id = _stable_id(entry.source_key)
    version_id = _version_id(entry)
    asset = await _existing_asset(session, entry)
    if asset is not None:
        _validate_asset_row(asset, entry)
        version = await session.get(McpServerVersionRow, version_id)
        if version is None or asset.current_published_version_id != version_id:
            raise BootstrapConflict("existing system MCP version is not canonical")
        slots = tuple((await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == version_id).order_by(McpSecretSlotRow.name).with_for_update(of=McpSecretSlotRow))).scalars().all())
        expected_slots = tuple(sorted(raw.secret_slots, key=lambda slot: slot.name))
        version_exact = _matches(
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
        )
        slots_exact = len(slots) == len(expected_slots) and all(
            _matches(
                slot,
                id=_stable_id(f"{entry.source_key}:version:{entry.version}:slot:{expected.name}"),
                mcp_server_version_id=version_id,
                name=expected.name,
                purpose=expected.purpose,
                payload_schema=expected.payload_schema,
                required=expected.required,
            )
            for slot, expected in zip(slots, expected_slots, strict=True)
        )
        await _assert_exact_version_history(
            session,
            McpServerVersionRow,
            McpServerVersionRow.mcp_server_id,
            asset_id,
            (entry,),
        )
        if version.payload_checksum == checksum:
            if not version_exact or not slots_exact:
                raise BootstrapConflict("existing System MCP checksum has drifted content")
            return False

        before_checksum = version.payload_checksum
        await _invalidate_project_mcp_secrets(
            session,
            mcp_server_id=asset_id,
            mcp_server_version_id=version_id,
            governance_sink=governance_sink,
            process_context=process_context,
        )
        await session.execute(
            delete(McpSecretSlotRow).where(
                McpSecretSlotRow.mcp_server_version_id == version_id,
            )
        )
        version.description = raw.description
        version.transport = raw.transport
        version.command = raw.command
        version.args = list(raw.args)
        version.url = raw.url
        version.non_secret_env = raw.env
        version.non_secret_headers = raw.headers
        version.oauth_metadata = raw.oauth
        version.routing = raw.routing
        version.tool_overrides = raw.tool_overrides
        version.timeout_seconds = raw.timeout_seconds
        version.payload_checksum = checksum
        session.add_all(_mcp_slot_rows(entry, version_id, raw.secret_slots))
        asset.version += 1
        await session.flush()
        await _record_system_upgrade(
            session,
            kind="mcp",
            asset_id=asset_id,
            version_id=version_id,
            before_checksum=before_checksum,
            after_checksum=checksum,
            package_digest=catalog_digest(catalog),
        )
        return True

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
    session.add_all(_mcp_slot_rows(entry, version_id, raw.secret_slots))
    await session.flush()
    version.workflow_status = "published"
    await session.flush()
    asset.current_published_version_id = version_id
    await session.flush()
    await _assert_exact_version_history(
        session,
        McpServerVersionRow,
        McpServerVersionRow.mcp_server_id,
        asset_id,
        (entry,),
    )
    return True


async def bootstrap_system_assets(
    session_factory: Callable[[], AsyncSession],
    *,
    governance_sink: DurableSharedAssetGovernanceEventSink | None = None,
    process_context: AuditProcessContext | None = None,
) -> BootstrapResult:
    """Apply canonical System Asset definitions atomically.

    ``counts`` reports unique assets by kind; ``applied_changes`` reports
    definitions created or updated in place by this invocation.
    """

    catalog = load_bootstrap_catalog()
    counts = Counter(kind for kind, _source_key in {(entry.kind, entry.source_key) for entry in catalog.entries})
    created = 0
    if (governance_sink is None) != (process_context is None):
        raise BootstrapConflict("System Asset audit authority is incomplete")
    seeders = {"skill": _seed_skill, "agent": _seed_agent}
    try:
        async with session_factory() as session, session.begin():
            # Bootstrap must not pre-lock asset_catalog_state: binding writes
            # lock an asset/version first and bump that row from a statement
            # trigger. Taking the locks in the opposite order here can
            # deadlock a project creation. A dedicated transaction-scoped
            # advisory lock serializes bootstrap callers without participating
            # in the resolver catalog lock graph.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _BOOTSTRAP_LOCK_KEY},
            )
            await session.execute(text("SELECT set_config('deerflow.system_asset_upgrade', 'on', true)"))
            await _lock_existing_canonical_assets(session, catalog)
            await _ensure_builtin_principal(session)
            for entry in sorted(
                catalog.entries,
                key=lambda item: (
                    {"skill": 0, "mcp": 1, "agent": 2}[item.kind],
                    _stable_id(item.source_key).int,
                    item.version,
                ),
            ):
                if entry.kind == "mcp":
                    changed = await _seed_mcp(
                        session,
                        catalog,
                        entry,
                        governance_sink=governance_sink,
                        process_context=process_context,
                    )
                else:
                    changed = await seeders[entry.kind](session, catalog, entry)
                created += int(changed)
            await _retire_removed_system_mcps(session, catalog)
    except BootstrapConflict:
        raise
    except IntegrityError as error:
        raise BootstrapConflict("existing PostgreSQL state conflicts with canonical bootstrap") from error

    result_counts = MappingProxyType({kind: counts.get(kind, 0) for kind in ("agent", "skill", "mcp")})
    return BootstrapResult(
        digest=catalog_digest(catalog),
        counts=result_counts,
        applied_changes=created,
    )


__all__ = [
    "BUILTIN_ASSET_EMAIL",
    "BUILTIN_ASSET_USER_ID",
    "BootstrapConflict",
    "BootstrapResult",
    "bootstrap_system_assets",
]
