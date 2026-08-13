from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.shared_assets.bootstrap import catalog as catalog_module
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.skill_archive import dump_skill_archive
from app.shared_assets.models import SkillArchiveFile
from scripts import generate_public_system_skill_catalog as generator


def _skill_archive(name: str, description: str) -> bytes:
    return dump_skill_archive(
        (
            SkillArchiveFile(
                path="SKILL.md",
                content=(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n").encode(),
                media_type="text/markdown",
            ),
        )
    )


def _write_source(root: Path, name: str, description: str) -> None:
    skill_root = root / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"),
        encoding="utf-8",
    )


def _configure_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: list[dict[str, object]],
    schema_version: int = 1,
) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "skills" / "public"
    source_root.mkdir(parents=True)
    bootstrap_root = tmp_path / "bootstrap"
    output_root = bootstrap_root / "content" / "public-skills"
    output_root.mkdir(parents=True)
    catalog_path = bootstrap_root / "catalog.json"
    catalog_path.write_text(
        json.dumps({"schema_version": schema_version, "entries": entries}),
        encoding="utf-8",
    )
    monkeypatch.setattr(generator, "_SOURCE_ROOT", source_root)
    monkeypatch.setattr(generator, "_BOOTSTRAP_ROOT", bootstrap_root)
    monkeypatch.setattr(generator, "_OUTPUT_ROOT", output_root)
    monkeypatch.setattr(generator, "_CATALOG_PATH", catalog_path)
    return source_root, output_root, catalog_path


def _entry(name: str, version: int, archive: bytes) -> dict[str, object]:
    return {
        "source_key": f"builtin:skill:{name}",
        "kind": "skill",
        "slug": name,
        "display_name": name,
        "version": version,
        "payload_path": f"content/public-skills/{name}-v{version}.skill.json",
        "payload_format": "skill_archive_v1",
        "sha256": hashlib.sha256(archive).hexdigest(),
    }


def _retained_mcp_entry(
    name: str,
    payload: bytes,
    *,
    payload_path: str | None = None,
) -> dict[str, object]:
    return {
        "source_key": f"builtin:mcp:{name}",
        "kind": "mcp",
        "slug": name,
        "display_name": name,
        "version": 1,
        "payload_path": payload_path or f"content/{name}-v1.mcp.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_bootstrap_payload(tmp_path: Path, entry: dict[str, object], payload: bytes) -> None:
    path = tmp_path / "bootstrap" / str(entry["payload_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _replace_content_root_with_symlink(tmp_path: Path) -> Path:
    content_root = tmp_path / "bootstrap" / "content"
    output_root = content_root / "public-skills"
    output_root.rmdir()
    content_root.rmdir()
    external_content_root = tmp_path / "outside-bootstrap-content"
    (external_content_root / "public-skills").mkdir(parents=True)
    content_root.symlink_to(external_content_root, target_is_directory=True)
    return external_content_root


def test_generator_appends_a_release_and_preserves_authenticated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "evolving-skill"
    version_one = _skill_archive(name, "Original behavior.")
    source_root, output_root, catalog_path = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[_entry(name, 1, version_one)],
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    _write_source(source_root, name, "Improved behavior.")

    catalog_bytes, payloads = generator._expected_outputs()
    generated = json.loads(catalog_bytes)

    assert generated["schema_version"] == 3
    assert [entry["version"] for entry in generated["entries"]] == [1, 2]
    assert payloads[f"content/public-skills/{name}-v1.skill.json"] == version_one
    assert payloads[f"content/public-skills/{name}-v2.skill.json"] != version_one

    generator._write(catalog_bytes, payloads)
    assert generator._check(catalog_bytes, payloads)

    repeated_catalog, repeated_payloads = generator._expected_outputs()
    assert repeated_catalog == catalog_path.read_bytes() == catalog_bytes
    assert repeated_payloads == payloads


def test_generator_refuses_to_implicitly_remove_a_released_system_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "released-skill"
    retained_name = "retained-skill"
    version_one = _skill_archive(name, "Released behavior.")
    retained_version = _skill_archive(retained_name, "Retained behavior.")
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[
            _entry(name, 1, version_one),
            _entry(retained_name, 1, retained_version),
        ],
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    (output_root / f"{retained_name}-v1.skill.json").write_bytes(retained_version)
    _write_source(source_root, retained_name, "Retained behavior.")

    with pytest.raises(
        ValueError,
        match="released system Skills cannot be removed implicitly",
    ):
        generator._expected_outputs()


def test_generator_rejects_authenticated_but_malformed_skill_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "malformed-history"
    malformed = b'{"schema_version":1,"files":[]}'
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[_entry(name, 1, malformed)],
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(malformed)
    _write_source(source_root, name, "Improved behavior.")

    with pytest.raises(ValueError, match="archive is invalid"):
        generator._expected_outputs()


def test_generator_rejects_retained_non_skill_payload_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained_payload = b'{"transport":"stdio"}'
    retained_entry = {
        "source_key": "builtin:mcp:retained-mcp",
        "kind": "mcp",
        "slug": "retained-mcp",
        "display_name": "Retained MCP",
        "version": 1,
        "payload_path": "content/retained-mcp-v1.mcp.json",
        "sha256": hashlib.sha256(retained_payload).hexdigest(),
    }
    source_root, _, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[retained_entry],
    )
    _write_source(source_root, "retained-skill", "Retained behavior.")
    retained_path = tmp_path / "bootstrap" / "content" / "retained-mcp-v1.mcp.json"
    retained_path.parent.mkdir(parents=True, exist_ok=True)
    retained_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="retained system asset payload digest mismatch"):
        generator._expected_outputs()


def test_generator_rejects_intermediate_symlink_when_reading_retained_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"transport":"stdio"}'
    retained_entry = _retained_mcp_entry("retained-mcp", payload)
    source_root, _, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[retained_entry],
    )
    _write_source(source_root, "generated-skill", "Generated behavior.")
    external_content_root = _replace_content_root_with_symlink(tmp_path)
    external_payload = external_content_root / "retained-mcp-v1.mcp.json"
    external_payload.write_bytes(payload)

    with pytest.raises(ValueError, match="symlink"):
        generator._expected_outputs()


