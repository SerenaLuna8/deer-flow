from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
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
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _configure_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: list[dict[str, object]],
    schema_version: int = 3,
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


def _skill_entry(name: str, archive: bytes, *, version: int = 1) -> dict[str, object]:
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


def _agent_payload(description: str) -> bytes:
    return json.dumps(
        {
            "description": description,
            "soul": "Build the requested governed asset.",
            "model_ref": "default",
            "tool_groups": [],
            "skill_source_keys": [],
            "mcp_source_keys": [],
        },
        separators=(",", ":"),
    ).encode()


def _agent_entry(name: str, payload: bytes, *, version: int = 1) -> dict[str, object]:
    return {
        "source_key": f"builtin:agent:{name}",
        "kind": "agent",
        "slug": name,
        "display_name": name,
        "version": version,
        "payload_path": f"content/{name}-v{version}.agent.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _mcp_entry(name: str, payload: bytes, *, payload_path: str | None = None) -> dict[str, object]:
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
    (content_root / "public-skills").rmdir()
    content_root.rmdir()
    external = tmp_path / "outside-bootstrap-content"
    (external / "public-skills").mkdir(parents=True)
    content_root.symlink_to(external, target_is_directory=True)
    return external


def test_generator_replaces_authenticated_system_skill_v1_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "evolving-skill"
    old_archive = _skill_archive(name, "Original behavior.")
    source_root, output_root, catalog_path = _configure_generator(tmp_path, monkeypatch, entries=[_skill_entry(name, old_archive)])
    (output_root / f"{name}-v1.skill.json").write_bytes(old_archive)
    _write_source(source_root, name, "Improved behavior.")

    catalog_bytes, payloads = generator._expected_outputs()
    generated = json.loads(catalog_bytes)

    assert generated["schema_version"] == 3
    assert [entry["version"] for entry in generated["entries"]] == [1]
    assert payloads[f"content/public-skills/{name}-v1.skill.json"] != old_archive
    assert not any(path.endswith("-v2.skill.json") for path in payloads)

    generator._write(catalog_bytes, payloads)
    assert generator._check(catalog_bytes, payloads)
    repeated_catalog, repeated_payloads = generator._expected_outputs()
    assert repeated_catalog == catalog_path.read_bytes() == catalog_bytes
    assert repeated_payloads == payloads


def test_generator_refuses_to_remove_existing_system_skill_implicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _skill_archive("removed-skill", "Removed behavior.")
    retained = _skill_archive("retained-skill", "Retained behavior.")
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[_skill_entry("removed-skill", removed), _skill_entry("retained-skill", retained)],
    )
    (output_root / "removed-skill-v1.skill.json").write_bytes(removed)
    (output_root / "retained-skill-v1.skill.json").write_bytes(retained)
    _write_source(source_root, "retained-skill", "Retained behavior.")

    with pytest.raises(ValueError, match="cannot be removed implicitly"):
        generator._expected_outputs()


def test_generator_rejects_authenticated_but_malformed_system_skill_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = b'{"schema_version":1,"files":[]}'
    source_root, output_root, _ = _configure_generator(tmp_path, monkeypatch, entries=[_skill_entry("malformed-skill", malformed)])
    (output_root / "malformed-skill-v1.skill.json").write_bytes(malformed)
    _write_source(source_root, "malformed-skill", "Replacement behavior.")

    with pytest.raises(ValueError, match="archive is invalid"):
        generator._expected_outputs()


def test_generator_rejects_retained_non_skill_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"transport":"stdio"}'
    entry = _mcp_entry("retained-mcp", payload)
    source_root, _, _ = _configure_generator(tmp_path, monkeypatch, entries=[entry])
    _write_source(source_root, "generated-skill", "Generated behavior.")
    _write_bootstrap_payload(tmp_path, entry, b"tampered")

    with pytest.raises(ValueError, match="payload digest mismatch"):
        generator._expected_outputs()


def test_generator_rejects_intermediate_symlink_when_reading_retained_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"transport":"stdio"}'
    entry = _mcp_entry("retained-mcp", payload)
    source_root, _, _ = _configure_generator(tmp_path, monkeypatch, entries=[entry])
    _write_source(source_root, "generated-skill", "Generated behavior.")
    external = _replace_content_root_with_symlink(tmp_path)
    (external / "retained-mcp-v1.mcp.json").write_bytes(payload)

    with pytest.raises(ValueError, match="symlink"):
        generator._expected_outputs()


def test_generator_check_fails_closed_for_intermediate_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, catalog_path = _configure_generator(tmp_path, monkeypatch, entries=[])
    external = _replace_content_root_with_symlink(tmp_path)
    payload = b"authenticated archive"
    relative_path = "content/public-skills/escaped-skill-v1.skill.json"
    (external / "public-skills" / "escaped-skill-v1.skill.json").write_bytes(payload)

    assert generator._check(catalog_path.read_bytes(), {relative_path: payload}) is False


def test_generator_write_rejects_intermediate_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, catalog_path = _configure_generator(tmp_path, monkeypatch, entries=[])
    external = _replace_content_root_with_symlink(tmp_path)
    relative_path = "content/public-skills/escaped-skill-v1.skill.json"

    with pytest.raises(ValueError, match="symlink"):
        generator._write(catalog_path.read_bytes(), {relative_path: b"payload"})
    assert not (external / "public-skills" / "escaped-skill-v1.skill.json").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [_skill_entry("non-v1-skill", b"payload", version=2)],
        [_skill_entry("multi-skill", b"one"), _skill_entry("multi-skill", b"two", version=2)],
    ],
    ids=["non-v1", "multiple-versions"],
)
def test_catalog_rejects_any_system_skill_shape_other_than_one_v1(entries: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError, match="one v1"):
        catalog_module.BootstrapCatalog.model_validate({"schema_version": 3, "entries": entries})


