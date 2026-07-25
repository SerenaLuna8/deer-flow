from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scripts.release_acceptance.models import ReleaseEvidence, ReviewReport

FROZEN_MATRIX_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "actors": (
        "unauthenticated",
        "project_outsider",
        "admin",
        "editor",
        "runner",
        "viewer",
        "different_owner",
        "different_project",
        "different_account",
        "removed_membership",
        "left_membership",
        "stale_membership",
        "pending_deletion_project",
        "suspended_project",
        "system_admin_with_membership",
        "system_admin_without_membership",
        "ordinary_platform_user",
    ),
    "account_relationships": ("unauthenticated", "same_account", "different_account"),
    "project_relationships": ("none", "same_project", "different_project", "project_outsider"),
    "membership_states": (
        "none",
        "active_admin",
        "active_editor",
        "active_runner",
        "active_viewer",
        "removed",
        "left",
        "stale_version",
        "pending_deletion",
        "suspended",
    ),
    "platform_roles": ("user", "system_admin"),
    "resource_families": (
        "auth",
        "project",
        "membership",
        "invite",
        "lifecycle",
        "agent",
        "skill",
        "mcp",
        "version",
        "binding",
        "credential",
        "thread",
        "message",
        "run",
        "run_event",
        "checkpoint",
        "file",
        "artifact",
        "memory",
        "connection",
        "automation",
        "occurrence",
        "result",
        "job",
        "dead_job",
        "quota",
        "usage",
        "audit",
        "retention",
        "admin",
        "channel",
    ),
    "scopes": ("account", "workspace", "project_shared", "project_private", "project_governance", "system_governance"),
    "ownerships": ("not_applicable", "own", "other_owner", "server_owned"),
    "operations": (
        "create",
        "list",
        "search",
        "page",
        "get",
        "export",
        "update",
        "delete",
        "publish",
        "bind",
        "approve",
        "run",
        "stop",
        "stream",
        "reconnect",
        "manual",
        "automatic",
        "retry",
        "requeue",
        "restore",
        "purge",
    ),
    "layers": ("frontend", "api", "service", "repository", "database", "worker", "scheduler"),
}