def test_generator_check_fails_closed_for_intermediate_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, catalog_path = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[],
    )
    external_content_root = _replace_content_root_with_symlink(tmp_path)
    payload = b"authenticated archive"
    relative_path = "content/public-skills/escaped-skill-v1.skill.json"
    (external_content_root / "public-skills" / "escaped-skill-v1.skill.json").write_bytes(payload)

    assert generator._check(catalog_path.read_bytes(), {relative_path: payload}) is False


def test_generator_write_rejects_intermediate_output_symlink_without_escaping_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, catalog_path = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[],
    )
    external_content_root = _replace_content_root_with_symlink(tmp_path)
    relative_path = "content/public-skills/escaped-skill-v1.skill.json"
    escaped_path = external_content_root / "public-skills" / "escaped-skill-v1.skill.json"

    with pytest.raises(ValueError, match="symlink"):
        generator._write(
            catalog_path.read_bytes(),
            {relative_path: b"must not escape the bootstrap root"},
        )

    assert not escaped_path.exists()


@pytest.mark.parametrize(
    "case",
    ["invalid-source-key", "invalid-slug", "duplicate-release", "duplicate-payload-path"],
)
def test_generator_validates_complete_retained_entry_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    first_payload = b'{"transport":"stdio","name":"first"}'
    second_payload = b'{"transport":"stdio","name":"second"}'
    first = _retained_mcp_entry("first-mcp", first_payload)
    entries = [first]
    payloads = [(first, first_payload)]

    if case == "invalid-source-key":
        first["source_key"] = "builtin:mcp:INVALID"
    elif case == "invalid-slug":
        first["slug"] = "INVALID"
    elif case == "duplicate-release":
        duplicate = {
            **first,
            "payload_path": "content/first-mcp-copy-v1.mcp.json",
            "sha256": hashlib.sha256(second_payload).hexdigest(),
        }
        entries.append(duplicate)
        payloads.append((duplicate, second_payload))
    else:
        duplicate = _retained_mcp_entry(
            "second-mcp",
            first_payload,
            payload_path=str(first["payload_path"]),
        )
        entries.append(duplicate)

    source_root, _, _ = _configure_generator(tmp_path, monkeypatch, entries=entries)
    _write_source(source_root, "generated-skill", "Generated behavior.")
    for entry, payload in payloads:
        _write_bootstrap_payload(tmp_path, entry, payload)

    with pytest.raises(ValueError):
        generator._expected_outputs()


