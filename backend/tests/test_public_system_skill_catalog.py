from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.catalog import BootstrapCatalogError
from app.shared_assets.bootstrap.skill_archive import load_skill_archive
from app.shared_assets.models import SkillArchiveFile
from scripts import generate_public_system_skill_catalog as generator

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SKILL_CREATOR_SOURCE = _REPOSITORY_ROOT / "skills" / "public" / "skill-creator" / "SKILL.md"
_SKILL_CREATOR_ARCHIVE = _REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap" / "content" / "public-skills" / "skill-creator-v1.skill.json"
_CATALOG = _REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap" / "catalog.json"


def _manifest(*, description: str) -> bytes:
    encoded_description = '""' if not description else description
    return (f"---\nname: generated-skill\ndescription: {encoded_description}\n---\n# Generated Skill\n").encode()


def test_generator_rejects_manifest_runtime_parser_would_reject() -> None:
    files = (
        SkillArchiveFile(
            path="SKILL.md",
            content=_manifest(description=""),
            media_type="text/markdown",
        ),
    )

    with pytest.raises(ValueError, match="frontmatter is invalid"):
        generator._skill_name(files)


def test_bootstrap_metadata_rejects_empty_description() -> None:
    entry = SimpleNamespace(
        slug="generated-skill",
        source_key="builtin:skill:generated-skill",
    )
    files = (
        SkillArchiveFile(
            path="SKILL.md",
            content=_manifest(description=""),
            media_type="text/markdown",
        ),
    )

    with pytest.raises(
        BootstrapCatalogError,
        match="archive is invalid",
    ):
        bootstrap_service._validated_skill_preview(
            entry,
            files,
        )


def test_generator_write_rejects_symlink_destination(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_root = tmp_path / "bootstrap"
    output_root = bootstrap_root / "content" / "public-skills"
    output_root.mkdir(parents=True)
    catalog_path = bootstrap_root / "catalog.json"
    catalog_path.write_bytes(b"old catalog\n")
    outside = tmp_path / "outside.skill.json"
    outside.write_bytes(b"outside remains unchanged\n")
    destination = output_root / "generated-skill-v1.skill.json"
    destination.symlink_to(outside)
    monkeypatch.setattr(generator, "_BOOTSTRAP_ROOT", bootstrap_root)
    monkeypatch.setattr(generator, "_OUTPUT_ROOT", output_root)
    monkeypatch.setattr(generator, "_CATALOG_PATH", catalog_path)

    with pytest.raises(ValueError, match="symbolic link"):
        generator._write(
            b"new catalog\n",
            {"content/public-skills/generated-skill-v1.skill.json": (b"new archive\n")},
        )

    assert outside.read_bytes() == b"outside remains unchanged\n"
    assert catalog_path.read_bytes() == b"old catalog\n"


def test_generator_derives_public_catalog_from_current_source_directories(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "skills" / "public"
    skill_root = source_root / "current-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_bytes(b"---\nname: current-skill\ndescription: Current generated Skill.\n---\n# Current Skill\n")
    bootstrap_root = tmp_path / "bootstrap"
    output_root = bootstrap_root / "content" / "public-skills"
    output_root.mkdir(parents=True)
    catalog_path = bootstrap_root / "catalog.json"
    stale_path = output_root / "stale-skill-v1.skill.json"
    stale_path.write_bytes(b"stale archive\n")
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "source_key": "builtin:skill:deerflow-core",
                        "kind": "skill",
                        "slug": "deerflow-core",
                        "display_name": "DeerFlow Core",
                        "version": 1,
                        "payload_path": "content/deerflow-core-v1.skill.md",
                        "sha256": "0" * 64,
                    },
                    {
                        "source_key": "builtin:skill:stale-skill",
                        "kind": "skill",
                        "slug": "stale-skill",
                        "display_name": "stale-skill",
                        "version": 1,
                        "payload_path": "content/public-skills/stale-skill-v1.skill.json",
                        "payload_format": "skill_archive_v1",
                        "sha256": "1" * 64,
                    },
                    {
                        "source_key": "builtin:mcp:retained-mcp",
                        "kind": "mcp",
                        "slug": "retained-mcp",
                        "display_name": "Retained MCP",
                        "version": 1,
                        "payload_path": "content/retained-mcp-v1.mcp.json",
                        "sha256": "2" * 64,
                    },
                    {
                        "source_key": "builtin:agent:retained-agent",
                        "kind": "agent",
                        "slug": "retained-agent",
                        "display_name": "Retained Agent",
                        "version": 1,
                        "payload_path": "content/retained-agent-v1.agent.json",
                        "sha256": "3" * 64,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(generator, "_SOURCE_ROOT", source_root)
    monkeypatch.setattr(generator, "_BOOTSTRAP_ROOT", bootstrap_root)
    monkeypatch.setattr(generator, "_OUTPUT_ROOT", output_root)
    monkeypatch.setattr(generator, "_CATALOG_PATH", catalog_path)

    catalog_bytes, payloads = generator._expected_outputs()
    generator._write(catalog_bytes, payloads)

    generated_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert [(entry["kind"], entry["slug"]) for entry in generated_catalog["entries"]] == [
        ("skill", "current-skill"),
        ("mcp", "retained-mcp"),
        ("agent", "retained-agent"),
    ]
    assert {path.name for path in output_root.iterdir()} == {"current-skill-v1.skill.json"}
    assert generator._check(catalog_bytes, payloads)


def _assert_skill_creator_builder_contract(instructions: str) -> None:
    assert "skill_manage" not in instructions
    builder_heading = "## DeerFlow Skill Builder"
    assert builder_heading in instructions
    builder_section = instructions.split(builder_heading, 1)[1].split("\n## ", 1)[0]
    assert "candidate package" in builder_section
    for path in ("SKILL.md", "scripts/", "references/", "assets/"):
        assert path in builder_section
    assert "Do not run" in builder_section
    for script in (
        "scripts/init_skill.py",
        "scripts/quick_validate.py",
        "scripts/package_skill.py",
    ):
        assert script in builder_section
    assert "Builder validation" in builder_section
    assert "Builder commit" in builder_section
    assert "explicit user confirmation" in builder_section
    assert "Outside the dedicated DeerFlow Skill Builder" in builder_section


def test_skill_creator_source_and_packaged_archive_use_builder_candidate_protocol() -> None:
    source_instructions = _SKILL_CREATOR_SOURCE.read_text(encoding="utf-8")
    archive_payload = _SKILL_CREATOR_ARCHIVE.read_bytes()
    archive_files = load_skill_archive(archive_payload)
    packaged_instructions = next(file.content.decode("utf-8") for file in archive_files if file.path == "SKILL.md")
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    catalog_entry = next(entry for entry in catalog["entries"] if entry.get("source_key") == "builtin:skill:skill-creator")

    _assert_skill_creator_builder_contract(source_instructions)
    _assert_skill_creator_builder_contract(packaged_instructions)
    assert packaged_instructions == source_instructions
    assert catalog_entry["sha256"] == hashlib.sha256(archive_payload).hexdigest()
