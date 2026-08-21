from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkAssetStale
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import CreateProject, ProjectRole
from app.projects.repository import ProjectRepository
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.bootstrap import catalog as catalog_module
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.skill_archive import dump_skill_archive
from app.shared_assets.models import (
    AgentPayload,
    AssetScope,
    SkillArchiveFile,
    SkillAssetRef,
)
from app.shared_assets.skill_credential_policy import SkillCredentialBindingInput
from app.shared_assets.skill_credential_service import (
    SkillCredentialBindingService,
)
from deerflow.persistence.projects import ProjectMembershipRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    AssetCatalogStateRow,
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
    McpServerRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user import UserRow
from scripts.upgrade_system_assets import upgrade_system_assets


def _skill_archive(
    name: str,
    description: str,
    release_note: str,
    *,
    required_secrets: tuple[str, ...] = (),
) -> bytes:
    secret_frontmatter = ""
    if required_secrets:
        secret_frontmatter = "required-secrets:\n" + "".join(f"  - name: {secret}\n    optional: false\n" for secret in required_secrets)
    return dump_skill_archive(
        (
            SkillArchiveFile(
                path="SKILL.md",
                content=(f"---\nname: {name}\ndescription: {description}\n{secret_frontmatter}---\n\n# {name}\n\nUse reviewed inputs.\n").encode(),
                media_type="text/markdown",
            ),
            SkillArchiveFile(
                path="references/release.txt",
                content=release_note.encode(),
                media_type="text/plain",
            ),
        )
    )


