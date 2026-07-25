from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.catalog import BootstrapCatalogError
from app.shared_assets.models import SkillArchiveFile
from scripts import generate_public_system_skill_catalog as generator


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
