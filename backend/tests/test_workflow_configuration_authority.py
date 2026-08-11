from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig, is_workflow_product_config_key

REPO_ROOT = Path(__file__).resolve().parents[2]
HELM_ROOT = REPO_ROOT / "deploy" / "helm" / "deer-flow"
FRONTEND_ROOT = REPO_ROOT / "frontend"
BACKEND_PRODUCTION_ROOTS = (
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "backend" / "packages" / "harness" / "deerflow",
    REPO_ROOT / "backend" / "scripts",
)

_MINIMAL_APP_CONFIG: dict[str, object] = {
    "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    "database": {"url": "postgresql://workflow-test:unused@localhost/workflow-test"},
}


def _compact_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _is_workflow_environment_name(value: str) -> bool:
    return "workflow" in _compact_identifier(value)


def _mapping_workflow_keys(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return [str(key) for key in value if is_workflow_product_config_key(key)]


@pytest.mark.parametrize(
    "key",
    [
        "workflow",
        "workflows",
        "workflow_runtime",
        "Workflow-Runtime",
        "WORKFLOW.RUNTIME",
        "workflow code",
        "workflow/http",
        "workflowRetry",
        "workflows_retention",
    ],
)
@pytest.mark.parametrize("value", [None, False, 0, {}, {"enabled": True}])
def test_app_config_rejects_every_top_level_workflow_product_namespace_for_all_sources(
    key: str,
    value: object,
) -> None:
    with pytest.raises(
        ValidationError,
        match=r"WORKFLOW_PRODUCT_CONFIG_FORBIDDEN:",
    ):
        AppConfig.model_validate({**_MINIMAL_APP_CONFIG, key: value})


def test_app_config_workflow_guard_does_not_reject_ordinary_nested_tool_options() -> None:
    config = AppConfig.model_validate(
        {
            **_MINIMAL_APP_CONFIG,
            "tools": [
                {
                    "name": "ordinary_transformer",
                    "group": "project-tools",
                    "use": "example.tools:ordinary_transformer",
                    "workflow_retry_hint": 2,
                }
            ],
            "ordinary_extension": {
                "display_label": "workflow helper",
            },
        }
    )

    assert config.tools[0].model_extra == {"workflow_retry_hint": 2}
    assert config.model_extra == {"ordinary_extension": {"display_label": "workflow helper"}}


@pytest.mark.parametrize(
    "path_environment_name",
    ["ACT_WEAVE_CONFIG_PATH", "DEER_FLOW_CONFIG_PATH"],
)
def test_both_config_path_aliases_reject_workflow_product_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path_environment_name: str,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "config_version: 37",
                "sandbox:",
                "  use: deerflow.sandbox.local:LocalSandboxProvider",
                "database:",
                "  url: postgresql://workflow-test:unused@localhost/workflow-test",
                "workflow_runtime:",
                "  enabled: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ACT_WEAVE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DEER_FLOW_CONFIG_PATH", raising=False)
    monkeypatch.setenv(path_environment_name, str(path))

    with pytest.raises(
        ValidationError,
        match=r"WORKFLOW_PRODUCT_CONFIG_FORBIDDEN: workflow_runtime",
    ):
        AppConfig.from_file()


def test_ambient_workflow_environment_cannot_overlay_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = AppConfig.model_validate(_MINIMAL_APP_CONFIG).model_dump()
    for name in (
        "WORKFLOW_RUNTIME",
        "ACT_WEAVE_WORKFLOW_RUNTIME",
        "DEER_FLOW_WORKFLOW_RUNTIME",
        "WORKFLOW_CODE_PROFILE",
        "WORKFLOW_HTTP_RETRY",
        "NEXT_PUBLIC_WORKFLOW_RETENTION",
    ):
        monkeypatch.setenv(name, '{"enabled":true,"max_retry_attempts":99}')

    loaded = AppConfig.model_validate(_MINIMAL_APP_CONFIG)

    assert loaded.model_dump() == baseline
    assert not any(is_workflow_product_config_key(key) for key in (loaded.model_extra or {}))


