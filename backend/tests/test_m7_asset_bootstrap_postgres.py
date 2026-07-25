from __future__ import annotations

import importlib
import importlib.resources
import inspect
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import yaml
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.shared_assets.contexts import resolve_asset_actor
from app.shared_assets.errors import AssetForbidden
from deerflow.persistence.projects import ProjectMembershipRow, ProjectRow
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

BUILTIN_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")
BUILTIN_EMAIL = "builtin-assets@deerflow.invalid"


@dataclass(frozen=True)
class M7Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker


@pytest.fixture
async def m7_database(migrated_postgres_database_url: str):
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        yield M7Database(engine, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _bootstrap_module():
    return importlib.import_module("app.shared_assets.bootstrap")


def _catalog_module():
    return importlib.import_module("app.shared_assets.bootstrap.catalog")


def _canonical_package_root() -> Path:
    return Path(str(importlib.resources.files("app.shared_assets.bootstrap")))


def _mutated_catalog_root(tmp_path: Path, mutate) -> Path:
    source = _canonical_package_root()
    root = tmp_path / "bootstrap"
    shutil.copytree(source, root)
    payload = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    mutate(payload, root)
    (root / "catalog.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_bootstrap_package_exists() -> None:
    assert importlib.util.find_spec("app.shared_assets.bootstrap") is not None


def test_packaged_agent_does_not_require_reserved_invalid_remote_mcp() -> None:
    catalog_module = _catalog_module()
    catalog = catalog_module.load_bootstrap_catalog()
    entries = {entry.source_key: entry for entry in catalog.entries}
    agent_entry = entries["builtin:agent:project-assistant"]
    assert agent_entry.display_name == "Main"
    agent_payload = json.loads(
        catalog_module.catalog_payload(catalog, agent_entry),
    )
    assert agent_payload["tool_groups"] == [
        "web",
        "file:read",
        "file:write",
        "bash",
        "task",
    ]

    for source_key in agent_payload["mcp_source_keys"]:
        mcp_entry = entries[source_key]
        mcp_payload = json.loads(
            catalog_module.catalog_payload(catalog, mcp_entry),
        )
        assert not (mcp_payload["transport"] in {"http", "sse"} and (urlsplit(str(mcp_payload.get("url", ""))).hostname or "").endswith(".invalid"))


def test_packaged_catalog_contains_complete_public_skill_archives() -> None:
    archive_module = importlib.import_module("app.shared_assets.bootstrap.skill_archive")
    catalog_module = _catalog_module()
    catalog = catalog_module.load_bootstrap_catalog()
    public_root = Path(__file__).resolve().parents[2] / "skills" / "public"
    expected: dict[str, dict[str, bytes]] = {}
    for directory in sorted(path for path in public_root.iterdir() if path.is_dir()):
        skill_path = directory / "SKILL.md"
        frontmatter = yaml.safe_load(skill_path.read_text(encoding="utf-8").split("---", 2)[1])
        slug = frontmatter["name"]
        expected[slug] = {path.relative_to(directory).as_posix(): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}

    skill_entries = [entry for entry in catalog.entries if entry.kind == "skill"]
    archived_entries = [entry for entry in skill_entries if entry.payload_format == "skill_archive_v1"]

    assert len(skill_entries) == 22
    assert len(archived_entries) == 21
    assert {entry.slug for entry in archived_entries} == set(expected)
    assert "vercel-deploy" in expected
    assert "vercel-deploy-claimable" not in expected
    for entry in archived_entries:
        archive_files = archive_module.load_skill_archive(
            catalog_module.catalog_payload(catalog, entry),
        )
        assert {file.path: file.content for file in archive_files} == expected[entry.slug]


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_catalog_is_atomic_and_idempotent(m7_database: M7Database) -> None:
    first = await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    second = await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    assert first.digest == second.digest
    assert first.counts == second.counts
    assert first.created > 0
    assert second.created == 0
    assert first.counts == {"agent": 1, "skill": 22, "mcp": 1}
    async with m7_database.session_factory() as session:
        system_skill_count = await session.scalar(select(func.count()).select_from(SkillRow).where(SkillRow.scope == "system"))
        project_skill_count = await session.scalar(select(func.count()).select_from(SkillRow).where(SkillRow.scope == "project"))
        skill_file_count = await session.scalar(select(func.count()).select_from(SkillVersionFileRow))
    assert system_skill_count == 22
    assert project_skill_count == 0
    assert skill_file_count == 89


@pytest.mark.postgres
@pytest.mark.anyio
async def test_packaged_mcp_reconstructs_through_provider_and_resolver(
    m7_database: M7Database,
) -> None:
    from app.projects.capabilities import capabilities_for
    from app.projects.context import ProjectContext
    from app.projects.models import ProjectRole
    from app.shared_assets.binding_service import BindingService
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from app.shared_assets.keyring import CredentialKeyring
    from app.shared_assets.models import AssetKind, AssetSelection
    from app.shared_assets.resolver import ProjectAssetResolver

    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    provider = PostgresAssetCatalogProvider(m7_database.session_factory)
    snapshots = await provider.list_system_mcp()
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    slots = snapshot.definition["credential_slots"]
    assert len(slots) == 1
    assert slots[0]["payload_schema"] == {
        "headers": ("X-DEERFLOW-DOCS-KEY",),
    }
    assert snapshot.credential_grant_ids == ()

    async with m7_database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CredentialRow)) == 0
        assert await session.scalar(select(func.count()).select_from(ProjectSystemMcpBindingRow)) == 0

    async with m7_database.session_factory() as session, session.begin():
        user_id = await _add_normal_user(session)
        project_id = uuid.uuid4()
        membership_id = uuid.uuid4()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"packaged-mcp-{uuid.uuid4().hex[:8]}",
                display_name="Packaged MCP",
                created_by_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ProjectMembershipRow(
                id=membership_id,
                project_id=project_id,
                user_id=user_id,
                role="admin",
            )
        )

    role = ProjectRole.ADMIN
    context = ProjectContext(
        user_id=uuid.UUID(user_id),
        project_id=project_id,
        membership_id=membership_id,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="packaged-mcp",
    )
    await BindingService(m7_database.session_factory).enable(
        context,
        AssetSelection(AssetKind.MCP, snapshot.asset_id, snapshot.version_id),
    )
    keyring = CredentialKeyring(
        active_key_id="packaged-mcp-test",
        _keys={"packaged-mcp-test": b"m" * 32},
    )
    resolver = ProjectAssetResolver(m7_database.session_factory, keyring=keyring)
    resolved = await resolver.resolve_project_asset_snapshot(
        context,
        AssetSelection(AssetKind.MCP, snapshot.asset_id),
    )

    assert resolved.definition["credential_slots"][0]["payload_schema"] == {
        "headers": ("X-DEERFLOW-DOCS-KEY",),
    }
    assert (await resolver.materialize_mcp_secrets(context, resolved)).by_slot == {}


