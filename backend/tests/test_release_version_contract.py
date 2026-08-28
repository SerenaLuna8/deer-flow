"""Public-release version surfaces stay on one v1 baseline."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = "1.0.0"


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_application_packages_and_lock_share_the_v1_release_version() -> None:
    backend = _toml(REPO_ROOT / "backend/pyproject.toml")
    harness = _toml(REPO_ROOT / "backend/packages/harness/pyproject.toml")
    frontend = json.loads((REPO_ROOT / "frontend/package.json").read_text(encoding="utf-8"))

    assert backend["project"]["version"] == RELEASE_VERSION
    assert harness["project"]["version"] == RELEASE_VERSION
    assert frontend["version"] == RELEASE_VERSION

    lock = _toml(REPO_ROOT / "backend/uv.lock")
    project_packages = {package["name"]: package["version"] for package in lock["package"] if package["name"] in {"deer-flow", "deerflow-harness"}}
    assert project_packages == {
        "deer-flow": RELEASE_VERSION,
        "deerflow-harness": RELEASE_VERSION,
    }


def test_runtime_product_metadata_uses_the_v1_release_version() -> None:
    gateway = (REPO_ROOT / "backend/app/gateway/app.py").read_text(encoding="utf-8")
    acp_tool = (REPO_ROOT / "backend/packages/harness/deerflow/tools/builtins/invoke_acp_agent_tool.py").read_text(encoding="utf-8")

    assert re.search(r'FastAPI\([\s\S]*?version="1\.0\.0",', gateway)
    assert re.search(
        r'client_info=Implementation\([\s\S]*?title="ActWeave",\s*version="1\.0\.0",',
        acp_tool,
    )


def test_public_release_copy_describes_fluva_v1_without_v2_residue() -> None:
    hero = (REPO_ROOT / "frontend/src/components/landing/hero.tsx").read_text(encoding="utf-8")
    whats_new = (REPO_ROOT / "frontend/src/components/landing/sections/whats-new-section.tsx").read_text(encoding="utf-8")

    assert "Get Started with Fluva 1.0" in hero
    assert "Meet Fluva 1.0" in whats_new
    assert "Fluva 2.0" not in hero + whats_new


def test_frontend_source_has_no_legacy_product_copy() -> None:
    frontend_root = REPO_ROOT / "frontend"
    source_paths = [frontend_root / "README.md"]
    source_paths.extend(path for path in (frontend_root / "src").rglob("*") if path.suffix in {".md", ".mdx", ".ts", ".tsx"})

    legacy_copy = {path.relative_to(REPO_ROOT).as_posix(): legacy for path in source_paths for legacy in ("ActWeave", '"actweave_bot"') if legacy in path.read_text(encoding="utf-8")}
    assert legacy_copy == {}


def test_project_assistant_description_uses_the_fluva_product_name() -> None:
    payload = json.loads((REPO_ROOT / "backend/app/shared_assets/bootstrap/content/project-assistant-v1.agent.json").read_text(encoding="utf-8"))

    assert payload["description"] == ("Canonical project-scoped Fluva assistant. Weave intelligence into action.")