def test_generator_validates_final_catalog_after_merging_new_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_name = "colliding-skill"
    description = "A valid generated Skill."
    archive = _skill_archive(skill_name, description)
    colliding_path = f"content/public-skills/{skill_name}-v1.skill.json"
    retained_entry = _retained_mcp_entry(
        "retained-mcp",
        archive,
        payload_path=colliding_path,
    )
    source_root, _, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[retained_entry],
    )
    _write_bootstrap_payload(tmp_path, retained_entry, archive)
    _write_source(source_root, skill_name, description)

    with pytest.raises(ValueError):
        generator._expected_outputs()


def test_catalog_loader_authenticates_each_contiguous_skill_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "versioned-skill"
    version_one = _skill_archive(name, "Version one.")
    version_two = _skill_archive(name, "Version two.")
    entries = [
        _entry(name, 1, version_one),
        _entry(name, 2, version_two),
    ]
    (tmp_path / "content" / "public-skills").mkdir(parents=True)
    for entry, content in zip(
        entries,
        (version_one, version_two),
        strict=True,
    ):
        (tmp_path / str(entry["payload_path"])).write_bytes(content)
    (tmp_path / "catalog.json").write_text(
        json.dumps({"schema_version": 2, "entries": entries}),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_module.resources, "files", lambda _package: tmp_path)

    catalog = catalog_module.load_bootstrap_catalog()

    assert [entry.version for entry in catalog.entries] == [1, 2]
    assert catalog_module.catalog_payload(catalog, catalog.entries[0]) == version_one
    assert catalog_module.catalog_payload(catalog, catalog.entries[1]) == version_two


