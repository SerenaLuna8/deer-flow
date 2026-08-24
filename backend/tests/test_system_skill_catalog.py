"""Offline regression coverage for the packaged System Skill catalog."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import app.shared_assets.bootstrap.catalog as catalog_module
from app.shared_assets.bootstrap.catalog import (
    BootstrapCatalogError,
    catalog_payload,
    load_bootstrap_catalog,
)
from app.shared_assets.bootstrap.skill_archive import load_skill_archive

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_SKILLS_ROOT = _REPOSITORY_ROOT / "skills" / "public"


def _public_skill_directories() -> dict[str, Path]:
    return {path.name: path for path in sorted(_PUBLIC_SKILLS_ROOT.iterdir()) if path.is_dir()}


def _source_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), f"public Skill source contains a symlink: {path}"
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


@pytest.fixture()
def copied_catalog_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source_root = Path(catalog_module.__file__).resolve().parent
    copied_root = tmp_path / "bootstrap"
    shutil.copytree(
        source_root,
        copied_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    monkeypatch.setattr(catalog_module, "_package_root", lambda: copied_root)
    return copied_root


def _read_manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "catalog.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    (root / "catalog.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )


def test_catalog_skill_entries_match_public_skill_directories() -> None:
    catalog = load_bootstrap_catalog()
    skill_entries = [entry for entry in catalog.entries if entry.kind == "skill"]

    assert {entry.slug for entry in skill_entries} == set(_public_skill_directories())
    assert len({entry.source_key for entry in skill_entries}) == len(_public_skill_directories())


def test_packaged_catalog_excludes_actweave_docs_mcp() -> None:
    catalog = load_bootstrap_catalog()
    mcp_entries = [entry for entry in catalog.entries if entry.kind == "mcp"]

    assert mcp_entries == []
    assert all(entry.source_key != "builtin:mcp:deerflow-docs" for entry in catalog.entries)
    assert all(entry.slug != "deerflow-docs" for entry in catalog.entries)
    assert all(entry.display_name != "ActWeave Docs" for entry in catalog.entries)


def test_each_latest_skill_archive_matches_its_public_source_bytes() -> None:
    catalog = load_bootstrap_catalog()
    source_directories = _public_skill_directories()
    latest_by_source = {
        source_key: max(
            (entry for entry in catalog.entries if entry.kind == "skill" and entry.source_key == source_key),
            key=lambda entry: entry.version,
        )
        for source_key in {entry.source_key for entry in catalog.entries if entry.kind == "skill"}
    }

    # Archive-format and asset-version evolution are separate concerns. This
    # verifies only each current source snapshot against the latest release;
    # historical release bytes intentionally remain older but are still
    # authenticated and decoded by the canonical loader/bootstrap tests.
    for entry in latest_by_source.values():
        archived_files = load_skill_archive(catalog_payload(catalog, entry))
        actual = {item.path: item.content for item in archived_files}

        assert actual == _source_files(source_directories[entry.slug])


def test_catalog_rejects_duplicate_source_key(
    copied_catalog_root: Path,
) -> None:
    manifest = _read_manifest(copied_catalog_root)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    duplicate = dict(entries[0])
    duplicate["slug"] = "duplicate-source-key-fixture"
    entries.append(duplicate)
    _write_manifest(copied_catalog_root, manifest)

    with pytest.raises(BootstrapCatalogError):
        load_bootstrap_catalog()


def test_catalog_rejects_payload_digest_drift(
    copied_catalog_root: Path,
) -> None:
    manifest = _read_manifest(copied_catalog_root)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    entry = next(item for item in entries if item["kind"] == "skill")
    payload_path = copied_catalog_root / entry["payload_path"]
    payload_path.write_bytes(payload_path.read_bytes() + b"tampered")

    with pytest.raises(BootstrapCatalogError, match="digest mismatch"):
        load_bootstrap_catalog()


def test_catalog_rejects_symlink_payload(
    copied_catalog_root: Path,
) -> None:
    manifest = _read_manifest(copied_catalog_root)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    entry = next(item for item in entries if item["kind"] == "skill")
    payload_path = copied_catalog_root / entry["payload_path"]
    regular_path = payload_path.with_name(f".{payload_path.name}.regular")
    payload_path.rename(regular_path)
    payload_path.symlink_to(regular_path.name)

    with pytest.raises(BootstrapCatalogError, match="symlink"):
        load_bootstrap_catalog()


@pytest.mark.parametrize(
    "schema_version",
    [True, "2", 2.0],
    ids=["bool", "string", "float"],
)
def test_catalog_rejects_coerced_schema_version(
    copied_catalog_root: Path,
    schema_version: object,
) -> None:
    manifest = _read_manifest(copied_catalog_root)
    manifest["schema_version"] = schema_version
    _write_manifest(copied_catalog_root, manifest)

    with pytest.raises(BootstrapCatalogError):
        load_bootstrap_catalog()


@pytest.mark.parametrize(
    "version",
    [True, "1", 1.0],
    ids=["bool", "string", "float"],
)
def test_catalog_rejects_coerced_entry_version(
    copied_catalog_root: Path,
    version: object,
) -> None:
    manifest = _read_manifest(copied_catalog_root)
    entries = manifest["entries"]
    assert isinstance(entries, list)
    entries[0]["version"] = version
    _write_manifest(copied_catalog_root, manifest)

    with pytest.raises(BootstrapCatalogError):
        load_bootstrap_catalog()


@pytest.mark.parametrize(
    "schema_version",
    [True, "1", 1.0],
    ids=["bool", "string", "float"],
)
def test_skill_archive_rejects_coerced_schema_version(
    schema_version: object,
) -> None:
    payload = json.dumps(
        {
            "schema_version": schema_version,
            "files": [
                {
                    "path": "SKILL.md",
                    "media_type": "text/markdown",
                    "content_base64": "",
                }
            ],
        }
    ).encode()

    with pytest.raises(BootstrapCatalogError):
        load_skill_archive(payload)


def _system_skill_entry(version: int) -> dict[str, object]:
    return {
        "source_key": "builtin:skill:snapshot-contract",
        "kind": "skill",
        "slug": "snapshot-contract",
        "display_name": "snapshot-contract",
        "version": version,
        "payload_path": f"content/snapshot-contract-v{version}.skill.json",
        "payload_format": "skill_archive_v1",
        "sha256": f"{version}" * 64,
    }


def test_schema_v3_accepts_one_system_skill_v1_without_scan_metadata() -> None:
    catalog = catalog_module.BootstrapCatalog.model_validate(
        {
            "schema_version": 3,
            "entries": [_system_skill_entry(1)],
        }
    )

    assert catalog.entries[0].version == 1


def test_schema_v3_rejects_system_skill_version_history() -> None:
    with pytest.raises(ValueError, match="require one v1"):
        catalog_module.BootstrapCatalog.model_validate(
            {
                "schema_version": 3,
                "entries": [
                    _system_skill_entry(1),
                    _system_skill_entry(2),
                    _system_skill_entry(3),
                ],
            }
        )


@pytest.mark.parametrize("field", ["scan_decision", "scan_summary"])
def test_schema_v3_rejects_removed_scan_metadata(field: str) -> None:
    entry = _system_skill_entry(1)
    entry[field] = "allow" if field == "scan_decision" else {}

    with pytest.raises(ValueError):
        catalog_module.BootstrapCatalog.model_validate({"schema_version": 3, "entries": [entry]})