@pytest.mark.parametrize(
    "entries",
    [
        [_agent_entry("non-v1-agent", b"payload", version=2)],
        [_agent_entry("multi-agent", b"one"), _agent_entry("multi-agent", b"two", version=2)],
    ],
    ids=["non-v1", "multiple-versions"],
)
def test_catalog_rejects_any_system_agent_shape_other_than_one_v1(entries: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError, match="one v1"):
        catalog_module.BootstrapCatalog.model_validate({"schema_version": 3, "entries": entries})


def test_catalog_loader_authenticates_single_system_agent_and_skill_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_payload = _skill_archive("one-skill", "One Skill.")
    agent_payload = _agent_payload("One Agent.")
    entries = [_skill_entry("one-skill", skill_payload), _agent_entry("one-agent", agent_payload)]
    for entry, payload in zip(entries, (skill_payload, agent_payload), strict=True):
        path = tmp_path / str(entry["payload_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (tmp_path / "catalog.json").write_text(json.dumps({"schema_version": 3, "entries": entries}), encoding="utf-8")
    monkeypatch.setattr(catalog_module.resources, "files", lambda _package: tmp_path)

    catalog = catalog_module.load_bootstrap_catalog()

    assert [(entry.kind, entry.version) for entry in catalog.entries] == [("skill", 1), ("agent", 1)]
    assert catalog_module.catalog_payload(catalog, catalog.entries[0]) == skill_payload
    assert catalog_module.catalog_payload(catalog, catalog.entries[1]) == agent_payload


def test_agent_dependencies_bind_skill_asset_and_mcp_v1() -> None:
    skill_payload = _skill_archive("dependency", "Dependency.")
    skill = catalog_module.BootstrapEntry.model_validate(_skill_entry("dependency", skill_payload))
    mcp = catalog_module.BootstrapEntry.model_validate(_mcp_entry("dependency", b'{"transport":"stdio"}'))
    entries = {(entry.source_key, entry.version): entry for entry in (skill, mcp)}

    assert bootstrap_service._resolved_skill_refs(entries, (skill.source_key,)) == (
        bootstrap_service.SkillAssetRef(
            scope=bootstrap_service.AssetScope.SYSTEM,
            asset_id=bootstrap_service._stable_id(skill.source_key),
        ),
    )
    assert bootstrap_service._resolved_dependency_ids(entries, (mcp.source_key,), "mcp") == (bootstrap_service._version_id(mcp),)


@pytest.mark.asyncio
async def test_bootstrap_orders_system_skill_before_mcp_and_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = catalog_module.BootstrapCatalog.model_validate(
        {
            "schema_version": 3,
            "entries": [
                _agent_entry("ordered-agent", _agent_payload("Ordered Agent.")),
                _mcp_entry("ordered-mcp", b'{"transport":"stdio"}'),
                _skill_entry("ordered-skill", _skill_archive("ordered-skill", "Ordered Skill.")),
            ],
        }
    )
    catalog._digest = "d" * 64
    calls: list[tuple[str, int]] = []

    class Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def execute(self, statement, parameters=None):
            sql = str(statement)
            if "pg_advisory_xact_lock" in sql:
                assert parameters == {"lock_key": bootstrap_service._BOOTSTRAP_LOCK_KEY}
                return None
            if "set_config" in sql:
                return None
            if getattr(statement, "_for_update_arg", None) is not None:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
            return None

        def begin(self) -> Transaction:
            return Transaction()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class SessionFactory:
        def __call__(self) -> Session:
            return Session()

    async def seed(_session, _catalog, entry, **_kwargs):
        calls.append((entry.kind, entry.version))
        return True

    monkeypatch.setattr(bootstrap_service, "load_bootstrap_catalog", lambda: catalog)
    monkeypatch.setattr(bootstrap_service, "catalog_digest", lambda _catalog: catalog._digest)
    monkeypatch.setattr(bootstrap_service, "_ensure_builtin_principal", AsyncMock())
    monkeypatch.setattr(bootstrap_service, "_seed_skill", seed)
    monkeypatch.setattr(bootstrap_service, "_seed_mcp", seed)
    monkeypatch.setattr(bootstrap_service, "_seed_agent", seed)

    result = await bootstrap_service.bootstrap_system_assets(SessionFactory())

    assert calls == [("skill", 1), ("mcp", 1), ("agent", 1)]
    assert result.applied_changes == 3
    assert result.counts == {"agent": 1, "skill": 1, "mcp": 1}


def test_generator_new_skill_v1_omits_scan_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "new-system-skill"
    source_root, _, _ = _configure_generator(tmp_path, monkeypatch, entries=[])
    _write_source(source_root, name, "Initial behavior.")

    catalog_bytes, _ = generator._expected_outputs()
    entry = json.loads(catalog_bytes)["entries"][0]

    assert entry["version"] == 1
    assert "scan_decision" not in entry
    assert "scan_summary" not in entry


def test_generator_rejects_legacy_multi_version_system_skill_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "legacy-history"
    version_one = _skill_archive(name, "Version one.")
    version_two = _skill_archive(name, "Version two.")
    source_root, output_root, _ = _configure_generator(
        tmp_path,
        monkeypatch,
        entries=[_skill_entry(name, version_one), _skill_entry(name, version_two, version=2)],
    )
    (output_root / f"{name}-v1.skill.json").write_bytes(version_one)
    (output_root / f"{name}-v2.skill.json").write_bytes(version_two)
    _write_source(source_root, name, "Version two.")

    with pytest.raises(ValueError, match="one v1"):
        generator._expected_outputs()