@pytest.mark.parametrize(
    "entries",
    [
        [
            _entry("version-gap", 1, b"one"),
            _entry("version-gap", 3, b"three"),
        ],
        [
            _entry("metadata-drift", 1, b"one"),
            {
                **_entry("metadata-drift", 2, b"two"),
                "display_name": "Changed Name",
            },
        ],
    ],
    ids=["version-gap", "metadata-drift"],
)
def test_catalog_rejects_noncanonical_skill_release_history(
    entries: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        catalog_module.BootstrapCatalog.model_validate({"schema_version": 2, "entries": entries})


def test_agent_v1_dependency_names_remain_pinned_to_release_v1() -> None:
    name = "versioned-dependency"
    version_one_archive = _skill_archive(name, "Version one.")
    version_two_archive = _skill_archive(name, "Version two.")
    skill_one = catalog_module.BootstrapEntry.model_validate(_entry(name, 1, version_one_archive))
    skill_two = catalog_module.BootstrapEntry.model_validate(_entry(name, 2, version_two_archive))
    mcp_one = catalog_module.BootstrapEntry.model_validate(
        {
            "source_key": "builtin:mcp:versioned-dependency",
            "kind": "mcp",
            "slug": "versioned-dependency",
            "display_name": "Versioned Dependency",
            "version": 1,
            "payload_path": "content/versioned-dependency-v1.mcp.json",
            "sha256": "3" * 64,
        }
    )
    releases = {(entry.source_key, entry.version): entry for entry in (skill_one, skill_two, mcp_one)}

    assert bootstrap_service._resolved_dependency_ids(
        releases,
        (skill_one.source_key,),
        "skill",
    ) == (bootstrap_service._version_id(skill_one),)
    assert bootstrap_service._resolved_dependency_ids(
        releases,
        (mcp_one.source_key,),
        "mcp",
    ) == (bootstrap_service._version_id(mcp_one),)


@pytest.mark.asyncio
async def test_bootstrap_appends_a_skill_release_and_moves_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "versioned-skill"
    version_one_archive = _skill_archive(name, "Version one.")
    version_two_archive = _skill_archive(name, "Version two.")
    entry_one = catalog_module.BootstrapEntry.model_validate(_entry(name, 1, version_one_archive))
    entry_two = catalog_module.BootstrapEntry.model_validate(_entry(name, 2, version_two_archive))
    catalog = catalog_module.BootstrapCatalog.model_validate(
        {
            "schema_version": 2,
            "entries": [entry_one.model_dump(), entry_two.model_dump()],
        }
    )
    catalog._payloads = MappingProxyType(
        {
            (entry_one.source_key, 1): version_one_archive,
            (entry_two.source_key, 2): version_two_archive,
        }
    )

    asset_id = bootstrap_service._stable_id(entry_one.source_key)
    version_one_id = bootstrap_service._version_id(entry_one)
    asset = SimpleNamespace(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=name,
        display_name=name,
        status="active",
        version=1,
        source_key=entry_one.source_key,
        created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
        current_published_version_id=version_one_id,
    )
    version_one = SimpleNamespace(
        id=version_one_id,
        skill_id=asset_id,
        version_number=1,
        workflow_status="published",
        description="Version one.",
        frontmatter={"name": name, "description": "Version one."},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={},
        supersedes_version_id=None,
        payload_checksum="1" * 64,
        submitted_at=None,
        reviewed_at=None,
        reviewed_by_user_id=None,
        review_note=None,
        created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
    )
    created: list[object] = []

    class Session:
        def add(self, value: object) -> None:
            created.append(value)

        def add_all(self, values: list[object]) -> None:
            created.extend(values)

        async def flush(self) -> None:
            return None

        async def get(self, _model, version_id):
            return version_one if version_id == version_one_id else None

    session = Session()
    previews = {
        1: SimpleNamespace(
            frontmatter={"name": name, "description": "Version one."},
            description="Version one.",
            compatibility=None,
            secret_requirements=(),
            checksum="1" * 64,
            scan_decision="allow",
            scan_summary={},
        ),
        2: SimpleNamespace(
            frontmatter={"name": name, "description": "Version two."},
            description="Version two.",
            compatibility=None,
            secret_requirements=(),
            checksum="2" * 64,
            scan_decision="allow",
            scan_summary={},
        ),
    }
    monkeypatch.setattr(
        bootstrap_service,
        "_existing_asset",
        AsyncMock(return_value=asset),
    )
    monkeypatch.setattr(
        bootstrap_service,
        "load_skill_archive",
        lambda payload: (
            bootstrap_service.SkillArchiveFile(
                path="SKILL.md",
                content=payload,
                media_type="text/markdown",
            ),
        ),
    )
    monkeypatch.setattr(
        bootstrap_service,
        "normalize_skill_files",
        lambda files, *, request_id: files,
    )
    monkeypatch.setattr(
        bootstrap_service,
        "_validated_skill_preview",
        lambda entry, _files: previews[entry.version],
    )
    monkeypatch.setattr(
        bootstrap_service.asyncio,
        "to_thread",
        AsyncMock(side_effect=lambda fn, *args: fn(*args)),
    )
    persisted_one = SimpleNamespace(
        skill_version_id=version_one_id,
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=len(version_one_archive),
        sha256=hashlib.sha256(version_one_archive).hexdigest(),
        content=version_one_archive,
    )
    query_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [deepcopy(persisted_one)]))
    history_result = SimpleNamespace(
        all=lambda: [
            (version_one_id, 1),
            (bootstrap_service._version_id(entry_two), 2),
        ]
    )
    session.execute = AsyncMock(
        side_effect=[query_result, history_result],
    )

    created_one = await bootstrap_service._seed_skill(
        session,
        catalog,
        entry_one,
    )
    created_two = await bootstrap_service._seed_skill(
        session,
        catalog,
        entry_two,
    )

    assert created_one is False
    assert created_two is True
    new_version = next(value for value in created if isinstance(value, bootstrap_service.SkillVersionRow))
    assert new_version.id == bootstrap_service._version_id(entry_two)
    assert new_version.supersedes_version_id == version_one_id
    assert new_version.workflow_status == "published"
    assert asset.current_published_version_id == new_version.id
    assert asset.version == 2