def _entry(name: str, version: int, payload: bytes) -> dict[str, object]:
    return {
        "source_key": f"builtin:skill:{name}",
        "kind": "skill",
        "slug": name,
        "display_name": name,
        "version": version,
        "payload_path": f"content/{name}-v{version}.skill.json",
        "payload_format": "skill_archive_v1",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _agent_payload(
    description: str,
    *,
    skill_source_key: str,
    tool_groups: tuple[str, ...] = (),
) -> bytes:
    return json.dumps(
        {
            "description": description,
            "soul": "Build the requested governed asset.",
            "model_ref": "default",
            "tool_groups": list(tool_groups),
            "skill_source_keys": [skill_source_key],
            "mcp_source_keys": [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _agent_entry(name: str, version: int, payload: bytes) -> dict[str, object]:
    return {
        "source_key": f"builtin:agent:{name}",
        "kind": "agent",
        "slug": name,
        "display_name": name,
        "version": version,
        "payload_path": f"content/{name}-v{version}.agent.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_catalog(
    root: Path,
    *,
    schema_version: int,
    name: str,
    releases: tuple[tuple[int, bytes], ...],
) -> None:
    root.mkdir(parents=True)
    entries: list[dict[str, object]] = []
    for version, payload in releases:
        entry = _entry(name, version, payload)
        destination = root / str(entry["payload_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        entries.append(entry)
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _mcp_payload() -> bytes:
    return json.dumps(
        {
            "description": "Canonical ActWeave documentation MCP definition.",
            "transport": "http",
            "url": "https://docs.deerflow.invalid/mcp",
            "env": {},
            "headers": {},
            "oauth": {},
            "routing": {"namespace": "deerflow-docs"},
            "tool_overrides": {},
            "timeout_seconds": 30,
            "credential_slots": [
                {
                    "name": "api-key",
                    "purpose": "Authenticate documentation requests when configured.",
                    "payload_schema": {"headers": ["X-DEERFLOW-DOCS-KEY"]},
                    "required": False,
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _mcp_entry(payload: bytes) -> dict[str, object]:
    return {
        "source_key": "builtin:mcp:deerflow-docs",
        "kind": "mcp",
        "slug": "deerflow-docs",
        "display_name": "ActWeave Docs",
        "version": 1,
        "payload_path": "content/deerflow-docs-v1.mcp.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_entries(root: Path, *, schema_version: int, entries: list[dict[str, object]], payloads: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in payloads.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _json_snapshot(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class _AcceptingAgentCatalogValidator:
    async def validate(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _add_project_credential(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    env_fields: tuple[str, ...],
) -> uuid.UUID:
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    credential = CredentialRow(
        id=credential_id,
        scope="project",
        project_id=project_id,
        name=f"system-skill-upgrade-{uuid.uuid4().hex[:8]}",
        display_name="System Skill Upgrade Credential",
        credential_type="skill_auth",
        status="active",
        is_delete=False,
        current_version_id=None,
        version=1,
        created_by_user_id=str(user_id),
    )
    session.add(credential)
    await session.flush()
    session.add(
        CredentialVersionRow(
            id=version_id,
            credential_id=credential_id,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": list(env_fields)},
            created_by_user_id=str(user_id),
        )
    )
    await session.flush()
    session.add(
        CredentialEnvelopeRow(
            credential_version_id=version_id,
            envelope_generation=1,
            key_id="system-skill-upgrade-test",
            nonce=b"n" * 12,
            ciphertext=b"c" * 32,
            is_active=True,
            created_by_user_id=str(user_id),
        )
    )
    credential.current_version_id = version_id
    await session.flush()
    return version_id


async def _create_private_thread(
    factory: async_sessionmaker[AsyncSession],
    context: ProjectContext,
    thread_id: str,
    agent_id: uuid.UUID,
) -> None:
    private = PrivateWorkContext.from_project(context)
    async with factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=private.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(agent_id, "project"),
        )


@dataclass(frozen=True)
class _SkillState:
    asset_id: uuid.UUID
    current_version_id: uuid.UUID
    asset_revision: int
    asset: tuple[object, ...]
    versions: tuple[tuple[object, ...], ...]
    files: tuple[tuple[object, ...], ...]
    binding: tuple[object, ...]
    catalog_state: tuple[int, datetime]


@dataclass(frozen=True)
class _CanonicalSkillState:
    asset_id: uuid.UUID
    current_version_id: uuid.UUID
    asset_revision: int
    asset_updated_at: datetime
    versions: tuple[tuple[object, ...], ...]
    files: tuple[tuple[object, ...], ...]
    catalog_state: tuple[int, datetime]


def _has_sqlstate(error: BaseException, expected: str) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == expected:
            return True
        for chained in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(chained, BaseException):
                pending.append(chained)
    return False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_packaged_v3_catalog_bootstraps_and_reruns_idempotently(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await bootstrap_service.bootstrap_system_assets(factory)
        second = await bootstrap_service.bootstrap_system_assets(factory)

        assert first.counts["skill"] > 0
        assert first.counts["agent"] > 0
        assert first.counts["mcp"] == 0
        assert first.applied_changes == sum(first.counts.values())
        assert second.counts == first.counts
        assert second.digest == first.digest
        assert second.applied_changes == 0

        async with factory() as session:
            leftover = (
                await session.execute(
                    select(McpServerRow).where(
                        McpServerRow.source_key == "builtin:mcp:deerflow-docs",
                    )
                )
            ).scalar_one_or_none()
            assert leftover is None

            builder = (
                await session.execute(
                    select(AgentRow).where(
                        AgentRow.source_key == "builtin:agent:skill-builder",
                    )
                )
            ).scalar_one()
            builder_versions = tuple((await session.execute(select(AgentVersionRow).where(AgentVersionRow.agent_id == builder.id).order_by(AgentVersionRow.version_number))).scalars().all())
            assert [version.version_number for version in builder_versions] == [1]
            assert builder.current_version_id == builder_versions[0].id
            assert builder.revision == 1
            assert builder_versions[0].tool_groups == [
                "web",
                "file:read",
                "file:write",
                "bash",
                "task",
            ]
            assert builder_versions[0].supersedes_version_id is None

            creator = (
                await session.execute(
                    select(SkillRow).where(
                        SkillRow.source_key == "builtin:skill:skill-creator",
                    )
                )
            ).scalar_one()
            skill_refs = (
                (
                    await session.execute(
                        select(AgentVersionSkillRefRow)
                        .where(
                            AgentVersionSkillRefRow.agent_version_id.in_(tuple(version.id for version in builder_versions)),
                        )
                        .order_by(AgentVersionSkillRefRow.agent_version_id)
                    )
                )
                .scalars()
                .all()
            )
            assert [
                (
                    reference.skill_asset_scope,
                    reference.skill_asset_id,
                    reference.sort_order,
                )
                for reference in skill_refs
            ] == [("system", creator.id, 0)]
            mcp_refs = (
                (
                    await session.execute(
                        select(AgentVersionMcpRefRow).where(
                            AgentVersionMcpRefRow.agent_version_id.in_(tuple(version.id for version in builder_versions)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert mcp_refs == []
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_bootstrap_archives_removed_actweave_docs_mcp(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets.contexts import SystemAssetReadContext
    from app.shared_assets.mcp_repository import McpRepository

    name = f"kept-skill-{uuid.uuid4().hex[:8]}"
    skill_payload = _skill_archive(name, "Kept skill.", "release one\n")
    mcp_payload = _mcp_payload()
    skill_entry = _entry(name, 1, skill_payload)
    mcp_entry = _mcp_entry(mcp_payload)
    catalog_root = tmp_path / "catalog"
    _write_entries(
        catalog_root,
        schema_version=3,
        entries=[skill_entry, mcp_entry],
        payloads={
            str(skill_entry["payload_path"]): skill_payload,
            str(mcp_entry["payload_path"]): mcp_payload,
        },
    )
    monkeypatch.setattr(catalog_module, "_package_root", lambda: catalog_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        seeded = await bootstrap_service.bootstrap_system_assets(factory)
        assert seeded.counts["mcp"] == 1

        _write_entries(
            catalog_root,
            schema_version=3,
            entries=[skill_entry],
            payloads={str(skill_entry["payload_path"]): skill_payload},
        )
        retired = await bootstrap_service.bootstrap_system_assets(factory)
        assert retired.counts["mcp"] == 0

        async with factory() as session:
            asset = (
                await session.execute(
                    select(McpServerRow).where(
                        McpServerRow.source_key == "builtin:mcp:deerflow-docs",
                    )
                )
            ).scalar_one()
            assert asset.status == "archived"
            enabled_bindings = (
                (
                    await session.execute(
                        select(ProjectSystemMcpBindingRow).where(
                            ProjectSystemMcpBindingRow.system_mcp_server_id == asset.id,
                            ProjectSystemMcpBindingRow.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert enabled_bindings == []
            visible = await McpRepository(session).list_system_visible(
                SystemAssetReadContext(user_id=uuid.uuid4(), request_id="retire-mcp"),
            )
            assert all(row.source_key != "builtin:mcp:deerflow-docs" for row in visible)
    finally:
        await engine.dispose()


async def _canonical_skill_state(
    factory: async_sessionmaker[AsyncSession],
    *,
    source_key: str,
) -> _CanonicalSkillState:
    async with factory() as session:
        asset = (await session.execute(select(SkillRow).where(SkillRow.source_key == source_key))).scalar_one()
        versions = tuple((await session.execute(select(SkillVersionRow).where(SkillVersionRow.skill_id == asset.id).order_by(SkillVersionRow.version_number))).scalars().all())
        files = tuple(
            (
                await session.execute(
                    select(SkillVersionFileRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillVersionFileRow.skill_version_id,
                    )
                    .where(SkillVersionRow.skill_id == asset.id)
                    .order_by(
                        SkillVersionRow.version_number,
                        SkillVersionFileRow.path,
                    )
                )
            )
            .scalars()
            .all()
        )
        catalog_state = await session.get(AssetCatalogStateRow, 1)
        assert catalog_state is not None
        assert asset.current_version_id is not None
        return _CanonicalSkillState(
            asset_id=asset.id,
            current_version_id=asset.current_version_id,
            asset_revision=asset.revision,
            asset_updated_at=asset.updated_at,
            versions=tuple(
                (
                    row.id,
                    row.version_number,
                    row.supersedes_version_id,
                    row.payload_checksum,
                    row.created_at,
                )
                for row in versions
            ),
            files=tuple(
                (
                    row.skill_version_id,
                    row.path,
                    row.media_type,
                    row.size_bytes,
                    row.sha256,
                    bytes(row.content),
                )
                for row in files
            ),
            catalog_state=(
                catalog_state.generation,
                catalog_state.updated_at,
            ),
        )


async def _skill_state(
    factory: async_sessionmaker[AsyncSession],
    *,
    source_key: str,
    project_id: uuid.UUID,
) -> _SkillState:
    async with factory() as session:
        asset = (await session.execute(select(SkillRow).where(SkillRow.source_key == source_key))).scalar_one()
        versions = tuple((await session.execute(select(SkillVersionRow).where(SkillVersionRow.skill_id == asset.id).order_by(SkillVersionRow.version_number))).scalars().all())
        files = tuple(
            (
                await session.execute(
                    select(SkillVersionFileRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillVersionFileRow.skill_version_id,
                    )
                    .where(SkillVersionRow.skill_id == asset.id)
                    .order_by(
                        SkillVersionRow.version_number,
                        SkillVersionFileRow.path,
                    )
                )
            )
            .scalars()
            .all()
        )
        bindings = tuple(
            (
                await session.execute(
                    select(ProjectSystemSkillBindingRow).where(
                        ProjectSystemSkillBindingRow.project_id == project_id,
                        ProjectSystemSkillBindingRow.system_skill_id == asset.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(bindings) == 1
        binding = bindings[0]
        catalog_state = await session.get(AssetCatalogStateRow, 1)
        assert catalog_state is not None
        assert asset.current_version_id is not None

        return _SkillState(
            asset_id=asset.id,
            current_version_id=asset.current_version_id,
            asset_revision=asset.revision,
            asset=(
                asset.id,
                asset.scope,
                asset.project_id,
                asset.slug,
                asset.display_name,
                asset.status,
                asset.current_version_id,
                asset.revision,
                asset.source_key,
                asset.created_by_user_id,
                asset.created_at,
                asset.updated_at,
            ),
            versions=tuple(
                (
                    row.id,
                    row.skill_id,
                    row.version_number,
                    row.description,
                    _json_snapshot(row.frontmatter),
                    row.compatibility,
                    _json_snapshot(row.secret_requirements),
                    row.scan_decision,
                    _json_snapshot(row.scan_summary),
                    row.supersedes_version_id,
                    row.payload_checksum,
                    row.created_by_user_id,
                    row.created_at,
                    row.revoked_at,
                    row.revoked_by_user_id,
                    row.revocation_reason_code,
                )
                for row in versions
            ),
            files=tuple(
                (
                    row.skill_version_id,
                    row.path,
                    row.media_type,
                    row.size_bytes,
                    row.sha256,
                    bytes(row.content),
                )
                for row in files
            ),
            binding=(
                binding.project_id,
                binding.system_skill_id,
                binding.system_asset_scope,
                binding.enabled,
                binding.version,
                binding.created_by_user_id,
                binding.updated_by_user_id,
                binding.created_at,
                binding.updated_at,
            ),
            catalog_state=(
                catalog_state.generation,
                catalog_state.updated_at,
            ),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_system_skill_upgrade_preserves_binding_and_reruns_idempotently(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"bootstrap-evolution-{uuid.uuid4().hex[:8]}"
    source_key = f"builtin:skill:{name}"
    version_one_payload = _skill_archive(
        name,
        "Original reviewed behavior.",
        "release one\n",
    )
    version_two_payload = _skill_archive(
        name,
        "Improved reviewed behavior.",
        "release two\n",
    )
    version_one_root = tmp_path / "catalog-v1"
    version_two_root = tmp_path / "catalog-v2"
    _write_catalog(
        version_one_root,
        schema_version=1,
        name=name,
        releases=((1, version_one_payload),),
    )
    _write_catalog(
        version_two_root,
        schema_version=2,
        name=name,
        releases=((1, version_two_payload),),
    )
    active_root = version_one_root
    monkeypatch.setattr(catalog_module, "_package_root", lambda: active_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    try:
        first = await bootstrap_service.bootstrap_system_assets(factory)
        assert first.applied_changes == 1

        async with factory.begin() as session:
            session.add(
                UserRow(
                    id=str(owner_id),
                    email=f"{owner_id.hex}@example.test",
                    password_hash=None,
                    system_role="user",
                    oauth_provider=None,
                    oauth_id=None,
                    needs_setup=False,
                    token_version=0,
                )
            )
        async with factory() as session:
            project = await ProjectRepository(session).create_with_admin(
                owner_id,
                CreateProject(
                    slug=f"bootstrap-project-{uuid.uuid4().hex[:8]}",
                    display_name="Bootstrap Evolution Project",
                ),
                "req-system-skill-bootstrap-v1",
            )

        before_upgrade = await _skill_state(
            factory,
            source_key=source_key,
            project_id=project.project_id,
        )
        assert before_upgrade.asset_revision == 1
        assert len(before_upgrade.versions) == 1
        assert before_upgrade.versions[0][2] == 1
        version_one_id = before_upgrade.versions[0][0]
        assert before_upgrade.current_version_id == version_one_id
        assert before_upgrade.binding[3:5] == (True, 1)

        active_root = version_two_root
        upgraded = await upgrade_system_assets(migrated_postgres_database_url)
        assert upgraded.applied_changes == 1
        assert upgraded.digest != first.digest

        after_upgrade = await _skill_state(
            factory,
            source_key=source_key,
            project_id=project.project_id,
        )
        assert after_upgrade.asset_id == before_upgrade.asset_id
        assert after_upgrade.asset_revision == 2
        assert len(after_upgrade.versions) == 1
        assert after_upgrade.versions[0][0] == version_one_id
        assert after_upgrade.versions[0][2] == 1
        assert after_upgrade.current_version_id == version_one_id
        assert after_upgrade.versions[0][9] is None
        assert after_upgrade.versions[0][10] != before_upgrade.versions[0][10]
        assert after_upgrade.files != before_upgrade.files
        assert after_upgrade.binding == before_upgrade.binding
        assert after_upgrade.catalog_state[0] > before_upgrade.catalog_state[0]

        repeated = await upgrade_system_assets(migrated_postgres_database_url)
        assert repeated.applied_changes == 0
        assert repeated.digest == upgraded.digest
        after_rerun = await _skill_state(
            factory,
            source_key=source_key,
            project_id=project.project_id,
        )
        assert after_rerun == after_upgrade
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_system_skill_upgrade_rechecks_changed_credential_declarations_for_new_runs(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"credential-evolution-{uuid.uuid4().hex[:8]}"
    release_one = _skill_archive(
        name,
        "Requires one configured token.",
        "release one\n",
        required_secrets=("API_TOKEN",),
    )
    release_two = _skill_archive(
        name,
        "Adds one newly required token.",
        "release two\n",
        required_secrets=("API_TOKEN", "NEW_TOKEN"),
    )
    version_one_root = tmp_path / "credential-catalog-v1"
    version_two_root = tmp_path / "credential-catalog-v2"
    _write_catalog(
        version_one_root,
        schema_version=1,
        name=name,
        releases=((1, release_one),),
    )
    _write_catalog(
        version_two_root,
        schema_version=2,
        name=name,
        releases=((1, release_two),),
    )
    active_root = version_one_root
    monkeypatch.setattr(catalog_module, "_package_root", lambda: active_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    editor_id = uuid.uuid4()
    try:
        await bootstrap_service.bootstrap_system_assets(factory)
        async with factory.begin() as session:
            session.add_all(
                (
                    UserRow(
                        id=str(admin_id),
                        email=f"{admin_id.hex}@example.test",
                        username=f"u{admin_id.hex[:8]}",
                        password_hash=None,
                        system_role="user",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                    ),
                    UserRow(
                        id=str(editor_id),
                        email=f"{editor_id.hex}@example.test",
                        username=f"u{editor_id.hex[:8]}",
                        password_hash=None,
                        system_role="user",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                    ),
                )
            )
        async with factory() as session:
            admin = await ProjectRepository(session).create_with_admin(
                admin_id,
                CreateProject(
                    slug=f"credential-evolution-{uuid.uuid4().hex[:8]}",
                    display_name="Credential Evolution Project",
                ),
                "req-system-skill-credential-upgrade",
            )
        editor_membership_id = uuid.uuid4()
        async with factory.begin() as session:
            session.add(
                ProjectMembershipRow(
                    id=editor_membership_id,
                    project_id=admin.project_id,
                    user_id=str(editor_id),
                    role="editor",
                    status="active",
                    version=1,
                )
            )
            credential_version_id = await _add_project_credential(
                session,
                project_id=admin.project_id,
                user_id=admin_id,
                env_fields=("API_TOKEN",),
            )
            skill_asset, skill_version = (
                await session.execute(
                    select(SkillRow, SkillVersionRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.id == SkillRow.current_version_id,
                    )
                    .where(SkillRow.source_key == f"builtin:skill:{name}")
                )
            ).one()

        configured = await SkillCredentialBindingService(
            factory,
        ).replace_for_version(
            admin,
            skill_asset.id,
            skill_version.id,
            (
                SkillCredentialBindingInput(
                    "API_TOKEN",
                    credential_version_id,
                    "API_TOKEN",
                ),
            ),
            expected_revision=0,
        )
        assert [(item.name, item.mapping_status) for item in configured.requirements] == [("API_TOKEN", "configured")]

        agent_service = AgentService(
            factory,
            catalog_validator=_AcceptingAgentCatalogValidator(),
        )
        created_agent = await agent_service.create_project(
            admin,
            CreateAgent(
                slug=f"credential-upgrade-agent-{uuid.uuid4().hex[:8]}",
                display_name="Credential Upgrade Agent",
            ),
            AgentPayload(
                description="Uses the evolving System Skill.",
                soul="Use the admitted System Skill.",
                model_ref="f605d8f9-0ec1-53f1-9b1f-4287626b1e12",
                tool_groups=(),
                skill_refs=(
                    SkillAssetRef(
                        AssetScope.SYSTEM,
                        skill_asset.id,
                    ),
                ),
                mcp_version_ids=(),
            ),
        )
        await agent_service.activate_version(
            admin,
            created_agent.asset.id,
            created_agent.version.id,
            expected_asset_version=created_agent.asset.revision,
        )

        private = PrivateWorkContext.from_project(admin)
        baseline_thread = f"system-skill-ready-{uuid.uuid4()}"
        await _create_private_thread(
            factory,
            admin,
            baseline_thread,
            created_agent.asset.id,
        )
        await PrivateRunAdmissionService(factory).admit(
            private,
            baseline_thread,
            PrivateRunCreate(run_id=f"system-skill-ready-run-{uuid.uuid4()}"),
        )

        active_root = version_two_root
        upgraded = await upgrade_system_assets(
            migrated_postgres_database_url,
        )
        assert upgraded.applied_changes == 1

        admin_view = await SkillCredentialBindingService(factory).get_exact(
            admin,
            skill_asset.id,
            skill_version.id,
        )
        assert [(item.name, item.mapping_status) for item in admin_view.requirements] == [
            ("API_TOKEN", "configured"),
            ("NEW_TOKEN", "missing"),
        ]

        editor = ProjectContext(
            user_id=editor_id,
            project_id=admin.project_id,
            membership_id=editor_membership_id,
            role=ProjectRole.EDITOR,
            capabilities=capabilities_for(ProjectRole.EDITOR),
            membership_version=1,
            request_id="req-editor-system-skill-readiness",
        )
        editor_view = await SkillCredentialBindingService(factory).get_exact(
            editor,
            skill_asset.id,
            skill_version.id,
        )
        assert [(item.name, item.mapping_status) for item in editor_view.requirements] == [
            ("API_TOKEN", "configured"),
            ("NEW_TOKEN", "missing"),
        ]
        assert all(item.credential_id is None and item.credential_version_id is None and item.source_env_field_name is None and item.eligible_credentials == () for item in editor_view.requirements)

        blocked_thread = f"system-skill-incomplete-{uuid.uuid4()}"
        await _create_private_thread(
            factory,
            admin,
            blocked_thread,
            created_agent.asset.id,
        )
        with pytest.raises(PrivateWorkAssetStale):
            await PrivateRunAdmissionService(factory).admit(
                private,
                blocked_thread,
                PrivateRunCreate(
                    run_id=f"system-skill-incomplete-run-{uuid.uuid4()}",
                ),
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_system_agent_upgrade_preserves_v1_pin_and_reruns_idempotently(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    skill_name = f"agent-dependency-{suffix}"
    agent_name = f"bootstrap-agent-{suffix}"
    skill_payload = _skill_archive(
        skill_name,
        "Pinned Agent dependency.",
        "release one\n",
    )
    skill_entry = _entry(skill_name, 1, skill_payload)
    version_one_payload = _agent_payload(
        "Original Agent release.",
        skill_source_key=str(skill_entry["source_key"]),
    )
    version_two_payload = _agent_payload(
        "Improved Agent release.",
        skill_source_key=str(skill_entry["source_key"]),
        tool_groups=("web", "file:read"),
    )
    agent_one = _agent_entry(agent_name, 1, version_one_payload)
    agent_two = _agent_entry(agent_name, 1, version_two_payload)
    version_one_root = tmp_path / "agent-catalog-v1"
    version_two_root = tmp_path / "agent-catalog-v2"
    _write_entries(
        version_one_root,
        schema_version=3,
        entries=[skill_entry, agent_one],
        payloads={
            str(skill_entry["payload_path"]): skill_payload,
            str(agent_one["payload_path"]): version_one_payload,
        },
    )
    _write_entries(
        version_two_root,
        schema_version=3,
        entries=[skill_entry, agent_two],
        payloads={
            str(skill_entry["payload_path"]): skill_payload,
            str(agent_two["payload_path"]): version_two_payload,
        },
    )
    active_root = version_one_root
    monkeypatch.setattr(catalog_module, "_package_root", lambda: active_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    source_key = str(agent_one["source_key"])
    try:
        first = await bootstrap_service.bootstrap_system_assets(factory)
        assert first.applied_changes == 2

        async with factory.begin() as session:
            session.add(
                UserRow(
                    id=str(owner_id),
                    email=f"{owner_id.hex}@example.test",
                    password_hash=None,
                    system_role="user",
                    oauth_provider=None,
                    oauth_id=None,
                    needs_setup=False,
                    token_version=0,
                )
            )
        async with factory() as session:
            project = await ProjectRepository(session).create_with_admin(
                owner_id,
                CreateProject(
                    slug=f"agent-upgrade-{suffix}",
                    display_name="Agent Bootstrap Evolution Project",
                ),
                "req-system-agent-bootstrap-v1",
            )
        async with factory.begin() as session:
            asset = (await session.execute(select(AgentRow).where(AgentRow.source_key == source_key))).scalar_one()
            version_one = (
                await session.execute(
                    select(AgentVersionRow).where(
                        AgentVersionRow.agent_id == asset.id,
                        AgentVersionRow.version_number == 1,
                    )
                )
            ).scalar_one()
            session.add(
                ProjectSystemAgentBindingRow(
                    project_id=project.project_id,
                    system_agent_id=asset.id,
                    enabled=True,
                    created_by_user_id=str(owner_id),
                    updated_by_user_id=str(owner_id),
                )
            )

        version_one_snapshot = (
            version_one.id,
            version_one.description,
            tuple(version_one.tool_groups),
            version_one.payload_checksum,
            version_one.created_at,
        )
        active_root = version_two_root
        upgraded = await upgrade_system_assets(migrated_postgres_database_url)
        assert upgraded.applied_changes == 1

        async with factory() as session:
            asset = (await session.execute(select(AgentRow).where(AgentRow.source_key == source_key))).scalar_one()
            versions = tuple((await session.execute(select(AgentVersionRow).where(AgentVersionRow.agent_id == asset.id).order_by(AgentVersionRow.version_number))).scalars().all())
            binding = await session.get(
                ProjectSystemAgentBindingRow,
                (project.project_id, asset.id),
            )
            assert binding is not None
            assert asset.revision == 2
            assert asset.current_version_id == versions[0].id
            assert [version.version_number for version in versions] == [1]
            assert versions[0].supersedes_version_id is None
            assert versions[0].id == version_one_snapshot[0]
            assert versions[0].description != version_one_snapshot[1]
            assert tuple(versions[0].tool_groups) != version_one_snapshot[2]
            assert versions[0].payload_checksum != version_one_snapshot[3]
            assert versions[0].created_at == version_one_snapshot[4]
            assert binding.enabled is True
            assert binding.version == 1
            upgraded_snapshot = (
                asset.current_version_id,
                asset.revision,
                tuple(
                    (
                        version.id,
                        version.version_number,
                        version.supersedes_version_id,
                        version.payload_checksum,
                    )
                    for version in versions
                ),
                binding.version,
            )

        repeated = await upgrade_system_assets(migrated_postgres_database_url)
        assert repeated.applied_changes == 0
        async with factory() as session:
            asset = (await session.execute(select(AgentRow).where(AgentRow.source_key == source_key))).scalar_one()
            versions = tuple((await session.execute(select(AgentVersionRow).where(AgentVersionRow.agent_id == asset.id).order_by(AgentVersionRow.version_number))).scalars().all())
            binding = await session.get(
                ProjectSystemAgentBindingRow,
                (project.project_id, asset.id),
            )
            assert binding is not None
            assert (
                asset.current_version_id,
                asset.revision,
                tuple(
                    (
                        version.id,
                        version.version_number,
                        version.supersedes_version_id,
                        version.payload_checksum,
                    )
                    for version in versions
                ),
                binding.version,
            ) == upgraded_snapshot
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_bootstrap_nowait_fails_before_writes_and_retries_after_row_unlock(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"bootstrap-nowait-{uuid.uuid4().hex[:8]}"
    source_key = f"builtin:skill:{name}"
    version_one_payload = _skill_archive(
        name,
        "Original reviewed behavior.",
        "release one\n",
    )
    version_two_payload = _skill_archive(
        name,
        "Improved reviewed behavior.",
        "release two\n",
    )
    version_one_root = tmp_path / "nowait-catalog-v1"
    version_two_root = tmp_path / "nowait-catalog-v2"
    _write_catalog(
        version_one_root,
        schema_version=1,
        name=name,
        releases=((1, version_one_payload),),
    )
    _write_catalog(
        version_two_root,
        schema_version=2,
        name=name,
        releases=((1, version_two_payload),),
    )
    active_root = version_one_root
    monkeypatch.setattr(catalog_module, "_package_root", lambda: active_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await bootstrap_service.bootstrap_system_assets(factory)
        assert first.applied_changes == 1
        before_failure = await _canonical_skill_state(
            factory,
            source_key=source_key,
        )
        assert before_failure.asset_revision == 1
        assert len(before_failure.versions) == 1

        active_root = version_two_root
        async with factory.begin() as blocker:
            locked_asset_id = (await blocker.execute(select(SkillRow.id).where(SkillRow.source_key == source_key).with_for_update(of=SkillRow))).scalar_one()
            assert locked_asset_id == before_failure.asset_id

            with pytest.raises(DBAPIError) as exc_info:
                await asyncio.wait_for(
                    bootstrap_service.bootstrap_system_assets(factory),
                    timeout=2,
                )
            assert _has_sqlstate(exc_info.value, "55P03")
            assert (
                await _canonical_skill_state(
                    factory,
                    source_key=source_key,
                )
                == before_failure
            )

        upgraded = await bootstrap_service.bootstrap_system_assets(factory)
        assert upgraded.applied_changes == 1
        after_retry = await _canonical_skill_state(
            factory,
            source_key=source_key,
        )
        assert after_retry.asset_id == before_failure.asset_id
        assert after_retry.asset_revision == 2
        assert len(after_retry.versions) == 1
        assert after_retry.versions[0][0] == before_failure.versions[0][0]
        assert after_retry.versions[0][1] == 1
        assert after_retry.versions[0][2] is None
        assert after_retry.versions[0][3] != before_failure.versions[0][3]
        assert after_retry.current_version_id == after_retry.versions[0][0]
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_concurrent_bootstraps_serialize_on_advisory_xact_lock(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"bootstrap-serialized-{uuid.uuid4().hex[:8]}"
    source_key = f"builtin:skill:{name}"
    payload = _skill_archive(
        name,
        "Reviewed serialized behavior.",
        "release one\n",
    )
    catalog_root = tmp_path / "serialized-catalog"
    _write_catalog(
        catalog_root,
        schema_version=1,
        name=name,
        releases=((1, payload),),
    )
    monkeypatch.setattr(catalog_module, "_package_root", lambda: catalog_root)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_reached_post_advisory = asyncio.Event()
    release_first = asyncio.Event()
    second_pid = asyncio.get_running_loop().create_future()
    original_lock_existing = bootstrap_service._lock_existing_canonical_assets
    post_advisory_calls = 0
    first_task = None
    second_task = None

    async def gated_lock_existing(
        session: AsyncSession,
        catalog: catalog_module.BootstrapCatalog,
    ) -> None:
        nonlocal post_advisory_calls
        post_advisory_calls += 1
        if post_advisory_calls == 1:
            first_reached_post_advisory.set()
            await asyncio.wait_for(release_first.wait(), timeout=5)
        await original_lock_existing(session, catalog)

    class _ObservedSecondSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(statement) and not second_pid.done():
                connection = await self.connection()
                pid = await connection.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
                second_pid.set_result(pid)
            return await super().execute(statement, *args, **kwargs)

    async def wait_until_advisory_wait_is_visible(pid: int) -> None:
        async with engine.connect() as probe:
            while not bool(
                await probe.scalar(
                    text(
                        """SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE pid = :pid
                              AND locktype = 'advisory'
                              AND granted IS FALSE
                        )"""
                    ),
                    {"pid": pid},
                )
            ):
                await asyncio.sleep(0.01)

    monkeypatch.setattr(
        bootstrap_service,
        "_lock_existing_canonical_assets",
        gated_lock_existing,
    )
    observed_factory = async_sessionmaker(
        engine,
        class_=_ObservedSecondSession,
        expire_on_commit=False,
    )
    try:
        first_task = asyncio.create_task(bootstrap_service.bootstrap_system_assets(factory))
        await asyncio.wait_for(first_reached_post_advisory.wait(), timeout=3)

        second_task = asyncio.create_task(bootstrap_service.bootstrap_system_assets(observed_factory))
        pid = await asyncio.wait_for(second_pid, timeout=3)
        await asyncio.wait_for(
            wait_until_advisory_wait_is_visible(pid),
            timeout=3,
        )
        assert second_task.done() is False

        release_first.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=5,
        )
        assert first_result.applied_changes == 1
        assert second_result.applied_changes == 0
        assert first_result.digest == second_result.digest
        assert post_advisory_calls == 2

        final_state = await _canonical_skill_state(
            factory,
            source_key=source_key,
        )
        assert final_state.asset_revision == 1
        assert len(final_state.versions) == 1
        assert final_state.current_version_id == final_state.versions[0][0]
    finally:
        release_first.set()
        tasks = tuple(task for task in (first_task, second_task) if task is not None)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
