from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from dataclasses import fields

import pytest


def test_harness_catalog_protocol_exposes_only_safe_snapshot_types() -> None:
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogMcpSnapshot,
        AssetCatalogProvider,
        AssetCatalogSkillSnapshot,
    )

    assert AssetCatalogProvider is not None
    forbidden = {
        "plaintext",
        "ciphertext",
        "nonce",
        "key_id",
        "storage_locator",
        "secret_hash",
    }
    for snapshot_type in (
        AssetCatalogAgentSnapshot,
        AssetCatalogSkillSnapshot,
        AssetCatalogMcpSnapshot,
    ):
        assert forbidden.isdisjoint(field.name for field in fields(snapshot_type))


def test_catalog_provider_can_be_installed_and_cleared() -> None:
    from deerflow.assets.catalog import (
        get_asset_catalog_provider,
        set_asset_catalog_provider,
    )

    provider = object()
    set_asset_catalog_provider(provider)  # type: ignore[arg-type]
    try:
        assert get_asset_catalog_provider() is provider
    finally:
        set_asset_catalog_provider(None)
    assert get_asset_catalog_provider() is None


def test_postgres_provider_is_permanent_and_has_no_filesystem_fallback() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    assert not hasattr(PostgresAssetCatalogProvider, "is_cutover_enabled")
    source = inspect.getsource(PostgresAssetCatalogProvider).lower()
    assert "cutover" not in source
    assert "path(" not in source


@pytest.mark.asyncio
async def test_postgres_provider_returns_empty_kind_without_transition_state() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    provider = PostgresAssetCatalogProvider.for_test()
    assert await provider.list_system_skills() == ()