@pytest.mark.asyncio
async def test_bootstrap_orders_each_skill_release_before_other_asset_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_name = "ordered-skill"
    version_one_archive = _skill_archive(skill_name, "Version one.")
    version_two_archive = _skill_archive(skill_name, "Version two.")
    entries = [
        _entry(skill_name, 2, version_two_archive),
        {
            "source_key": "builtin:mcp:ordered-mcp",
            "kind": "mcp",
            "slug": "ordered-mcp",
            "display_name": "Ordered MCP",
            "version": 1,
            "payload_path": "content/ordered-mcp-v1.mcp.json",
            "sha256": "3" * 64,
        },
        {
            "source_key": "builtin:agent:ordered-agent",
            "kind": "agent",
            "slug": "ordered-agent",
            "display_name": "Ordered Agent",
            "version": 1,
            "payload_path": "content/ordered-agent-v1.agent.json",
            "sha256": "4" * 64,
        },
        _entry(skill_name, 1, version_one_archive),
    ]
    catalog = catalog_module.BootstrapCatalog.model_validate({"schema_version": 2, "entries": entries})
    catalog._digest = "d" * 64
    calls: list[tuple[str, int]] = []
    lock_statements: list[str] = []

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def execute(self, statement, parameters=None):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql:
                assert parameters == {
                    "lock_key": bootstrap_service._BOOTSTRAP_LOCK_KEY,
                }
                return None
            for_update = statement._for_update_arg
            if for_update is None or for_update.nowait is not True:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
            assert parameters is None
            assert "FOR UPDATE" in sql
            lock_statements.append(sql)

        def begin(self) -> Transaction:
            return Transaction()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class SessionFactory:
        def __call__(self) -> Session:
            return Session()

    async def seed(_session, _catalog, entry):
        calls.append((entry.kind, entry.version))
        return True

    monkeypatch.setattr(
        bootstrap_service,
        "load_bootstrap_catalog",
        lambda: catalog,
    )
    monkeypatch.setattr(
        bootstrap_service,
        "catalog_digest",
        lambda _catalog: catalog._digest,
    )
    monkeypatch.setattr(
        bootstrap_service,
        "_ensure_builtin_principal",
        AsyncMock(),
    )
    monkeypatch.setattr(bootstrap_service, "_seed_skill", seed)
    monkeypatch.setattr(bootstrap_service, "_seed_mcp", seed)
    monkeypatch.setattr(bootstrap_service, "_seed_agent", seed)

    result = await bootstrap_service.bootstrap_system_assets(SessionFactory())

    assert ["agents" if "FROM agents" in sql else "skills" if "FROM skills" in sql else "mcp_servers" for sql in lock_statements] == ["agents", "skills", "mcp_servers"]
    assert calls == [
        ("skill", 1),
        ("skill", 2),
        ("mcp", 1),
        ("agent", 1),
    ]
    assert result.applied_releases == 4
    assert result.created == result.applied_releases
    assert result.counts == {"agent": 1, "skill": 1, "mcp": 1}


def _scan_preview(
    name: str,
    *,
    decision: str,
    summary: dict[str, object],
) -> SimpleNamespace:
    return SimpleNamespace(
        frontmatter={"name": name},
        scan_decision=decision,
        scan_summary=summary,
    )


def test_generator_new_skill_v1_contains_scan_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "new-snapshot"
    source_root, _, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[],
        schema_version=2,
    )
    _write_source(source_root, name, "Initial behavior.")
    monkeypatch.setattr(
        generator,
        "_analyze_skill_files",
        lambda *_args, **_kwargs: _scan_preview(
            name,
            decision="allow",
            summary={"rule_ids": [], "severity_counts": {}},
        ),
    )

    catalog_bytes, _ = generator._expected_outputs()
    generated = json.loads(catalog_bytes)
    release = next(entry for entry in generated["entries"] if entry["kind"] == "skill")

    assert generated["schema_version"] == 3
    assert release["version"] == 1
    assert release["scan_decision"] == "allow"
    assert release["scan_summary"] == {"rule_ids": [], "severity_counts": {}}