_PYTEST_SELECTOR = re.compile(r"^pytest::(?P<path>tests/[A-Za-z0-9_./-]+\.py)::(?P<node>[A-Za-z_][A-Za-z0-9_]*)$")
_PLAYWRIGHT_SELECTOR = re.compile(r"^playwright::(?P<path>tests/e2e/[A-Za-z0-9_./-]+\.spec\.ts)::(?P<title>[^\r\n]+)$")
_PLAYWRIGHT_TEST = re.compile(r'\btest(?:\.(?P<modifier>skip|fixme))?\(\s*["\'](?P<title>[^"\']+)["\']')
_REQUEST_EXPORT = re.compile(
    r"^export\s+(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)(?:<[^()\r\n]+>)?\s*\(",
    re.MULTILINE,
)
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_FRONTEND_NON_REQUEST_EXPORTS = frozenset(
    {
        "frontend/src/core/admin-operations/api.ts:operationsOverviewQueryOptions",
        "frontend/src/core/admin-operations/api.ts:adminProjectsQueryOptions",
        "frontend/src/core/admin-operations/api.ts:adminJobsQueryOptions",
        "frontend/src/core/admin-operations/api.ts:adminAuditQueryOptions",
        "frontend/src/core/admin-operations/api.ts:safeRequeueMutationOptions",
        "frontend/src/core/admin-operations/api.ts:useOperationsOverview",
        "frontend/src/core/admin-operations/api.ts:useAdminProjects",
        "frontend/src/core/admin-operations/api.ts:useAdminJobs",
        "frontend/src/core/admin-operations/api.ts:useAdminAudit",
        "frontend/src/core/admin-operations/api.ts:useSafeRequeue",
        "frontend/src/core/admin-operations/api.ts:adminProjectLifecycleMutationOptions",
        "frontend/src/core/admin-operations/api.ts:useAdminProjectLifecycle",
        "frontend/src/core/models/api.ts:loadModels",
        "frontend/src/core/private-work/connections.ts:projectConnectionsQueryKey",
        "frontend/src/core/private-work/connections.ts:projectConnectionProvidersQueryKey",
        "frontend/src/core/private-work/memory.ts:projectMemoryQueryKey",
        "frontend/src/core/private-work/memory.ts:projectMemoryMutationKey",
        "frontend/src/core/private-work/memory.ts:projectMemoryPermissions",
        "frontend/src/core/project-automations/api.ts:parseAutomationInput",
        "frontend/src/core/project-automations/api.ts:requestAutomation",
        "frontend/src/core/project-automations/api.ts:readAutomationResponse",
        "frontend/src/core/project-automations/api.ts:automationBaseURL",
        "frontend/src/core/project-automations/api.ts:createAutomationIdempotencyKey",
        "frontend/src/core/suggestions/api.ts:loadSuggestionsConfig",
        "frontend/src/core/threads/api.ts:buildRunMessagesUrl",
        "frontend/src/core/uploads/api.ts:supportsUploadLimits",
    }
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatrixDimensions(_ClosedModel):
    actors: tuple[str, ...]
    account_relationships: tuple[str, ...]
    project_relationships: tuple[str, ...]
    membership_states: tuple[str, ...]
    platform_roles: tuple[str, ...]
    resource_families: tuple[str, ...]
    scopes: tuple[str, ...]
    ownerships: tuple[str, ...]
    operations: tuple[str, ...]
    layers: tuple[str, ...]

    @model_validator(mode="after")
    def validate_frozen_dimensions(self):
        for name, expected in FROZEN_MATRIX_DIMENSIONS.items():
            if getattr(self, name) != expected:
                raise ValueError(f"matrix dimension drift: {name}")
        return self


class MatrixCase(_ClosedModel):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,}$", max_length=160)
    actor: str
    account_relationship: str
    project_relationship: str
    membership_state: str
    platform_role: str
    resource_family: str
    scope: str
    ownership: str
    operation: str
    expected_status: int = Field(ge=200, le=599)
    expected_code: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    expected_db_delta: int = Field(ge=0)
    layers: tuple[str, ...] = Field(min_length=1)
    evidence_selectors: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_contract(self):
        values = {
            "actors": self.actor,
            "account_relationships": self.account_relationship,
            "project_relationships": self.project_relationship,
            "membership_states": self.membership_state,
            "platform_roles": self.platform_role,
            "resource_families": self.resource_family,
            "scopes": self.scope,
            "ownerships": self.ownership,
            "operations": self.operation,
        }
        for dimension, value in values.items():
            if value not in FROZEN_MATRIX_DIMENSIONS[dimension]:
                raise ValueError(f"unknown matrix {dimension}: {value}")
        if len(set(self.layers)) != len(self.layers) or any(layer not in FROZEN_MATRIX_DIMENSIONS["layers"] for layer in self.layers):
            raise ValueError("invalid matrix layers")
        if len(set(self.evidence_selectors)) != len(self.evidence_selectors) or any(_selector_kind(selector) is None for selector in self.evidence_selectors):
            raise ValueError("invalid matrix evidence selector")
        if self.expected_status >= 400:
            if self.expected_code is None or self.expected_db_delta != 0 or "database" not in self.layers:
                raise ValueError("denial case must be side-effect free and database-backed")
            if not any(selector.startswith("pytest::") and "postgres.py::" in selector for selector in self.evidence_selectors):
                raise ValueError("denial case requires PostgreSQL evidence")
        elif self.expected_code is not None:
            raise ValueError("successful case cannot expose an error code")
        if "frontend" in self.layers and not any(selector.startswith("playwright::") for selector in self.evidence_selectors):
            raise ValueError("frontend case requires Playwright evidence")
        return self