def _snapshots(generation: int):
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogMcpSnapshot,
        AssetCatalogScope,
        AssetCatalogSkillFile,
        AssetCatalogSkillSnapshot,
    )

    return (
        AssetCatalogAgentSnapshot(
            slug=f"agent-{generation}",
            scope=AssetCatalogScope.SYSTEM,
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            generation=generation,
            checksum="a" * 64,
            description="",
            soul="system",
            model_ref="default",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
        AssetCatalogSkillSnapshot(
            slug=f"skill-{generation}",
            scope=AssetCatalogScope.SYSTEM,
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            generation=generation,
            checksum="b" * 64,
            description="",
            files=(AssetCatalogSkillFile("SKILL.md", b"---\nname: test\ndescription: test\n---\n"),),
        ),
        AssetCatalogMcpSnapshot(
            slug=f"mcp-{generation}",
            scope=AssetCatalogScope.SYSTEM,
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            generation=generation,
            checksum="c" * 64,
            definition={"transport": "stdio", "command": "test"},
            credential_grant_ids=(),
        ),
    )


@pytest.mark.asyncio
async def test_older_generation_writer_cannot_overwrite_newer_cache() -> None:
    from app.shared_assets.catalog_provider import (
        AssetCatalogUnavailable,
        PostgresAssetCatalogProvider,
    )

    provider = PostgresAssetCatalogProvider.for_test(generation=1)
    old_mcp = _snapshots(1)[2]
    new_mcp = _snapshots(2)[2]
    provider._prepare_cache(1)

    release_old_writer = threading.Barrier(2)
    old_writer_errors: list[BaseException] = []
    new_writer_errors: list[BaseException] = []

    def write_old_generation() -> None:
        release_old_writer.wait()
        try:
            provider._store("mcp", (old_mcp,), expected_generation=1)
        except BaseException as exc:  # pragma: no branch - asserted below
            old_writer_errors.append(exc)

    old_writer = threading.Thread(target=write_old_generation)
    old_writer.start()
    provider._prepare_cache(2)
    try:
        provider._store("mcp", (new_mcp,), expected_generation=2)
    except BaseException as exc:  # pragma: no branch - asserted below
        new_writer_errors.append(exc)
    finally:
        release_old_writer.wait()
    old_writer.join(timeout=1)

    assert not old_writer.is_alive()
    assert new_writer_errors == []
    assert len(old_writer_errors) == 1
    assert isinstance(old_writer_errors[0], AssetCatalogUnavailable)
    assert provider._cached("mcp") == (new_mcp,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["publish", "suspend", "grant_revoke"])
async def test_generation_change_invalidates_all_catalog_caches(mutation: str) -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    first = _snapshots(1)
    provider = PostgresAssetCatalogProvider.for_test(
        generation=1,
        agents=(first[0],),
        skills=(first[1],),
        mcp=(first[2],),
    )
    await provider.list_system_agents()
    await provider.list_system_skills()
    await provider.list_system_mcp()
    assert provider.cache_load_counts_for_test() == {"agent": 1, "skill": 1, "mcp": 1}

    second = _snapshots(2)
    provider.replace_test_catalog(
        generation=2,
        agents=(second[0],),
        skills=(second[1],),
        mcp=(second[2],),
        mutation=mutation,
    )

    assert (await provider.list_system_agents())[0].slug == "agent-2"
    assert (await provider.list_system_skills())[0].slug == "skill-2"
    assert (await provider.list_system_mcp())[0].slug == "mcp-2"
    assert provider.cache_load_counts_for_test() == {"agent": 2, "skill": 2, "mcp": 2}


@pytest.mark.asyncio
async def test_provider_rejects_project_snapshot_on_runtime_path() -> None:
    from app.shared_assets.catalog_provider import AssetCatalogUnavailable, PostgresAssetCatalogProvider
    from deerflow.assets.catalog import AssetCatalogScope

    _agent, skill, _mcp = _snapshots(1)
    provider = PostgresAssetCatalogProvider.for_test(
        generation=1,
        skills=(skill.__class__(**{**skill.__dict__, "scope": AssetCatalogScope.PROJECT}),),
    )

    with pytest.raises(AssetCatalogUnavailable):
        await provider.list_system_skills()


@pytest.mark.asyncio
async def test_sync_lookup_runs_on_provider_owning_loop() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    _agent, skill, _mcp = _snapshots(1)
    provider = PostgresAssetCatalogProvider.for_test(
        generation=1,
        skills=(skill,),
    )
    owner_loop_id = id(asyncio.get_running_loop())

    snapshots = await asyncio.to_thread(provider.run_sync, "list_system_skills")

    assert snapshots == (skill,)
    assert provider.last_lookup_loop_id_for_test() == owner_loop_id


@pytest.mark.asyncio
async def test_provider_reuses_task7_verified_skill_archive(monkeypatch) -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider
    from app.shared_assets.models import SkillArchiveFile
    from app.shared_assets.skill_service import SkillService
    from deerflow.persistence.shared_assets import SkillRow, SkillVersionFileRow, SkillVersionRow

    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SkillRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="verified-skill",
        display_name="Verified skill",
        status="active",
        current_published_version_id=version_id,
        created_by_user_id="system",
    )
    version = SkillVersionRow(
        id=version_id,
        skill_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="verified",
        frontmatter={},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={},
        payload_checksum="e" * 64,
        created_by_user_id="system",
    )
    file_row = SkillVersionFileRow(
        skill_version_id=version_id,
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=7,
        sha256="f" * 64,
        content=b"corrupt",
    )

    class _Result:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

        def scalars(self):
            return self

    class _Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return _Result([(asset, version)] if self.calls == 1 else [file_row])

    verified_calls = []

    def _verified(record, request_id):
        verified_calls.append((record, request_id))
        return (SkillArchiveFile(path="SKILL.md", content=b"verified", media_type="text/markdown"),)

    monkeypatch.setattr(SkillService, "_verified_archive_files", staticmethod(_verified))
    snapshots = await PostgresAssetCatalogProvider._load_skills(_Session(), 11)  # type: ignore[arg-type]

    assert len(verified_calls) == 1
    assert snapshots[0].files[0].content == b"verified"


