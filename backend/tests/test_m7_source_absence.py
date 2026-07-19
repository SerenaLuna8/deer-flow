"""Lock the M7 production tree to the project-only runtime surface."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"

BANNED_PRODUCTION_PATHS = (
    "extensions_config.example.json",
    "frontend/src/app/api/memory",
    "frontend/src/app/workspace/agents",
    "frontend/src/app/workspace/chats",
    "frontend/src/app/workspace/memory",
    "frontend/src/app/workspace/projects",
    "frontend/src/app/workspace/scheduled-tasks",
    "frontend/src/app/workspace/skills",
    "frontend/src/app/workspace/tools",
    "frontend/src/components/workspace/channels",
    "frontend/src/components/workspace/scheduled-task-schedule-input.tsx",
    "frontend/src/components/workspace/settings/channels-settings-page.tsx",
    "frontend/src/components/workspace/thread-scheduled-tasks-link.tsx",
    "frontend/src/core/agents",
    "frontend/src/core/channels",
    "frontend/src/core/mcp",
    "frontend/src/core/memory",
    "frontend/src/core/scheduled-tasks",
    "frontend/src/core/skills/api.ts",
    "frontend/src/core/skills/hooks.ts",
    "frontend/src/core/skills/type.ts",
    "backend/app/automations/cutover.py",
    "backend/app/automations/legacy_reads.py",
    "backend/app/channels/runtime_config_store.py",
    "backend/app/gateway/routers/agents.py",
    "backend/app/gateway/routers/artifacts.py",
    "backend/app/gateway/routers/asset_catalog_compat.py",
    "backend/app/gateway/routers/assistants_compat.py",
    "backend/app/gateway/routers/channel_connections.py",
    "backend/app/gateway/routers/channels.py",
    "backend/app/gateway/routers/console.py",
    "backend/app/gateway/routers/features.py",
    "backend/app/gateway/routers/feedback.py",
    "backend/app/gateway/routers/input_polish.py",
    "backend/app/gateway/routers/mcp.py",
    "backend/app/gateway/routers/memory.py",
    "backend/app/gateway/routers/runs.py",
    "backend/app/gateway/routers/scheduled_tasks.py",
    "backend/app/gateway/routers/skills.py",
    "backend/app/gateway/routers/suggestions.py",
    "backend/app/gateway/routers/thread_runs.py",
    "backend/app/gateway/routers/threads.py",
    "backend/app/gateway/routers/uploads.py",
    "backend/app/private_work/cutover.py",
    "backend/app/recovery/pre_cutover_backup.py",
    "backend/app/reliability/cutover.py",
    "backend/packages/harness/deerflow/config/agents_api_config.py",
    "backend/packages/harness/deerflow/config/extensions_config.py",
    "backend/packages/harness/deerflow/config/run_events_config.py",
    "backend/packages/harness/deerflow/config/stream_bridge_config.py",
    "backend/packages/harness/deerflow/persistence/automations/migration_digest.py",
    "backend/packages/harness/deerflow/persistence/channel_connections/legacy_sql.py",
    "backend/packages/harness/deerflow/persistence/migration_ledger",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0001_baseline.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0002_runs_token_usage.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0003_scheduled_tasks.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0004_migration_ledger.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0005_project_foundation.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0006_project_governance.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0007_project_shared_assets.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0008_project_private_work_expand.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0009_project_private_work_finalize.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0010_private_file_source.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0011_private_artifact_tombstone.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0012_project_automation_expand.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0013_project_automation_finalize.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0014_project_reliability_expand.py",
    "backend/packages/harness/deerflow/persistence/migrations/versions/0015_project_reliability_finalize.py",
    "backend/packages/harness/deerflow/runtime/events/store/memory.py",
    "backend/packages/harness/deerflow/runtime/runs/store/memory.py",
    "backend/packages/harness/deerflow/runtime/stream_bridge",
    "backend/packages/harness/deerflow/skills/installer.py",
    "backend/packages/harness/deerflow/skills/storage/user_scoped_skill_storage.py",
    "backend/packages/harness/deerflow/tools/builtins/setup_agent_tool.py",
    "backend/packages/harness/deerflow/tools/builtins/update_agent_tool.py",
    "backend/packages/harness/deerflow/tools/skill_manage_tool.py",
    "backend/scripts/migrate_assets.py",
    "backend/scripts/migrate_automations.py",
    "backend/scripts/migrate_private_work.py",
    "backend/scripts/migrate_reliability.py",
    "backend/scripts/migrate_sqlite_to_postgres.py",
    "backend/scripts/migrate_user_isolation.py",
    "backend/scripts/sqlite_inventory.py",
)

BANNED_ROUTE_LITERALS = frozenset(
    {
        "/api/agents",
        "/api/artifacts",
        "/api/assistants",
        "/api/channel-connections",
        "/api/channels",
        "/api/console",
        "/api/features",
        "/api/feedback",
        "/api/input-polish",
        "/api/langgraph",
        "/api/mcp/config",
        "/api/memory",
        "/api/runs",
        "/api/scheduled-tasks",
        "/api/skills",
        "/api/suggestions",
        "/api/threads",
        "/api/uploads",
    }
)

BANNED_IMPORT_PREFIXES = (
    "app.automations.legacy_reads",
    "app.gateway.routers.agents",
    "app.gateway.routers.runs",
    "app.gateway.routers.scheduled_tasks",
    "app.gateway.routers.threads",
    "deerflow.config.extensions_config",
    "deerflow.persistence.migration_ledger",
    "deerflow.runtime.stream_bridge",
    "deerflow.tools.builtins.setup_agent_tool",
    "deerflow.tools.builtins.update_agent_tool",
)
BANNED_RUNTIME_SYMBOLS = frozenset({"setup_agent", "update_agent"})

CONFIG_TOMBSTONES = frozenset(
    {
        "agents_api",
        "extensions",
        "extensions_config",
        "legacy_event_store",
        "legacy_run_store",
        "mcp_config",
        "mcp_config_path",
        "run_events",
        "stream_bridge",
    }
)
CONFIG_TOMBSTONE_ALLOWLIST = {
    "backend/packages/harness/deerflow/config/app_config.py",
}


def _production_files() -> tuple[Path, ...]:
    roots = (
        BACKEND_ROOT / "app",
        BACKEND_ROOT / "packages" / "harness" / "deerflow",
        BACKEND_ROOT / "scripts",
        REPO_ROOT / "frontend" / "src" / "app",
        REPO_ROOT / "frontend" / "src" / "components",
        REPO_ROOT / "frontend" / "src" / "core",
        REPO_ROOT / "docker" / "nginx",
    )
    suffixes = {".py", ".ts", ".tsx", ".js", ".mjs", ".conf"}
    return tuple(sorted(path for root in roots for path in root.rglob("*") if path.is_file() and path.suffix in suffixes))


def _python_imports(path: Path) -> set[str]:
    tree = _python_tree(path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _python_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _python_string_literals(path: Path) -> set[str]:
    return {node.value for node in ast.walk(_python_tree(path)) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _python_symbols(path: Path) -> set[str]:
    symbols: set[str] = set()
    for node in ast.walk(_python_tree(path)):
        if isinstance(node, ast.Name):
            symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            symbols.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.alias):
            symbols.add(node.asname or node.name.rsplit(".", 1)[-1])
    return symbols


def _contains_route(value: str, literal: str) -> bool:
    return re.search(rf"{re.escape(literal)}(?:[/{{?*]|$)", value) is not None


def _banned_path_has_production_source(path: Path) -> bool:
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    production_suffixes = {
        ".conf",
        ".js",
        ".json",
        ".mjs",
        ".py",
        ".pyi",
        ".so",
        ".ts",
        ".tsx",
    }
    return any(child.is_file() and child.suffix in production_suffixes for child in path.rglob("*"))


def test_deleted_production_paths_do_not_exist() -> None:
    assert len(BANNED_PRODUCTION_PATHS) >= 70
    for relative in BANNED_PRODUCTION_PATHS:
        assert not _banned_path_has_production_source(REPO_ROOT / relative), relative


def test_legacy_route_inventory_covers_every_removed_global_surface() -> None:
    assert {
        "/api/assistants",
        "/api/console",
        "/api/input-polish",
        "/api/suggestions",
    } <= BANNED_ROUTE_LITERALS


def test_production_sources_have_no_legacy_route_literals() -> None:
    findings: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            strings = _python_string_literals(path)
            symbols = _python_symbols(path)
            for literal in BANNED_ROUTE_LITERALS:
                if any(_contains_route(value, literal) for value in strings):
                    findings.append(f"{path.relative_to(REPO_ROOT)}:{literal}")
            for symbol in BANNED_RUNTIME_SYMBOLS & symbols:
                findings.append(f"{path.relative_to(REPO_ROOT)}:{symbol}")
        elif path.suffix == ".conf":
            for literal in BANNED_ROUTE_LITERALS:
                nginx_route = re.compile(rf"\b(?:location|rewrite)\b[^\n]*{re.escape(literal)}(?:[/{{?*\s]|$)")
                if nginx_route.search(text):
                    findings.append(f"{path.relative_to(REPO_ROOT)}:{literal}")
        else:
            for literal in BANNED_ROUTE_LITERALS:
                route_literal = re.compile(rf"""["'`]([^"'`]*{re.escape(literal)}(?:[/{{?*]|$))""")
                if route_literal.search(text):
                    findings.append(f"{path.relative_to(REPO_ROOT)}:{literal}")
    assert findings == []


def test_python_production_sources_have_no_legacy_imports() -> None:
    findings: list[str] = []
    for path in _production_files():
        if path.suffix != ".py":
            continue
        for imported in _python_imports(path):
            if imported.startswith(BANNED_IMPORT_PREFIXES):
                findings.append(f"{path.relative_to(REPO_ROOT)}:{imported}")
    assert findings == []


def test_config_tombstones_exist_only_in_exact_validator_allowlist() -> None:
    from deerflow.config.app_config import LEGACY_CONFIG_TOMBSTONES

    assert LEGACY_CONFIG_TOMBSTONES == CONFIG_TOMBSTONES
    findings: list[str] = []
    config_root = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "config"
    for path in config_root.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in CONFIG_TOMBSTONE_ALLOWLIST:
            continue
        legacy_strings = CONFIG_TOMBSTONES & _python_string_literals(path)
        legacy_symbols = CONFIG_TOMBSTONES & _python_symbols(path)
        for key in sorted(legacy_strings | legacy_symbols):
            findings.append(f"{relative}:{key}")
    assert findings == []


def _install_mutation_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative: str,
    content: str,
) -> None:
    repo_root = tmp_path / "repo"
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("test_m7_source_absence.REPO_ROOT", repo_root)
    monkeypatch.setattr("test_m7_source_absence.BACKEND_ROOT", repo_root / "backend")


def _assert_gate_rejects(gate, message: str) -> None:
    try:
        gate()
    except AssertionError:
        return
    raise AssertionError(message)


def test_source_gate_mutation_rejects_unquoted_nginx_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "docker/nginx/nginx.conf",
        "server { location /api/threads { proxy_pass http://gateway; } }\n",
    )
    _assert_gate_rejects(
        test_production_sources_have_no_legacy_route_literals,
        "source gate accepted an unquoted legacy Nginx location",
    )


def test_source_gate_mutation_rejects_config_key_literal_outside_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "backend/packages/harness/deerflow/config/other.py",
        'REMOVED_BACKEND = "run_events"\n',
    )
    _assert_gate_rejects(
        test_config_tombstones_exist_only_in_exact_validator_allowlist,
        "source gate accepted a legacy config key outside app_config.py",
    )


def test_source_gate_mutation_ignores_removed_symbol_in_python_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "backend/app/safe.py",
        "# setup_agent is historical prose, not an executable symbol\nVALUE = 1\n",
    )
    test_production_sources_have_no_legacy_route_literals()