class SurfaceManifest(_ClosedModel):
    count: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class IsolationMatrix(_ClosedModel):
    schema_version: int
    dimensions: MatrixDimensions
    surface_manifest: SurfaceManifest
    cases: tuple[MatrixCase, ...]

    @model_validator(mode="after")
    def validate_matrix_identity(self):
        if self.schema_version != 1:
            raise ValueError("unsupported matrix schema version")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate matrix case id")
        return self

    @property
    def case_ids(self) -> frozenset[str]:
        return frozenset(case.case_id for case in self.cases)

    def uncovered_dimensions(self) -> tuple[str, ...]:
        case_fields = {
            "actors": {case.actor for case in self.cases},
            "account_relationships": {case.account_relationship for case in self.cases},
            "project_relationships": {case.project_relationship for case in self.cases},
            "membership_states": {case.membership_state for case in self.cases},
            "platform_roles": {case.platform_role for case in self.cases},
            "resource_families": {case.resource_family for case in self.cases},
            "scopes": {case.scope for case in self.cases},
            "ownerships": {case.ownership for case in self.cases},
            "operations": {case.operation for case in self.cases},
            "layers": {layer for case in self.cases for layer in case.layers},
        }
        return tuple(f"{dimension}:{value}" for dimension, expected in FROZEN_MATRIX_DIMENSIONS.items() for value in expected if value not in case_fields[dimension])

    def pytest_selectors(self) -> tuple[str, ...]:
        return tuple(sorted({selector for case in self.cases for selector in case.evidence_selectors if selector.startswith("pytest::")}))

    def playwright_selectors(self) -> tuple[str, ...]:
        return tuple(sorted({selector for case in self.cases for selector in case.evidence_selectors if selector.startswith("playwright::")}))

    @staticmethod
    def discovered_surface_digest(discovered: Iterable[SurfaceRef]) -> str:
        return canonical_digest(
            [
                {
                    "layer": surface.layer,
                    "operation": surface.operation,
                    "resource_family": surface.resource_family,
                    "surface_id": surface.surface_id,
                }
                for surface in sorted(discovered)
            ]
        )

    def unmapped_surface(self, discovered: Iterable[SurfaceRef]) -> tuple[str, ...]:
        covered = {(case.resource_family, case.operation, layer) for case in self.cases for layer in case.layers}
        return tuple(sorted(surface.surface_id for surface in discovered if (surface.resource_family, surface.operation, surface.layer) not in covered))

    def orphaned_surface_cases(self, discovered: Iterable[SurfaceRef]) -> tuple[str, ...]:
        available = {(surface.resource_family, surface.operation, surface.layer) for surface in discovered}
        return tuple(sorted(case.case_id for case in self.cases if case.case_id.startswith("surface.") and not any((case.resource_family, case.operation, layer) in available for layer in case.layers)))


@dataclass(frozen=True, slots=True, order=True)
class SurfaceRef:
    surface_id: str
    resource_family: str
    operation: str
    layer: str


@dataclass(frozen=True, slots=True)
class SelectorValidationReport:
    missing: tuple[str, ...]
    skipped: tuple[str, ...]
    xfailed: tuple[str, ...]
    pytest_count: int
    playwright_count: int


def canonical_json_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def contract_digest(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return canonical_digest(value)


def schema_bytes(model: type[Any]) -> bytes:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("schema model must be a Pydantic model")
    encoded = json.dumps(model.model_json_schema(), sort_keys=True, indent=2, ensure_ascii=False)
    return (encoded + "\n").encode("utf-8")


def write_contract_schemas(repository_root: Path) -> None:
    contracts = repository_root / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "m8_release_evidence.schema.json").write_bytes(schema_bytes(ReleaseEvidence))
    (contracts / "m8_review_report.schema.json").write_bytes(schema_bytes(ReviewReport))


def load_isolation_matrix(path: Path) -> IsolationMatrix:
    with path.open("r", encoding="utf-8") as handle:
        return IsolationMatrix.model_validate(json.load(handle))