@pytest.mark.parametrize(
    ("actual", "expected", "matches"),
    [
        ({"value": False}, {"value": 0}, False),
        ({"value": 1}, {"value": 1.0}, False),
        ({"items": ["a", "b"]}, {"items": ["b", "a"]}, False),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
    ],
)
def test_bootstrap_graph_json_equality_is_type_and_order_strict(
    actual: object,
    expected: object,
    matches: bool,
) -> None:
    service = importlib.import_module("app.shared_assets.bootstrap.service")

    assert service._matches(SimpleNamespace(value=actual), value=expected) is matches


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_canonical_parent_metadata_drift(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session, session.begin():
        agent = (await session.execute(select(AgentRow).where(AgentRow.source_key == "builtin:agent:project-assistant"))).scalar_one()
        agent.version = 2

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_canonical_version_metadata_drift(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session, session.begin():
        skill = (await session.execute(select(SkillVersionRow).join(SkillRow, SkillVersionRow.skill_id == SkillRow.id).where(SkillRow.source_key == "builtin:skill:deerflow-core"))).scalar_one()
        skill.review_note = "not canonical"

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_canonical_skill_file_drift(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session, session.begin():
        await session.execute(text("ALTER TABLE skill_version_files DISABLE TRIGGER USER"))
        await session.execute(delete(SkillVersionFileRow))
        await session.execute(text("ALTER TABLE skill_version_files ENABLE TRIGGER USER"))

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_canonical_agent_reference_drift(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session, session.begin():
        await session.execute(text("ALTER TABLE agent_version_skill_refs DISABLE TRIGGER USER"))
        await session.execute(delete(AgentVersionSkillRefRow))
        await session.execute(text("ALTER TABLE agent_version_skill_refs ENABLE TRIGGER USER"))

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_canonical_mcp_version_and_slot_drift(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session, session.begin():
        mcp_version = (await session.execute(select(McpServerVersionRow).join(McpServerRow, McpServerVersionRow.mcp_server_id == McpServerRow.id).where(McpServerRow.source_key == "builtin:mcp:deerflow-docs"))).scalar_one()
        mcp_version.review_note = "not canonical"

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    async with m7_database.session_factory() as session, session.begin():
        mcp_version = (await session.execute(select(McpServerVersionRow).join(McpServerRow, McpServerVersionRow.mcp_server_id == McpServerRow.id).where(McpServerRow.source_key == "builtin:mcp:deerflow-docs"))).scalar_one()
        mcp_version.review_note = None
        await session.execute(text("ALTER TABLE mcp_version_credential_slots DISABLE TRIGGER USER"))
        await session.execute(delete(McpCredentialSlotRow))
        await session.execute(text("ALTER TABLE mcp_version_credential_slots ENABLE TRIGGER USER"))

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_agent_graph_contains_only_executable_canonical_refs(
    m7_database: M7Database,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    async with m7_database.session_factory() as session:
        version = (await session.execute(select(AgentVersionRow).join(AgentRow, AgentVersionRow.agent_id == AgentRow.id).where(AgentRow.source_key == "builtin:agent:project-assistant"))).scalar_one()
        skill_refs = (await session.execute(select(AgentVersionSkillRefRow).where(AgentVersionSkillRefRow.agent_version_id == version.id).order_by(AgentVersionSkillRefRow.sort_order))).scalars().all()
        mcp_refs = (await session.execute(select(AgentVersionMcpRefRow).where(AgentVersionMcpRefRow.agent_version_id == version.id).order_by(AgentVersionMcpRefRow.sort_order))).scalars().all()

        assert len(skill_refs) == 1
        assert skill_refs[0].sort_order == 0
        assert mcp_refs == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload, _root: payload.update({"unknown": True}),
        lambda payload, _root: payload["entries"].append(dict(payload["entries"][0])),
        lambda payload, root: (root / payload["entries"][0]["payload_path"]).write_bytes(b"tampered"),
    ],
    ids=["unknown-manifest-key", "duplicate-source-key", "digest-mismatch"],
)
def test_bootstrap_catalog_rejects_invalid_manifest(tmp_path: Path, monkeypatch, mutate) -> None:
    module = _catalog_module()
    root = _mutated_catalog_root(tmp_path, mutate)
    monkeypatch.setattr(module.resources, "files", lambda _package: root)

    with pytest.raises((ValueError, module.BootstrapCatalogError)):
        module.load_bootstrap_catalog()


def test_bootstrap_catalog_rejects_symlink_payload(tmp_path: Path, monkeypatch) -> None:
    module = _catalog_module()
    root = _mutated_catalog_root(tmp_path, lambda _payload, _root: None)
    entry = json.loads((root / "catalog.json").read_text(encoding="utf-8"))["entries"][0]
    payload_path = root / entry["payload_path"]
    real_path = root / "real-payload"
    payload_path.rename(real_path)
    payload_path.symlink_to(real_path)
    monkeypatch.setattr(module.resources, "files", lambda _package: root)

    with pytest.raises(module.BootstrapCatalogError):
        module.load_bootstrap_catalog()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_transaction_rolls_back_all_prior_entries(m7_database: M7Database) -> None:
    catalog = _bootstrap_module().load_bootstrap_catalog()
    agent_entry = next(entry for entry in catalog.entries if entry.kind == "agent")
    preexisting_user_id = str(uuid.uuid4())
    preexisting_agent_id = uuid.uuid4()
    async with m7_database.session_factory() as session, session.begin():
        session.add(
            UserRow(
                id=preexisting_user_id,
                email=f"existing-{uuid.uuid4()}@example.invalid",
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add(
            AgentRow(
                id=preexisting_agent_id,
                scope="system",
                project_id=None,
                slug=f"conflict-{uuid.uuid4().hex[:8]}",
                display_name="Conflicting source",
                status="active",
                current_published_version_id=None,
                version=1,
                source_key=agent_entry.source_key,
                created_by_user_id=preexisting_user_id,
            )
        )

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    async with m7_database.session_factory() as session:
        assert await session.get(UserRow, str(BUILTIN_USER_ID)) is None
        created_skills = await session.scalar(select(func.count()).select_from(SkillRow).where(SkillRow.source_key.like("builtin:%")))
        assert created_skills == 0
        assert await session.get(AgentRow, preexisting_agent_id) is not None


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_creates_no_credentials_or_project_bindings(m7_database: M7Database) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    async with m7_database.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CredentialRow)) == 0
        for table in (
            ProjectSystemAgentBindingRow,
            ProjectSystemSkillBindingRow,
            ProjectSystemMcpBindingRow,
        ):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.postgres
@pytest.mark.anyio
async def test_builtin_principal_is_non_login_and_has_no_project_membership(m7_database: M7Database) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    async with m7_database.session_factory() as session:
        row = await session.get(UserRow, str(BUILTIN_USER_ID))
        assert row is not None
        assert row.email == BUILTIN_EMAIL
        assert row.password_hash is None
        assert row.oauth_provider is None
        assert row.oauth_id is None
        assert row.system_role == "user"
        assert await session.scalar(select(func.count()).select_from(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(BUILTIN_USER_ID))) == 0

    with pytest.raises(AssetForbidden):
        resolve_asset_actor(
            SimpleNamespace(id=BUILTIN_USER_ID, system_role="user"),
            request_id="builtin-principal",
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_rejects_preexisting_builtin_membership(
    m7_database: M7Database,
) -> None:
    project_id = uuid.uuid4()
    async with m7_database.session_factory() as session, session.begin():
        session.add(
            UserRow(
                id=str(BUILTIN_USER_ID),
                email=BUILTIN_EMAIL,
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"builtin-conflict-{uuid.uuid4().hex[:8]}",
                display_name="Builtin conflict",
                created_by_user_id=str(BUILTIN_USER_ID),
            )
        )
        await session.flush()
        session.add(
            ProjectMembershipRow(
                project_id=project_id,
                user_id=str(BUILTIN_USER_ID),
                role="viewer",
            )
        )

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


async def _add_normal_user(session) -> str:
    user_id = str(uuid.uuid4())
    session.add(
        UserRow(
            id=user_id,
            email=f"normal-{uuid.uuid4()}@example.invalid",
            password_hash=None,
            system_role="user",
            oauth_provider=None,
            oauth_id=None,
            needs_setup=False,
            token_version=0,
        )
    )
    await session.flush()
    return user_id


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("table_kind", "actor_field"),
    [
        ("credential", "created"),
        ("credential", "revoked"),
        ("version", "created"),
        ("version", "revoked"),
        ("envelope", "created"),
        ("grant", "created"),
        ("grant", "revoked"),
    ],
)
async def test_bootstrap_rejects_builtin_credential_actor_references(
    m7_database: M7Database,
    table_kind: str,
    actor_field: str,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    builtin_id = str(BUILTIN_USER_ID)
    async with m7_database.session_factory() as session, session.begin():
        normal_id = await _add_normal_user(session)
        credential_id = uuid.uuid4()
        credential = CredentialRow(
            id=credential_id,
            scope="system",
            project_id=None,
            name=f"credential-{uuid.uuid4().hex[:10]}",
            display_name="Forbidden builtin reference",
            credential_type="api_key",
            status="revoked" if table_kind == "credential" and actor_field == "revoked" else "active",
            current_version_id=None,
            version=1,
            source_key=None,
            revoked_by_user_id=builtin_id if table_kind == "credential" and actor_field == "revoked" else None,
            created_by_user_id=builtin_id if table_kind == "credential" and actor_field == "created" else normal_id,
        )
        session.add(credential)
        await session.flush()

        version_id = uuid.uuid4()
        if table_kind in {"version", "envelope", "grant"}:
            session.add(
                CredentialVersionRow(
                    id=version_id,
                    credential_id=credential_id,
                    version_number=1,
                    status="revoked" if table_kind == "version" and actor_field == "revoked" else "active",
                    payload_schema_version=1,
                    payload_schema={"type": "object"},
                    supersedes_version_id=None,
                    revoked_by_user_id=builtin_id if table_kind == "version" and actor_field == "revoked" else None,
                    created_by_user_id=builtin_id if table_kind == "version" and actor_field == "created" else normal_id,
                )
            )
            await session.flush()

        if table_kind == "envelope":
            session.add(
                CredentialEnvelopeRow(
                    credential_version_id=version_id,
                    envelope_generation=1,
                    key_id="test-key",
                    nonce=b"0" * 12,
                    ciphertext=b"0" * 16,
                    is_active=True,
                    created_by_user_id=builtin_id,
                    rotated_from_envelope_id=None,
                )
            )
        elif table_kind == "grant":
            slot = (await session.execute(select(McpCredentialSlotRow))).scalar_one()
            session.add(
                CredentialGrantRow(
                    mcp_server_version_id=slot.mcp_server_version_id,
                    credential_slot_id=slot.id,
                    credential_version_id=version_id,
                    status="revoked" if actor_field == "revoked" else "active",
                    version=1,
                    created_by_user_id=builtin_id if actor_field == "created" else normal_id,
                    revoked_by_user_id=builtin_id if actor_field == "revoked" else None,
                )
            )

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("binding_kind", "actor_field"),
    [
        ("agent", "created"),
        ("agent", "updated"),
        ("skill", "created"),
        ("skill", "updated"),
        ("mcp", "created"),
        ("mcp", "updated"),
    ],
)
async def test_bootstrap_rejects_builtin_project_binding_actor_references(
    m7_database: M7Database,
    binding_kind: str,
    actor_field: str,
) -> None:
    await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    builtin_id = str(BUILTIN_USER_ID)
    async with m7_database.session_factory() as session, session.begin():
        normal_id = await _add_normal_user(session)
        project_id = uuid.uuid4()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"binding-conflict-{uuid.uuid4().hex[:8]}",
                display_name="Binding conflict",
                created_by_user_id=normal_id,
            )
        )
        await session.flush()
        actor_values = {
            "created_by_user_id": builtin_id if actor_field == "created" else normal_id,
            "updated_by_user_id": builtin_id if actor_field == "updated" else normal_id,
        }
        if binding_kind == "agent":
            asset = (await session.execute(select(AgentRow).where(AgentRow.source_key == "builtin:agent:project-assistant"))).scalar_one()
            binding = ProjectSystemAgentBindingRow(
                project_id=project_id,
                system_agent_id=asset.id,
                agent_version_id=asset.current_published_version_id,
                **actor_values,
            )
        elif binding_kind == "skill":
            asset = (await session.execute(select(SkillRow).where(SkillRow.source_key == "builtin:skill:deerflow-core"))).scalar_one()
            binding = ProjectSystemSkillBindingRow(
                project_id=project_id,
                system_skill_id=asset.id,
                skill_version_id=asset.current_published_version_id,
                **actor_values,
            )
        else:
            asset = (await session.execute(select(McpServerRow).where(McpServerRow.source_key == "builtin:mcp:deerflow-docs"))).scalar_one()
            binding = ProjectSystemMcpBindingRow(
                project_id=project_id,
                system_mcp_server_id=asset.id,
                mcp_server_version_id=asset.current_published_version_id,
                **actor_values,
            )
        session.add(binding)

    with pytest.raises(_bootstrap_module().BootstrapConflict):
        await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)


def test_runtime_catalog_has_no_cutover_or_filesystem_branch() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    assert not hasattr(PostgresAssetCatalogProvider, "is_cutover_enabled")
    source = inspect.getsource(PostgresAssetCatalogProvider).lower()
    assert "cutover" not in source
    assert "path(" not in source


def test_legacy_skill_storage_and_installer_imports_are_absent() -> None:
    from deerflow.skills import storage
    from deerflow.skills.types import SkillCategory

    assert not hasattr(SkillCategory, "LEGACY")
    assert not hasattr(storage, "get_or_new_" + "user_skill_storage")
    assert importlib.util.find_spec("deerflow.skills.installer") is None
    assert importlib.util.find_spec("deerflow.skills.storage.user_scoped_skill_storage") is None
    assert importlib.util.find_spec("deerflow.tools.skill_manage_tool") is None


@pytest.mark.parametrize(
    "module_name",
    ["app.gateway.app", "app.worker.app", "app.scheduler.app"],
)
def test_process_module_import_does_not_read_legacy_asset_sources(monkeypatch, module_name: str) -> None:
    from deerflow.mcp.config import ExtensionsConfig
    from deerflow.skills.storage.local_skill_storage import LocalSkillStorage

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy asset source was read during process startup import")

    monkeypatch.setattr(ExtensionsConfig, "from_file", forbidden, raising=False)
    monkeypatch.setattr(LocalSkillStorage, "load_skills", forbidden)
    module = importlib.import_module(module_name)
    importlib.reload(module)
