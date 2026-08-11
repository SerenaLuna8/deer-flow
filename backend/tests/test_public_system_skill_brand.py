from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = REPOSITORY_ROOT / "skills" / "public" / "frontend-design" / "SKILL.md"
CATALOG_PATH = REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap" / "catalog.json"
ARCHIVE_PATH = REPOSITORY_ROOT / "backend" / "app" / "shared_assets" / "bootstrap" / "content" / "public-skills" / "frontend-design-v1.skill.json"


def test_frontend_design_skill_uses_current_actweave_brand_in_source_and_archive() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    assert "Created By ActWeave" in source
    assert "https://github.com/SerenaLuna8/deer-flow" in source
    assert "Created By Deerflow" not in source
    assert "deerflow.tech" not in source

    archive_bytes = ARCHIVE_PATH.read_bytes()
    archive = json.loads(archive_bytes)
    packaged_skill = next(item for item in archive["files"] if item["path"] == "SKILL.md")
    packaged_source = base64.b64decode(packaged_skill["content_base64"]).decode("utf-8")
    assert packaged_source == source

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_entry = next(entry for entry in catalog["entries"] if entry["source_key"] == "builtin:skill:frontend-design")
    assert catalog_entry["sha256"] == hashlib.sha256(archive_bytes).hexdigest()


def test_editor_workspace_uses_actweave_brand_without_renaming_code_packages() -> None:
    workspace_path = REPOSITORY_ROOT / "act-weave.code-workspace"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))

    assert workspace["settings"]["python-envs.pythonProjects"][0]["workspace"] == "act-weave"
    assert not (REPOSITORY_ROOT / "deer-flow.code-workspace").exists()
    assert (REPOSITORY_ROOT / "backend" / "packages" / "harness" / "deerflow").is_dir()