def validate_evidence_selectors(matrix: IsolationMatrix, repository_root: Path) -> SelectorValidationReport:
    missing: list[str] = []
    skipped: list[str] = []
    xfailed: list[str] = []
    pytest_count = 0
    playwright_count = 0
    python_cache: dict[Path, tuple[set[str], set[str], set[str]]] = {}
    playwright_cache: dict[Path, tuple[set[str], set[str]]] = {}

    for selector in (*matrix.pytest_selectors(), *matrix.playwright_selectors()):
        kind = _selector_kind(selector)
        if kind == "pytest":
            pytest_count += 1
            match = _PYTEST_SELECTOR.fullmatch(selector)
            if match is None:
                missing.append(selector)
                continue
            path = repository_root / "backend" / match.group("path")
            if path not in python_cache:
                python_cache[path] = _python_test_inventory(path)
            collected, static_skips, static_xfails = python_cache[path]
            node = match.group("node")
            if node not in collected:
                missing.append(selector)
            if node in static_skips:
                skipped.append(selector)
            if node in static_xfails:
                xfailed.append(selector)
        elif kind == "playwright":
            playwright_count += 1
            match = _PLAYWRIGHT_SELECTOR.fullmatch(selector)
            if match is None:
                missing.append(selector)
                continue
            path = repository_root / "frontend" / match.group("path")
            if path not in playwright_cache:
                playwright_cache[path] = _playwright_test_inventory(path)
            collected, static_skips = playwright_cache[path]
            title = match.group("title")
            if title not in collected:
                missing.append(selector)
            if title in static_skips:
                skipped.append(selector)
        else:
            missing.append(selector)
    return SelectorValidationReport(
        missing=tuple(sorted(missing)),
        skipped=tuple(sorted(skipped)),
        xfailed=tuple(sorted(xfailed)),
        pytest_count=pytest_count,
        playwright_count=playwright_count,
    )


def discover_scoped_surface(repository_root: Path) -> tuple[SurfaceRef, ...]:
    surfaces: set[SurfaceRef] = set()
    router_root = repository_root / "backend" / "app" / "gateway" / "routers"
    for path in sorted(router_root.glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        prefix = _router_prefix(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                route = _route_decorator(decorator)
                if route is None:
                    continue
                method, route_path = route
                full_path = prefix + route_path
                if not _is_scoped_route(path.stem, full_path):
                    continue
                operation = _operation_for_name(node.name, method=method, route_path=full_path)
                family = _resource_family(f"{path.stem}.{node.name}.{full_path}")
                surfaces.add(SurfaceRef(f"route:{path.relative_to(repository_root)}:{node.name}", family, operation, "api"))

    repository_files = {
        *repository_root.glob("backend/app/**/*repository.py"),
        *repository_root.glob("backend/app/**/repositories.py"),
        *repository_root.glob("backend/packages/harness/deerflow/persistence/**/sql.py"),
    }
    for path in sorted(repository_files):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or "repository" not in node.name.casefold():
                continue
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) or member.name.startswith("_"):
                    continue
                family = _resource_family(f"{path.stem}.{node.name}.{member.name}")
                operation = _operation_for_name(member.name)
                surfaces.add(
                    SurfaceRef(
                        f"repository:{path.relative_to(repository_root)}:{node.name}.{member.name}",
                        family,
                        operation,
                        "repository",
                    )
                )

    frontend_root = repository_root / "frontend" / "src" / "core"
    seen_frontend_exclusions: set[str] = set()
    for path in sorted(frontend_root.rglob("*.ts")):
        if path.name not in {"api.ts", "client.ts", "memory.ts", "connections.ts"}:
            continue
        source = path.read_text(encoding="utf-8")
        for name in _REQUEST_EXPORT.findall(source):
            export_id = f"{path.relative_to(repository_root)}:{name}"
            if export_id in _FRONTEND_NON_REQUEST_EXPORTS:
                seen_frontend_exclusions.add(export_id)
                continue
            operation = _operation_for_name(name)
            family = _resource_family(f"{path.parent.name}.{path.stem}.{name}")
            surfaces.add(SurfaceRef(f"frontend:{path.relative_to(repository_root)}:{name}", family, operation, "frontend"))
    for missing in sorted(_FRONTEND_NON_REQUEST_EXPORTS - seen_frontend_exclusions):
        surfaces.add(SurfaceRef(f"frontend-exclusion-drift:{missing}", "unknown", "unknown", "frontend"))
    return tuple(sorted(surfaces))


def _selector_kind(selector: str) -> str | None:
    pytest_match = _PYTEST_SELECTOR.fullmatch(selector)
    if pytest_match and _is_canonical_test_path(pytest_match.group("path")):
        return "pytest"
    playwright_match = _PLAYWRIGHT_SELECTOR.fullmatch(selector)
    if playwright_match and _is_canonical_test_path(playwright_match.group("path")):
        return "playwright"
    return None


def _is_canonical_test_path(value: str) -> bool:
    path = PurePosixPath(value)
    return str(path) == value and not any(part in {".", ".."} for part in path.parts)


def _python_test_inventory(path: Path) -> tuple[set[str], set[str], set[str]]:
    if not path.is_file():
        return set(), set(), set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collected: set[str] = set()
    skipped: set[str] = set()
    xfailed: set[str] = set()
    module_marks = _module_pytest_marks(tree)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
            continue
        collected.add(node.name)
        marks = module_marks | {_dotted_name(decorator) for decorator in node.decorator_list}
        if any(mark.endswith(("mark.skip", "mark.skipif")) for mark in marks):
            skipped.add(node.name)
        if any(mark.endswith("mark.xfail") for mark in marks):
            xfailed.add(node.name)
    return collected, skipped, xfailed


def _module_pytest_marks(tree: ast.Module) -> set[str]:
    marks: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            continue
        value = node.value
        items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        marks.update(_dotted_name(item) for item in items)
    return marks


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _playwright_test_inventory(path: Path) -> tuple[set[str], set[str]]:
    if not path.is_file():
        return set(), set()
    collected: set[str] = set()
    skipped: set[str] = set()
    for match in _PLAYWRIGHT_TEST.finditer(path.read_text(encoding="utf-8")):
        title = match.group("title")
        collected.add(title)
        if match.group("modifier"):
            skipped.add(title)
    return collected, skipped


def _router_prefix(tree: ast.Module) -> str:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id.endswith("router") for target in node.targets):
            continue
        if not isinstance(node.value, ast.Call) or _dotted_name(node.value.func).split(".")[-1] != "APIRouter":
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
    return ""