@pytest.mark.asyncio
async def test_catalog_sql_selects_only_active_assets() -> None:
    from app.shared_assets.catalog_provider import PostgresAssetCatalogProvider

    class _EmptyResult:
        def all(self):
            return []

    class _CapturingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return _EmptyResult()

    for kind, loader, table_name in (
        ("agent", PostgresAssetCatalogProvider._load_agents, "agents"),
        ("skill", PostgresAssetCatalogProvider._load_skills, "skills"),
        ("mcp", PostgresAssetCatalogProvider._load_mcp, "mcp_servers"),
    ):
        session = _CapturingSession()
        assert await loader(session, 1) == ()  # type: ignore[arg-type]
        statement = str(session.statements[0])
        assert f"{table_name}.status =" in statement, kind
        assert f"{table_name}.status !=" not in statement, kind


class _CatalogResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values

    def scalars(self):
        return self


class _CatalogSession:
    def __init__(self, *results):
        self._results = iter(results)

    async def execute(self, _statement):
        return _CatalogResult(next(self._results))


@pytest.mark.asyncio
async def test_agent_catalog_rejects_corrupt_payload_checksum() -> None:
    from app.shared_assets.catalog_provider import (
        AssetCatalogUnavailable,
        PostgresAssetCatalogProvider,
    )
    from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow

    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = AgentRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="corrupt-agent",
        display_name="Corrupt agent",
        status="active",
        current_published_version_id=version_id,
        created_by_user_id="system",
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="description",
        soul="soul",
        model_ref="default",
        tool_groups=["search"],
        payload_checksum="f" * 64,
        created_by_user_id="system",
    )
    session = _CatalogSession([(asset, version)], [], [], [], [])

    with pytest.raises(AssetCatalogUnavailable, match="agent catalog is invalid"):
        await PostgresAssetCatalogProvider._load_agents(session, 1)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_catalog_rejects_archived_row_if_backend_returns_it() -> None:
    from app.shared_assets.agent_service import AgentService
    from app.shared_assets.catalog_provider import (
        AssetCatalogUnavailable,
        PostgresAssetCatalogProvider,
    )
    from app.shared_assets.models import AgentPayload
    from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow

    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    payload = AgentPayload("description", "soul", "default", (), (), ())
    asset = AgentRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="archived-agent",
        display_name="Archived agent",
        status="archived",
        current_published_version_id=version_id,
        created_by_user_id="system",
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        workflow_status="published",
        description=payload.description,
        soul=payload.soul,
        model_ref=payload.model_ref,
        tool_groups=[],
        payload_checksum=AgentService._payload_checksum(payload),
        created_by_user_id="system",
    )

    with pytest.raises(AssetCatalogUnavailable, match="agent catalog is invalid"):
        await PostgresAssetCatalogProvider._load_agents(  # type: ignore[arg-type]
            _CatalogSession([(asset, version)]),
            1,
        )


@pytest.mark.asyncio
async def test_mcp_catalog_rejects_corrupt_payload_checksum(monkeypatch) -> None:
    from app.shared_assets.catalog_provider import (
        AssetCatalogUnavailable,
        PostgresAssetCatalogProvider,
    )
    from app.shared_assets.credential_closure import LockedMcpCredentialClosure
    from deerflow.persistence.shared_assets import McpServerRow, McpServerVersionRow

    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = McpServerRow(
        id=asset_id,
        scope="system",
        project_id=None,
        slug="corrupt-mcp",
        display_name="Corrupt MCP",
        status="active",
        current_published_version_id=version_id,
        created_by_user_id="system",
    )
    version = McpServerVersionRow(
        id=version_id,
        mcp_server_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="description",
        transport="stdio",
        command="mcp-command",
        args=[],
        url=None,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
        routing={},
        tool_overrides={},
        timeout_seconds=30,
        payload_checksum="f" * 64,
        created_by_user_id="system",
    )

    async def _closure(_session, _targets):
        return {version_id: LockedMcpCredentialClosure((), ())}

    monkeypatch.setattr("app.shared_assets.catalog_provider.lock_mcp_credential_closures", _closure)
    session = _CatalogSession([(asset, version)])

    with pytest.raises(AssetCatalogUnavailable, match="MCP catalog is invalid"):
        await PostgresAssetCatalogProvider._load_mcp(session, 1)  # type: ignore[arg-type]
