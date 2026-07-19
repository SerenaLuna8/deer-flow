"""Lock the M7 production tree to the project-only runtime surface."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"

BANNED_PRODUCTION_PATHS = (
    "app/automations/cutover.py",
    "app/automations/legacy_reads.py",
    "app/channels/runtime_config_store.py",
    "app/gateway/routers/agents.py",
    "app/gateway/routers/artifacts.py",
    "app/gateway/routers/channel_connections.py",
    "app/gateway/routers/channels.py",
    "app/gateway/routers/features.py",
    "app/gateway/routers/feedback.py",
    "app/gateway/routers/mcp.py",
    "app/gateway/routers/memory.py",
    "app/gateway/routers/runs.py",
    "app/gateway/routers/scheduled_tasks.py",
    "app/gateway/routers/skills.py",
    "app/gateway/routers/thread_runs.py",
    "app/gateway/routers/threads.py",
    "app/gateway/routers/uploads.py",
    "app/private_work/cutover.py",
    "app/reliability/cutover.py",
    "packages/harness/deerflow/config/extensions_config.py",
    "packages/harness/deerflow/persistence/migration_ledger",
    "packages/harness/deerflow/runtime/stream_bridge",
    "packages/harness/deerflow/tools/builtins/setup_agent_tool.py",
    "packages/harness/deerflow/tools/builtins/update_agent_tool.py",
)

BANNED_ROUTE_LITERALS = frozenset(
    {
        "/api/agents",
        "/api/artifacts",
        "/api/channel-connections",
        "/api/channels",
        "/api/features",
        "/api/feedback",
        "/api/langgraph",
        "/api/mcp/config",
        "/api/memory",
        "/api/runs",
        "/api/scheduled-tasks",
        "/api/skills",
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
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _banned_path_has_production_source(path: Path) -> bool:
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    return any(child.is_file() and child.suffix in {".py", ".pyi", ".so"} for child in path.rglob("*"))


def test_deleted_production_paths_do_not_exist() -> None:
    for relative in BANNED_PRODUCTION_PATHS:
        assert not _banned_path_has_production_source(BACKEND_ROOT / relative), relative


def test_production_sources_have_no_legacy_route_literals() -> None:
    findings: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        for literal in BANNED_ROUTE_LITERALS:
            route_literal = re.compile(rf"""["'`]({re.escape(literal)})(?:[/{{?*"'`]|$)""")
            if route_literal.search(text):
                findings.append(f"{path.relative_to(REPO_ROOT)}:{literal}")
        for symbol in BANNED_RUNTIME_SYMBOLS:
            if re.search(rf"\b{re.escape(symbol)}\b", text):
                findings.append(f"{path.relative_to(REPO_ROOT)}:{symbol}")
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
    for path in _production_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if "LEGACY_CONFIG_TOMBSTONES" in text and relative not in CONFIG_TOMBSTONE_ALLOWLIST:
            findings.append(relative)
    assert findings == []