def _route_decorator(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr not in _HTTP_METHODS or not node.args:
        return None
    path = node.args[0]
    if not isinstance(path, ast.Constant) or not isinstance(path.value, str):
        return None
    return node.func.attr, path.value


def _is_scoped_route(module_name: str, route_path: str) -> bool:
    if "/projects" in route_path or "/admin" in route_path or "{project_id}" in route_path:
        return True
    return module_name.startswith(("project_", "admin_")) or module_name in {
        "automations",
        "connections",
        "memory",
        "notifications",
        "privacy_center",
        "private_work",
        "projects",
    }


def _resource_family(value: str) -> str:
    normalized = _normalized_identifier(value)
    # A Skill version file is a project-shared immutable Skill resource, not
    # project-private user file storage. Keep the more specific authority when
    # both words appear in route/repository/frontend identifiers.
    if "skill" in normalized and "file" in normalized:
        return "skill"
    candidates = (
        ("run_event", ("run_event", "stream", "event")),
        ("dead_job", ("dead_job", "deadjob", "dead")),
        ("membership", ("membership", "member")),
        # The current notification catalog is closed to project invitations;
        # keep its account-scoped recipient surfaces under Invite authority.
        ("invite", ("invitation", "invite", "notification")),
        ("lifecycle", ("lifecycle", "deletion", "recover")),
        ("version", ("catalog_state", "catalogstate")),
        ("automation", ("automation", "scheduled_task", "scheduledtask")),
        ("connection", ("connection",)),
        ("credential", ("credential",)),
        ("binding", ("binding",)),
        ("checkpoint", ("checkpoint",)),
        ("artifact", ("artifact",)),
        ("memory", ("memory",)),
        ("occurrence", ("occurrence",)),
        ("retention", ("retention", "privacy", "purge")),
        ("channel", ("channel", "webhook")),
        ("quota", ("quota",)),
        ("usage", ("usage",)),
        ("audit", ("audit",)),
        ("message", ("message", "feedback", "input_polish", "inputpolish")),
        ("thread", ("thread", "sidecar")),
        ("job", ("job",)),
        ("run", ("run", "subtask", "task")),
        ("file", ("file", "upload", "workspace_change", "workspacechange")),
        ("admin", ("system_asset_catalog", "systemassetcatalog")),
        ("agent", ("agent",)),
        ("skill", ("skill",)),
        ("mcp", ("mcp",)),
        ("version", ("version",)),
        ("agent", ("shared_asset", "sharedasset", "asset")),
        ("result", ("result",)),
        ("admin", ("admin", "operation")),
        ("auth", ("auth", "session")),
        ("project", ("project",)),
    )
    for family, needles in candidates:
        if any(needle in normalized for needle in needles):
            return family
    return "unknown"


def _operation_for_name(name: str, *, method: str | None = None, route_path: str = "") -> str:
    normalized = _normalized_identifier(f"{name}_{route_path}")
    if ("notification" in normalized and "accept" in normalized) or "for_accept" in normalized:
        return "approve"
    if normalized.startswith("set_automatic_display_name"):
        return "update"
    if normalized.startswith("current_published_descriptions"):
        return "list"
    # These semantic verbs must win before substring heuristics. In particular,
    # ``asset_id`` contains ``set_`` and would otherwise misclassify both routes
    # as updates. Forking creates a new immutable version; preview is read-only.
    if normalized.startswith("fork_"):
        return "create"
    if normalized.startswith("preview_"):
        return "get"
    if any(
        needle in normalized
        for needle in (
            "fetch_admin_projects",
            "fetch_admin_jobs",
            "fetch_admin_audit",
            "fetch_subtask_steps",
            "fetch_workspace_changes",
        )
    ):
        return "list"
    if "fetch_" in normalized or "load_" in normalized or "reload_" in normalized:
        return "get"
    explicit = (
        ("reconnect", ("reconnect", "last_event")),
        ("requeue", ("requeue",)),
        ("restore", ("restore", "recover")),
        ("publish", ("publish",)),
        ("approve", ("approve",)),
        ("export", ("export", "download")),
        ("stream", ("stream",)),
        ("search", ("search",)),
        ("page", ("paginate", "pagination", "page")),
        ("manual", ("manual", "trigger")),
        ("automatic", ("automatic", "scheduled", "claim_due")),
        ("retry", ("retry",)),
        ("purge", ("purge",)),
        ("bind", ("bind", "enable_system", "disable_system", "rollback_system", "upgrade_system")),
        ("stop", ("stop", "cancel", "abort")),
        ("delete", ("delete", "remove", "revoke", "leave", "disconnect", "close", "request_project_deletion")),
        (
            "update",
            (
                "update",
                "patch",
                "change",
                "configure",
                "migrate_",
                "replace",
                "pause",
                "resume",
                "pin",
                "enter",
                "attach",
                "begin_execution",
                "heartbeat",
                "mark_",
                "prepare_",
                "settle_",
                "set_",
                "suspend_",
                "override_grant",
                "bump_",
                "ensure_and_lock",
                "consume_",
                "finish",
                "reject_",
                "advance_",
                "end_",
                "compact_",
                "polish_",
            ),
        ),
        (
            "create",
            (
                "create",
                "insert",
                "stage",
                "finalize",
                "claim",
                "redeem",
                "begin_connect",
                "complete_callback",
                "add_",
                "append",
                "store_",
                "upsert",
                "enqueue",
                "put",
                "record",
                "connect_",
                "import_",
                "submit_",
                "branch_",
            ),
        ),
        (
            "list",
            (
                "list",
                "history",
                "visible",
                "count_",
                "all_versions",
                "fetch_admin_projects",
                "fetch_admin_jobs",
                "fetch_admin_audit",
                "fetch_subtask_steps",
                "fetch_workspace_changes",
            ),
        ),
        (
            "get",
            (
                "get",
                "read",
                "load",
                "resolve",
                "status",
                "detail",
                "current",
                "find",
                "assert_",
                "validate_",
                "check_",
                "coordinates",
                "predicates",
                "locate_",
                "lock_",
                "require_",
                "next_",
                "grant_state",
                "hash_",
                "has_",
                "counter",
                "ledger_entry",
                "policy",
                "threshold_",
                "transaction",
                "fetch_",
                "load_",
                "reload_",
            ),
        ),
        ("run", ("run", "admit", "execute")),
    )
    for operation, needles in explicit:
        if any(normalized.startswith(needle) if needle == "set_" else needle in normalized for needle in needles):
            return operation
    if method == "get":
        return "get" if "{" in route_path else "list"
    if method == "post":
        return "create"
    if method in {"put", "patch"}:
        return "update"
    if method == "delete":
        return "delete"
    return "unknown"


def _normalized_identifier(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).casefold().replace("-", "_")