def test_config_example_and_deployment_sources_define_no_workflow_product_policy() -> None:
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert _mapping_workflow_keys(example) == []

    values = yaml.safe_load((HELM_ROOT / "values.yaml").read_text(encoding="utf-8"))
    assert _mapping_workflow_keys(values) == []
    assert _mapping_workflow_keys(yaml.safe_load(values["config"])) == []

    schema_path = HELM_ROOT / "values.schema.json"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is True
    workflow_name_pattern = schema["propertyNames"]["not"]["pattern"]
    for rejected in (
        "workflow",
        "workflows",
        "workflow_runtime",
        "WorkflowRuntime",
        "WORKFLOW-RUNTIME",
        "work.flow.http",
        "work flow retention",
        "w-o-r-k_f.l/o wRuntime",
    ):
        assert re.search(workflow_name_pattern, rejected)
    for allowed in ("image", "worker", "scheduler", "config", "postgresql"):
        assert re.search(workflow_name_pattern, allowed) is None


def _compose_environment_names(compose: Mapping[str, object]) -> Iterable[str]:
    services = compose.get("services")
    if not isinstance(services, Mapping):
        return
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        environment = service.get("environment", {})
        if isinstance(environment, Mapping):
            yield from (str(key) for key in environment)
        elif isinstance(environment, list):
            for item in environment:
                if isinstance(item, str):
                    yield item.split("=", 1)[0]
        build = service.get("build")
        if not isinstance(build, Mapping):
            continue
        arguments = build.get("args", {})
        if isinstance(arguments, Mapping):
            yield from (str(key) for key in arguments)
        elif isinstance(arguments, list):
            for item in arguments:
                if isinstance(item, str):
                    yield item.split("=", 1)[0]


def test_compose_exposes_no_workflow_product_environment_or_build_argument() -> None:
    compose_files = sorted((REPO_ROOT / "docker").glob("docker-compose*.yaml"))
    assert compose_files
    violations: list[str] = []
    for path in compose_files:
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name in _compose_environment_names(compose):
            if _is_workflow_environment_name(name):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{name}")
    assert violations == []


def test_helm_templates_do_not_read_workflow_product_values_and_guard_embedded_config() -> None:
    violations: list[str] = []
    for path in sorted((HELM_ROOT / "templates").glob("*")):
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.Values\s*\.\s*([A-Za-z0-9_-]+)", source):
            if is_workflow_product_config_key(match.group(1)):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{match.group(1)}")
        for match in re.finditer(r"(?:get|index|dig)\s+\.Values\s+[\"']([^\"']+)", source):
            if is_workflow_product_config_key(match.group(1)):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{match.group(1)}")
    assert violations == []

    config_template = (HELM_ROOT / "templates" / "configmap-config.yaml").read_text(encoding="utf-8")
    assert "WORKFLOW_PRODUCT_CONFIG_FORBIDDEN" in config_template
    assert 'hasPrefix "workflow"' in config_template


def _python_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        yield from sorted(root.rglob("*.py"))


def _literal_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _annotation_mentions_app_config(annotation: ast.AST | None) -> bool:
    return annotation is not None and any(isinstance(child, ast.Name) and child.id == "AppConfig" for child in ast.walk(annotation))


