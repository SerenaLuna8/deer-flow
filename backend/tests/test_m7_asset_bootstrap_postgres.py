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

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.shared_assets.contexts import resolve_asset_actor
from app.shared_assets.errors import AssetForbidden
from deerflow.persistence.projects import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    CredentialRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
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


@pytest.mark.postgres
@pytest.mark.anyio
async def test_bootstrap_catalog_is_atomic_and_idempotent(m7_database: M7Database) -> None:
    first = await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)
    second = await _bootstrap_module().bootstrap_system_assets(m7_database.session_factory)

    assert first.digest == second.digest
    assert first.counts == second.counts
    assert first.created > 0
    assert second.created == 0


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
    assert not hasattr(storage, "get_or_new_user_skill_storage")
    assert importlib.util.find_spec("deerflow.skills.installer") is None
    assert importlib.util.find_spec("deerflow.skills.storage.user_scoped_skill_storage") is None
    assert importlib.util.find_spec("deerflow.tools.skill_manage_tool") is None


@pytest.mark.parametrize(
    "module_name",
    ["app.gateway.app", "app.worker.app", "app.scheduler.app"],
)
def test_process_module_import_does_not_read_legacy_asset_sources(monkeypatch, module_name: str) -> None:
    from deerflow.config.extensions_config import ExtensionsConfig
    from deerflow.skills.storage.local_skill_storage import LocalSkillStorage

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy asset source was read during process startup import")

    monkeypatch.setattr(ExtensionsConfig, "from_file", forbidden)
    monkeypatch.setattr(LocalSkillStorage, "load_skills", forbidden)
    module = importlib.import_module(module_name)
    importlib.reload(module)
