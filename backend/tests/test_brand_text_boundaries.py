from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_public_issue_templates_target_the_current_repository() -> None:
    issue_templates = "\n".join(
        _read(path)
        for path in (
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/ISSUE_TEMPLATE/bug-report.yml",
            ".github/ISSUE_TEMPLATE/feature-request.yml",
        )
    )

    assert "github.com/SerenaLuna8/deer-flow/" in issue_templates
    assert "github.com/bytedance/deer-flow/" not in issue_templates


def test_harness_guides_distinguish_product_brand_from_code_namespace() -> None:
    quick_starts = "\n".join(
        _read(path)
        for path in (
            "frontend/src/content/en/harness/quick-start.mdx",
            "frontend/src/content/zh/harness/quick-start.mdx",
        )
    )
    integration_guides = "\n".join(
        _read(path)
        for path in (
            "frontend/src/content/en/harness/integration-guide.mdx",
            "frontend/src/content/zh/harness/integration-guide.mdx",
        )
    )

    assert "ActWeave repository" in quick_starts
    assert "ActWeave 仓库" in quick_starts
    assert "from deerflow.agents import" in quick_starts
    assert 'app.mount("/actweave", gateway_app)' in integration_guides
    assert "my-actweave-config.yaml" in integration_guides
    assert 'app.mount("/deerflow", gateway_app)' not in integration_guides
    assert "my-deerflow-config.yaml" not in integration_guides
    assert "from deerflow.client import DeerFlowClient" in integration_guides


def test_native_maintenance_examples_use_actweave_brand() -> None:
    sso_guide = _read("backend/docs/SSO.md")
    github_examples = "\n".join(
        _read(path)
        for path in (
            "backend/app/channels/github.py",
            "backend/app/gateway/github/triggers.py",
            "backend/app/gateway/github/dispatcher.py",
        )
    )
    skill_path = REPOSITORY_ROOT / ".agent/skills/actweave-maintainer-orchestrator/SKILL.md"

    assert "/realms/actweave" in sso_guide
    assert "/realms/deerflow" not in sso_guide
    assert "client_id: actweave" in sso_guide
    assert "client_id: deerflow" not in sso_guide
    assert "deerflow" not in sso_guide.lower()
    assert "actweave-bot" in github_examples
    assert "deerflow-bot" not in github_examples
    assert skill_path.is_file()
    assert not (REPOSITORY_ROOT / ".agent/skills/deerflow-maintainer-orchestrator").exists()
    skill = skill_path.read_text(encoding="utf-8")
    assert "name: actweave-maintainer-orchestrator" in skill
    assert "Default repository is `SerenaLuna8/deer-flow`" in skill
    assert "backend/packages/harness/deerflow/" in skill


def test_systematic_literature_review_arxiv_user_agent_is_packaged() -> None:
    source_path = REPOSITORY_ROOT / "skills/public/systematic-literature-review/scripts/arxiv_search.py"
    archive_path = REPOSITORY_ROOT / "backend/app/shared_assets/bootstrap/content/public-skills/systematic-literature-review-v1.skill.json"
    source = source_path.read_text(encoding="utf-8")
    archive_bytes = archive_path.read_bytes()
    archive = json.loads(archive_bytes)
    packaged_script = next(item for item in archive["files"] if item["path"] == "scripts/arxiv_search.py")
    catalog = json.loads(_read("backend/app/shared_assets/bootstrap/catalog.json"))
    catalog_entry = next(entry for entry in catalog["entries"] if entry["source_key"] == "builtin:skill:systematic-literature-review")

    assert '"User-Agent": "actweave-slr-skill/0.1"' in source
    assert "deerflow-slr-skill" not in source
    assert base64.b64decode(packaged_script["content_base64"]).decode("utf-8") == source
    assert catalog_entry["sha256"] == hashlib.sha256(archive_bytes).hexdigest()