def test_generator_unchanged_legacy_skill_does_not_emit_synthetic_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "legacy-unchanged"
    description = "Unchanged behavior."
    version_one = _skill_archive(name, description)
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[_entry(name, 1, version_one)],
        schema_version=2,
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    _write_source(source_root, name, description)
    monkeypatch.setattr(
        generator,
        "_analyze_skill_files",
        lambda *_args, **_kwargs: _scan_preview(
            name,
            decision="warn",
            summary={
                "rule_ids": ["network-local-http"],
                "severity_counts": {"LOW": 1},
            },
        ),
    )

    catalog_bytes, _ = generator._expected_outputs()
    generated = json.loads(catalog_bytes)
    releases = [entry for entry in generated["entries"] if entry["kind"] == "skill"]

    assert generated["schema_version"] == 3
    assert [entry["version"] for entry in releases] == [1]
    assert "scan_decision" not in releases[0]
    assert "scan_summary" not in releases[0]


def test_generator_scan_drift_emits_release_with_identical_archive_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "scan-drift"
    description = "Stable behavior."
    version_one = _skill_archive(name, description)
    version_one_entry = {
        **_entry(name, 1, version_one),
        "scan_decision": "allow",
        "scan_summary": {"rule_ids": [], "severity_counts": {}},
    }
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[version_one_entry],
        schema_version=3,
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    _write_source(source_root, name, description)
    current_summary = {
        "rule_ids": ["network-local-http"],
        "severity_counts": {"LOW": 1},
    }
    monkeypatch.setattr(
        generator,
        "_analyze_skill_files",
        lambda *_args, **_kwargs: _scan_preview(
            name,
            decision="warn",
            summary=current_summary,
        ),
    )

    catalog_bytes, payloads = generator._expected_outputs()
    generated = json.loads(catalog_bytes)
    releases = [entry for entry in generated["entries"] if entry["kind"] == "skill"]

    assert [entry["version"] for entry in releases] == [1, 2]
    assert releases[1]["scan_decision"] == "warn"
    assert releases[1]["scan_summary"] == current_summary
    assert releases[1]["sha256"] == releases[0]["sha256"]
    assert payloads[str(releases[1]["payload_path"])] == version_one


def test_generator_migrates_unchanged_multi_release_schema_v2_history_without_a_new_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "legacy-prefix-unchanged"
    version_one = _skill_archive(name, "Version one.")
    version_two = _skill_archive(name, "Version two.")
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[
            _entry(name, 1, version_one),
            _entry(name, 2, version_two),
        ],
        schema_version=2,
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    (output_root / f"{name}-v2.skill.json").write_bytes(version_two)
    _write_source(source_root, name, "Version two.")
    monkeypatch.setattr(
        generator,
        "_analyze_skill_files",
        lambda *_args, **_kwargs: _scan_preview(
            name,
            decision="allow",
            summary={"rule_ids": [], "severity_counts": {}},
        ),
    )

    catalog_bytes, _ = generator._expected_outputs()
    generated = json.loads(catalog_bytes)
    releases = [entry for entry in generated["entries"] if entry["kind"] == "skill"]

    assert generated["schema_version"] == 3
    assert [entry["version"] for entry in releases] == [1, 2]
    assert all("scan_decision" not in entry for entry in releases)
    assert all("scan_summary" not in entry for entry in releases)


def test_generator_appends_snapshotted_v3_after_multi_release_schema_v2_history_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "legacy-prefix-changed"
    version_one = _skill_archive(name, "Version one.")
    version_two = _skill_archive(name, "Version two.")
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[
            _entry(name, 1, version_one),
            _entry(name, 2, version_two),
        ],
        schema_version=2,
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    (output_root / f"{name}-v2.skill.json").write_bytes(version_two)
    _write_source(source_root, name, "Version three.")
    current_summary = {
        "rule_ids": ["network-local-http"],
        "severity_counts": {"LOW": 1},
    }
    monkeypatch.setattr(
        generator,
        "_analyze_skill_files",
        lambda *_args, **_kwargs: _scan_preview(
            name,
            decision="warn",
            summary=current_summary,
        ),
    )

    catalog_bytes, _ = generator._expected_outputs()
    generated = json.loads(catalog_bytes)
    releases = [entry for entry in generated["entries"] if entry["kind"] == "skill"]

    assert [entry["version"] for entry in releases] == [1, 2, 3]
    assert all("scan_decision" not in entry for entry in releases[:2])
    assert releases[2]["scan_decision"] == "warn"
    assert releases[2]["scan_summary"] == current_summary