def _app_config_receiver_names(tree: ast.AST) -> set[str]:
    names = {"app_config"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for argument in arguments:
                if _annotation_mentions_app_config(argument.annotation):
                    names.add(argument.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _annotation_mentions_app_config(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, (ast.Assign, ast.NamedExpr)):
            value = node.value
            if not (isinstance(value, ast.Call) and ((isinstance(value.func, ast.Name) and value.func.id == "get_app_config") or (isinstance(value.func, ast.Attribute) and value.func.attr == "get_app_config"))):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _node_mentions_app_config(
    node: ast.AST,
    *,
    receiver_names: set[str],
) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in receiver_names:
            return True
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name) and child.func.id == "get_app_config":
                return True
            if isinstance(child.func, ast.Attribute) and child.func.attr == "get_app_config":
                return True
    return False


def _backend_workflow_authority_reads(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    app_config_receiver_names = _app_config_receiver_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "os" and node.func.attr == "getenv" and node.args:
                name = _literal_string(node.args[0])
                if name is not None and _is_workflow_environment_name(name):
                    violations.append(f"line {node.lineno}:os.getenv({name})")
            if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 and _node_mentions_app_config(node.args[0], receiver_names=app_config_receiver_names):
                key = _literal_string(node.args[1])
                if key is not None and is_workflow_product_config_key(key):
                    violations.append(f"line {node.lineno}:getattr({key})")
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args and _node_mentions_app_config(node.func.value, receiver_names=app_config_receiver_names):
                key = _literal_string(node.args[0])
                if key is not None and is_workflow_product_config_key(key):
                    violations.append(f"line {node.lineno}:config.get({key})")
        elif isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) and node.value.value.id == "os" and node.value.attr == "environ":
                name = _literal_string(node.slice)
                if name is not None and _is_workflow_environment_name(name):
                    violations.append(f"line {node.lineno}:os.environ[{name}]")
        elif isinstance(node, ast.Attribute) and is_workflow_product_config_key(node.attr) and _node_mentions_app_config(node.value, receiver_names=app_config_receiver_names):
            violations.append(f"line {node.lineno}:config.{node.attr}")
    return violations


def test_database_system_setting_workflow_runtime_section_is_not_an_app_config_violation(
    tmp_path: Path,
) -> None:
    source = "\n".join(
        [
            'WORKFLOW_RUNTIME_SECTION = "workflow_runtime"',
            "def read_database_policy(system_settings):",
            '    return system_settings.get("workflow_runtime")',
        ]
    )
    path = tmp_path / "database_system_settings.py"
    path.write_text(source, encoding="utf-8")

    assert _backend_workflow_authority_reads(path) == []

    runtime_policy_path = REPO_ROOT / "backend" / "app" / "workflows" / "runtime_policy.py"
    assert 'Literal["workflow_runtime"]' in runtime_policy_path.read_text(encoding="utf-8")
    assert _backend_workflow_authority_reads(runtime_policy_path) == []


def test_backend_has_no_workflow_product_environment_or_app_config_read_path() -> None:
    violations: list[str] = []
    for path in _python_files(BACKEND_PRODUCTION_ROOTS):
        for violation in _backend_workflow_authority_reads(path):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{violation}")
    assert violations == []


_DIRECT_PROCESS_ENV = re.compile(r"process\.env(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*[\"']([^\"']+)[\"']\s*\])")


def test_frontend_has_no_workflow_product_browser_or_server_build_environment_read() -> None:
    production_files = [
        *sorted((FRONTEND_ROOT / "src").rglob("*.ts")),
        *sorted((FRONTEND_ROOT / "src").rglob("*.tsx")),
        FRONTEND_ROOT / "src" / "env.js",
        FRONTEND_ROOT / "next.config.js",
        FRONTEND_ROOT / "Dockerfile",
    ]
    violations: list[str] = []
    for path in production_files:
        source = path.read_text(encoding="utf-8")
        for match in _DIRECT_PROCESS_ENV.finditer(source):
            name = match.group(1) or match.group(2)
            if name is not None and _is_workflow_environment_name(name):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{name}")

    env_contract_sources = (
        (FRONTEND_ROOT / "src" / "env.js").read_text(encoding="utf-8"),
        (FRONTEND_ROOT / "next.config.js").read_text(encoding="utf-8"),
        (FRONTEND_ROOT / "Dockerfile").read_text(encoding="utf-8"),
    )
    for source in env_contract_sources:
        for name in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", source):
            if _is_workflow_environment_name(name):
                violations.append(f"frontend-env-contract:{name}")

    assert violations == []
