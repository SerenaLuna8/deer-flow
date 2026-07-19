"""Lock the M7 production tree to the project-only runtime surface."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

M7_CHANGED_MARKDOWN_DOCS = (
    "AGENTS.md",
    "backend/AGENTS.md",
    "frontend/AGENTS.md",
    "README.md",
    "README_zh.md",
    "CHANGELOG.md",
    "docs/operations/m6-backup-recovery.md",
    "docs/superpowers/specs/2026-07-12-project-first-saas-design.md",
    "docs/superpowers/specs/2026-07-18-project-legacy-cleanup-m7-design.md",
    "docs/superpowers/plans/2026-07-18-project-legacy-cleanup-m7.md",
)

ACTIVE_OPERATIONAL_DOCS = (
    "AGENTS.md",
    "backend/AGENTS.md",
    "frontend/AGENTS.md",
    "README.md",
    "README_zh.md",
    "docs/operations/m6-backup-recovery.md",
)

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
CONFIG_TOMBSTONE_VALIDATORS = frozenset({"reject_removed_legacy_config", "_drop_null_config_sections"})

FRONTEND_PRODUCTION_SUFFIXES = frozenset({".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"})
FRONTEND_NON_PRODUCTION_DIRS = frozenset({"__fixtures__", "__mocks__", "__tests__", "fixtures", "stories", "tests"})
FRONTEND_NON_PRODUCTION_MARKERS = (
    ".spec.",
    ".stories.",
    ".story.",
    ".test.",
)


def _is_frontend_production_source(path: Path) -> bool:
    frontend_root = REPO_ROOT / "frontend" / "src"
    try:
        relative = path.relative_to(frontend_root)
    except ValueError:
        return False
    if path.suffix not in FRONTEND_PRODUCTION_SUFFIXES:
        return False
    if path.name.endswith(".d.ts"):
        return False
    if any(part in FRONTEND_NON_PRODUCTION_DIRS for part in relative.parts[:-1]):
        return False
    return not any(marker in path.name for marker in FRONTEND_NON_PRODUCTION_MARKERS)


def _production_files() -> tuple[Path, ...]:
    backend_roots = (
        BACKEND_ROOT / "app",
        BACKEND_ROOT / "packages" / "harness" / "deerflow",
        BACKEND_ROOT / "scripts",
    )
    backend_files = (path for root in backend_roots for path in root.rglob("*") if path.is_file() and path.suffix == ".py")
    frontend_root = REPO_ROOT / "frontend" / "src"
    frontend_files = (path for path in frontend_root.rglob("*") if path.is_file() and _is_frontend_production_source(path))
    nginx_root = REPO_ROOT / "docker" / "nginx"
    nginx_files = (path for path in nginx_root.rglob("*") if path.is_file() and path.suffix == ".conf")
    return tuple(sorted((*backend_files, *frontend_files, *nginx_files)))


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
    for match in re.finditer(rf"{re.escape(literal)}(?:[/{{?*]|$)", value):
        prefix = value[: match.start()]
        if not prefix or prefix.endswith("}") or "://" in prefix:
            return True
    return False


def _javascript_string_literals(text: str) -> tuple[str, ...]:
    strings: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            index = len(text) if closing == -1 else closing + 2
            continue
        quote = text[index]
        if quote not in {'"', "'", "`"}:
            index += 1
            continue
        index += 1
        value: list[str] = []
        while index < len(text):
            character = text[index]
            if character == "\\" and index + 1 < len(text):
                value.extend((character, text[index + 1]))
                index += 2
                continue
            if character == quote:
                index += 1
                break
            value.append(character)
            index += 1
        strings.append("".join(value))
    return tuple(strings)


def _nginx_directives(text: str) -> str:
    return "\n".join(line.partition("#")[0] for line in text.splitlines())


_INLINE_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\)")
_REFERENCE_MARKDOWN_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")


def _relative_markdown_targets(line: str) -> tuple[str, ...]:
    targets = [match.group("target") for match in _INLINE_MARKDOWN_LINK.finditer(line)]
    reference = _REFERENCE_MARKDOWN_LINK.match(line)
    if reference is not None:
        targets.append(reference.group("target"))
    return tuple(targets)


def _markdown_link_findings(documents: tuple[Path, ...], *, repo_root: Path) -> list[str]:
    findings: list[str] = []
    for document in documents:
        relative_document = document.relative_to(repo_root).as_posix()
        if not document.is_file():
            findings.append(f"{relative_document}:0:<missing-document>")
            continue
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            for raw_target in _relative_markdown_targets(line):
                target = raw_target[1:-1] if raw_target.startswith("<") and raw_target.endswith(">") else raw_target
                if not target or target.startswith(("#", "/", "//")):
                    continue
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                decoded_path = unquote(parsed.path)
                if not decoded_path:
                    continue
                if not (document.parent / decoded_path).exists():
                    findings.append(f"{relative_document}:{line_number}:{raw_target}")
    return findings


_ACTIVE_DOC_RESIDUE_PATTERNS = (
    re.compile(r"\bmigrate-(?:sqlite|assets|private-work|automations|reliability)\b"),
    re.compile(r"\bmigrate_user_isolation\.py\b"),
    *(re.compile(re.escape(route)) for route in sorted(BANNED_ROUTE_LITERALS)),
    re.compile(r"\bextensions_config(?:\.example)?\.json\b"),
    re.compile(r"\b(?:agents_api|stream_bridge)\b"),
    re.compile(r"\bcutover\b", re.IGNORECASE),
    re.compile(r"\b6/8\b"),
    re.compile(r"\b75%\b"),
)


def _active_doc_residue_findings(documents: tuple[Path, ...], *, repo_root: Path) -> list[str]:
    findings: list[str] = []
    for document in documents:
        relative_document = document.relative_to(repo_root).as_posix()
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in _ACTIVE_DOC_RESIDUE_PATTERNS:
                match = pattern.search(line)
                if match is not None:
                    findings.append(f"{relative_document}:{line_number}:{match.group(0)}")
    return findings


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
            directives = _nginx_directives(text)
            for literal in BANNED_ROUTE_LITERALS:
                nginx_route = re.compile(rf"\b(?:location|rewrite)\b[^\n]*{re.escape(literal)}(?:[/{{?*\s]|$)")
                if nginx_route.search(directives):
                    findings.append(f"{path.relative_to(REPO_ROOT)}:{literal}")
        else:
            strings = _javascript_string_literals(text)
            for literal in BANNED_ROUTE_LITERALS:
                if any(_contains_route(value, literal) for value in strings):
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
        if path.name == "app_config.py":
            findings.extend(_app_config_tombstone_findings(path, relative))
            continue
        legacy_strings = CONFIG_TOMBSTONES & _python_string_literals(path)
        legacy_symbols = CONFIG_TOMBSTONES & _python_symbols(path)
        for key in sorted(legacy_strings | legacy_symbols):
            findings.append(f"{relative}:{key}")
    assert findings == []


def _is_before_model_validator(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "model_validator":
            continue
        if any(keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "before" for keyword in decorator.keywords):
            return True
    return False


def _app_config_tombstone_findings(path: Path, relative: str) -> list[str]:
    tree = _python_tree(path)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    definitions = [node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "LEGACY_CONFIG_TOMBSTONES" for target in node.targets)]
    findings: list[str] = []
    if len(definitions) != 1:
        findings.append(f"{relative}:LEGACY_CONFIG_TOMBSTONES:definition")
        definition_nodes: set[ast.AST] = set()
    else:
        definition_nodes = set(ast.walk(definitions[0]))
        definition_strings = {node.value for node in definition_nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        if definition_strings != CONFIG_TOMBSTONES:
            findings.append(f"{relative}:LEGACY_CONFIG_TOMBSTONES:definition-values")

    validators: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in CONFIG_TOMBSTONE_VALIDATORS and _is_before_model_validator(node):
            validators[node.name] = node
    for missing in sorted(CONFIG_TOMBSTONE_VALIDATORS - validators.keys()):
        findings.append(f"{relative}:LEGACY_CONFIG_TOMBSTONES:{missing}:missing")

    allowed_consumers: set[ast.Name] = set()
    reject_validator = validators.get("reject_removed_legacy_config")
    if reject_validator is not None:
        for node in ast.walk(reject_validator):
            if not isinstance(node, ast.Name) or node.id != "LEGACY_CONFIG_TOMBSTONES":
                continue
            attribute = parents.get(node)
            call = parents.get(attribute) if attribute is not None else None
            if (
                isinstance(attribute, ast.Attribute)
                and attribute.value is node
                and attribute.attr == "intersection"
                and isinstance(call, ast.Call)
                and call.func is attribute
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "value"
            ):
                allowed_consumers.add(node)

    null_validator = validators.get("_drop_null_config_sections")
    if null_validator is not None:
        for node in ast.walk(null_validator):
            if not isinstance(node, ast.Name) or node.id != "LEGACY_CONFIG_TOMBSTONES":
                continue
            comparison = parents.get(node)
            if (
                isinstance(comparison, ast.Compare)
                and len(comparison.ops) == 1
                and isinstance(comparison.ops[0], ast.In)
                and len(comparison.comparators) == 1
                and comparison.comparators[0] is node
                and isinstance(comparison.left, ast.Name)
                and comparison.left.id == "key"
            ):
                allowed_consumers.add(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in CONFIG_TOMBSTONES and node not in definition_nodes:
            findings.append(f"{relative}:{node.value}:literal:{node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "LEGACY_CONFIG_TOMBSTONES" and isinstance(node.ctx, ast.Load) and node not in allowed_consumers:
            findings.append(f"{relative}:LEGACY_CONFIG_TOMBSTONES:use:{node.lineno}")
    for symbol in sorted(CONFIG_TOMBSTONES & _python_symbols(path)):
        findings.append(f"{relative}:{symbol}:symbol")
    return findings


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


def test_source_gate_mutation_rejects_unquoted_nginx_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "docker/nginx/nginx.conf",
        "server { rewrite ^/api/threads/(.*)$ /api/projects/$1 break; }\n",
    )
    _assert_gate_rejects(
        test_production_sources_have_no_legacy_route_literals,
        "source gate accepted an unquoted legacy Nginx rewrite",
    )


def test_source_gate_mutation_ignores_commented_nginx_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "docker/nginx/nginx.conf",
        "# location /api/threads { proxy_pass http://gateway; }\nserver { location /api/projects { proxy_pass http://gateway; } }\n",
    )
    test_production_sources_have_no_legacy_route_literals()


def test_markdown_link_gate_mutation_rejects_missing_relative_target(tmp_path: Path) -> None:
    document = tmp_path / "docs" / "guide.md"
    document.parent.mkdir(parents=True)
    document.write_text("See [missing](../missing.md).\n", encoding="utf-8")

    assert _markdown_link_findings((document,), repo_root=tmp_path) == [
        "docs/guide.md:1:../missing.md",
    ]


def test_markdown_link_gate_mutation_accepts_relative_target_and_ignores_remote_or_anchor(tmp_path: Path) -> None:
    document = tmp_path / "docs" / "guide.md"
    target = tmp_path / "README.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "[local](../README.md#setup) [remote](https://example.com/x) [anchor](#local)\n",
        encoding="utf-8",
    )
    target.write_text("# Setup\n", encoding="utf-8")

    assert _markdown_link_findings((document,), repo_root=tmp_path) == []


def test_active_doc_gate_mutation_rejects_removed_command_and_global_api(tmp_path: Path) -> None:
    document = tmp_path / "README.md"
    document.write_text(
        "Run `make migrate-private-work`, then call `/api/threads`.\n",
        encoding="utf-8",
    )

    assert _active_doc_residue_findings((document,), repo_root=tmp_path) == [
        "README.md:1:migrate-private-work",
        "README.md:1:/api/threads",
    ]


def test_all_m7_changed_markdown_links_resolve() -> None:
    documents = tuple(REPO_ROOT / relative for relative in M7_CHANGED_MARKDOWN_DOCS)
    assert _markdown_link_findings(documents, repo_root=REPO_ROOT) == []


def test_active_operational_docs_describe_only_final_m7_surfaces() -> None:
    documents = tuple(REPO_ROOT / relative for relative in ACTIVE_OPERATIONAL_DOCS)
    assert _active_doc_residue_findings(documents, repo_root=REPO_ROOT) == []


@pytest.mark.parametrize(
    "relative",
    (
        "frontend/src/hooks/useLegacy.ts",
        "frontend/src/core/legacy.ts",
        "frontend/src/core/legacy.tsx",
        "frontend/src/core/legacy.js",
    ),
)
def test_source_gate_mutation_rejects_exact_frontend_route_literal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative: str,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        relative,
        'const url = "/api/threads";\n',
    )
    _assert_gate_rejects(
        test_production_sources_have_no_legacy_route_literals,
        f"source gate accepted an exact legacy route in {relative}",
    )


def test_source_gate_mutation_excludes_frontend_test_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "frontend/src/__tests__/legacy.test.ts",
        'const historicalFixture = "/api/threads";\n',
    )
    test_production_sources_have_no_legacy_route_literals()


@pytest.mark.parametrize(
    "comment",
    (
        '// const legacy = "/api/threads";\nconst current = "/api/projects";\n',
        '/* const legacy = "/api/threads";\nconst second = `/api/runs`; */\nconst current = "/api/projects";\n',
    ),
    ids=("line-comment", "multiline-block-comment"),
)
def test_source_gate_mutation_ignores_javascript_comment_literals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    comment: str,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "frontend/src/hooks/useLegacy.ts",
        comment,
    )
    test_production_sources_have_no_legacy_route_literals()


@pytest.mark.parametrize(
    "source",
    (
        'const marker = "// not a comment"; const route = "/api/threads";\n',
        'const marker = "/* not a comment */"; const route = "/api/threads";\n',
        'const marker = "\\"// escaped quote"; const route = "/api/threads";\n',
        "const marker = `/* template text */`; const route = `/api/threads`;\n",
        'const route = "https://gateway.example/api/threads";\n',
    ),
    ids=(
        "line-marker-in-string",
        "block-marker-in-string",
        "escaped-quote-before-line-marker",
        "block-marker-in-template",
        "url-double-slash",
    ),
)
def test_source_gate_mutation_keeps_comment_markers_inside_strings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "frontend/src/hooks/useLegacy.ts",
        source,
    )
    _assert_gate_rejects(
        test_production_sources_have_no_legacy_route_literals,
        "source gate let a string-contained comment marker hide a legacy route",
    )


@pytest.mark.parametrize(
    "source",
    (
        "const route = '/api/threads';\n",
        'const route = "/api/threads";\n',
        "const route = `/api/threads`;\n",
    ),
    ids=("single-quoted", "double-quoted", "template-literal"),
)
def test_source_gate_mutation_rejects_javascript_string_quote_styles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "frontend/src/hooks/useLegacy.ts",
        source,
    )
    _assert_gate_rejects(
        test_production_sources_have_no_legacy_route_literals,
        "source gate accepted a real legacy JavaScript string literal",
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


def test_source_gate_mutation_rejects_app_config_literal_outside_validators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_config = (BACKEND_ROOT / "packages" / "harness" / "deerflow" / "config" / "app_config.py").read_text(encoding="utf-8")
    _install_mutation_repo(
        monkeypatch,
        tmp_path,
        "backend/packages/harness/deerflow/config/app_config.py",
        f'{app_config}\nREINTRODUCED = "run_events"\n',
    )
    _assert_gate_rejects(
        test_config_tombstones_exist_only_in_exact_validator_allowlist,
        "source gate accepted a tombstone literal outside app_config validators",
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
