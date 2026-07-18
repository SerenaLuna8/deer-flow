from __future__ import annotations

import ast
import uuid
from dataclasses import fields
from pathlib import Path

import pytest

from app.private_work.runtime_context import prepare_private_run_config
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetForbidden
from app.shared_assets.models import AssetKind, AssetScope, AssetSelection, ResolvedMcpSnapshot
from app.shared_assets.resolver import ProjectAssetResolver, materialize_mcp_secrets
from deerflow.runtime import PrivateResourceScope

HARNESS_ROOT = Path(__file__).parents[1] / "packages" / "harness" / "deerflow"


def test_private_resource_scope_contains_no_authority_fields() -> None:
    assert [field.name for field in fields(PrivateResourceScope)] == [
        "project_id",
        "owner_user_id",
        "membership_version",
    ]
    assert PrivateResourceScope("project", "owner", 3) == PrivateResourceScope(
        project_id="project",
        owner_user_id="owner",
        membership_version=3,
    )


def test_harness_import_graph_never_depends_on_app_modules() -> None:
    violations: list[str] = []
    for source_path in HARNESS_ROOT.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            for module in imported:
                if module == "app" or module.startswith("app."):
                    violations.append(f"{source_path.relative_to(HARNESS_ROOT)}:{node.lineno}:{module}")

    assert violations == []


def _must_not_open_session():
    raise AssertionError("untrusted client context must fail before database access")


@pytest.mark.asyncio
async def test_m3_resolver_and_materializer_reject_client_shaped_authority_dicts() -> None:
    client_context = {
        "user_id": "attacker",
        "project_id": "attacker",
        "role": "admin",
        "capabilities": ["shared_assets.execute"],
        "membership_version": 999,
        "request_id": "req-client-dict",
    }
    resolver = ProjectAssetResolver(_must_not_open_session)
    selection = AssetSelection(kind=AssetKind.MCP, asset_id=uuid.uuid4())
    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(),
    )

    with pytest.raises(AssetForbidden):
        await resolver.resolve_project_asset_snapshot(client_context, selection)  # type: ignore[arg-type]
    with pytest.raises(AssetForbidden):
        await resolver.materialize_mcp_secrets(client_context, snapshot)  # type: ignore[arg-type]
    with pytest.raises(AssetForbidden):
        await materialize_mcp_secrets(  # type: ignore[arg-type]
            client_context,
            snapshot,
            session_factory=_must_not_open_session,
        )


@pytest.mark.asyncio
async def test_only_server_injected_exact_project_context_reaches_m3_after_gateway_sanitizing() -> None:
    client_context = {
        "project_context": {
            "user_id": "attacker",
            "project_id": "attacker",
            "role": "admin",
            "capabilities": ["shared_assets.execute"],
        }
    }
    private_scope = object()
    config = prepare_private_run_config(
        thread_id="thread-safe",
        opaque_scope=private_scope,
        request_config={"context": client_context},
        metadata=None,
        body_context=None,
    )
    assert "project_context" not in config["context"]
    assert config["context"]["private_scope"] is private_scope

    trusted = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-server-context",
    )
    config["context"]["project_context"] = trusted
    selection = AssetSelection(kind=AssetKind.MCP, asset_id=uuid.uuid4())

    with pytest.raises(AssertionError, match="database access"):
        await ProjectAssetResolver(_must_not_open_session).resolve_project_asset_snapshot(
            config["context"]["project_context"],
            selection,
        )
