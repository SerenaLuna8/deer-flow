"""Architecture boundary tests for governed model construction."""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_DIRECT_CALLERS = {
    Path("packages/harness/deerflow/models/factory.py"),
    Path("packages/harness/deerflow/models/runtime.py"),
}
_ALLOWED_PROVIDER_IMPORTERS = {
    # Provider type introspection and generic construction boundary.
    Path("packages/harness/deerflow/models/factory.py"),
    # Provider-specific serialization/response compatibility adapters.
    Path("packages/harness/deerflow/models/patched_deepseek.py"),
    # Provider-specific transport outcome proof for retry safety.
    Path("packages/harness/deerflow/models/provider_outcome.py"),
    Path("packages/harness/deerflow/models/provider_wire.py"),
    Path("packages/harness/deerflow/models/vllm_provider.py"),
}
_PROVIDER_PACKAGES = {
    "anthropic",
    "langchain_anthropic",
    "langchain_deepseek",
    "langchain_openai",
    "openai",
}
_MODEL_EXECUTION_METHODS = {
    "abatch",
    "abatch_as_completed",
    "agenerate",
    "ainvoke",
    "astream",
    "batch",
    "batch_as_completed",
    "bind_tools",
    "generate",
    "invoke",
    "stream",
    "with_structured_output",
}
# ``build_chat_model`` deliberately returns a LangChain ``BaseChatModel`` for
# graph composition and stateful tool loops. LangGraph owns graph streaming and
# middleware execution; Runtime governs invocations it can wrap, including
# tool-bound Runnables. The remaining raw binding/in-graph auxiliary operations
# are explicit here. Every other production caller must use ModelRuntime or hand
# the Runtime-built model to LangGraph without invoking it itself.
_ALLOWED_RAW_MODEL_EXECUTION: dict[Path, frozenset[str]] = {
    # Runtime dynamically resolves Runnable.invoke/ainvoke and is the canonical
    # governed invocation boundary for models and tool-bound Runnables.
    Path("packages/harness/deerflow/models/runtime.py"): frozenset({"ainvoke", "astream", "invoke"}),
    # Dream is a bounded stateful tool loop. Tool binding must remain raw, but
    # every bound-Runnable invocation is governed by ModelRuntime.
    Path("packages/harness/deerflow/agents/memory/dream.py"): frozenset({"bind_tools"}),
}


def _is_create_chat_model_call(node: ast.Call) -> bool:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id == "create_chat_model"
    return isinstance(function, ast.Attribute) and function.attr == "create_chat_model"


def _imports_create_chat_model(node: ast.ImportFrom) -> bool:
    return any(alias.name == "create_chat_model" for alias in node.names)


def _dynamic_provider_import(node: ast.Call) -> str | None:
    function = node.func
    is_import = (isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}) or (isinstance(function, ast.Attribute) and function.attr == "import_module")
    if not is_import or not node.args:
        return None
    module = node.args[0]
    if not isinstance(module, ast.Constant) or not isinstance(module.value, str):
        return None
    package = module.value.split(".", 1)[0]
    return package if package in _PROVIDER_PACKAGES else None


def _model_receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _direct_model_execution_method(node: ast.Call) -> str | None:
    function = node.func
    method: str | None = None
    receiver_node: ast.expr | None = None
    if isinstance(function, ast.Attribute):
        method = function.attr
        receiver_node = function.value
    elif isinstance(function, ast.Name) and function.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        method = node.args[1].value
        receiver_node = node.args[0]
    if method not in _MODEL_EXECUTION_METHODS or receiver_node is None:
        return None
    receiver = _model_receiver_name(receiver_node)
    if receiver is None:
        return None
    normalized = receiver.lower()
    if normalized.endswith("runtime"):
        return None
    if not (normalized in {"bound", "runnable"} or "model" in normalized or "llm" in normalized):
        return None
    return method


def test_production_model_construction_is_owned_by_model_runtime() -> None:
    violations: list[str] = []

    production_paths = (
        *_BACKEND_ROOT.joinpath("app").rglob("*.py"),
        *_BACKEND_ROOT.joinpath("packages").rglob("*.py"),
    )
    for path in sorted(production_paths):
        relative = path.relative_to(_BACKEND_ROOT)
        if relative.parts[0] == "tests" or relative in _ALLOWED_DIRECT_CALLERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_create_chat_model_call(node):
                violations.append(f"{relative}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and _imports_create_chat_model(node):
                violations.append(f"{relative}:{node.lineno} (import)")

    assert violations == [], f"Production model construction must go through ModelRuntime; direct create_chat_model calls remain at: {', '.join(violations)}"


def test_provider_sdk_imports_are_confined_to_model_adapters() -> None:
    violations: list[str] = []
    observed_allowlist: set[Path] = set()
    production_paths = (
        *_BACKEND_ROOT.joinpath("app").rglob("*.py"),
        *_BACKEND_ROOT.joinpath("packages", "harness", "deerflow").rglob("*.py"),
    )
    for path in sorted(production_paths):
        relative = path.relative_to(_BACKEND_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            packages: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                packages.append(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Import):
                packages.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.Call):
                package = _dynamic_provider_import(node)
                if package is not None:
                    packages.append(package)
            provider_packages = _PROVIDER_PACKAGES.intersection(packages)
            if not provider_packages:
                continue
            if relative in _ALLOWED_PROVIDER_IMPORTERS:
                observed_allowlist.add(relative)
                continue
            suffix = " (dynamic import)" if isinstance(node, ast.Call) else ""
            violations.append(f"{relative}:{node.lineno}{suffix}")

    assert violations == [], "Provider SDK imports must remain inside deerflow.models adapters: " + ", ".join(violations)
    stale_allowlist = sorted(_ALLOWED_PROVIDER_IMPORTERS - observed_allowlist)
    assert stale_allowlist == [], "Remove stale Provider SDK importer allowlist entries: " + ", ".join(map(str, stale_allowlist))


def test_raw_model_execution_is_confined_to_explicit_orchestration_boundaries() -> None:
    violations: list[str] = []
    observed_allowlist: set[tuple[Path, str]] = set()
    production_paths = (
        *_BACKEND_ROOT.joinpath("app").rglob("*.py"),
        *_BACKEND_ROOT.joinpath("packages", "harness", "deerflow").rglob("*.py"),
    )

    for path in sorted(production_paths):
        relative = path.relative_to(_BACKEND_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        allowed_methods = _ALLOWED_RAW_MODEL_EXECUTION.get(relative, frozenset())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = _direct_model_execution_method(node)
            if method is None:
                continue
            if method in allowed_methods:
                observed_allowlist.add((relative, method))
                continue
            violations.append(f"{relative}:{node.lineno} ({method})")

    expected_allowlist = {(path, method) for path, methods in _ALLOWED_RAW_MODEL_EXECUTION.items() for method in methods}
    stale_allowlist = sorted(expected_allowlist - observed_allowlist)
    assert violations == [], "Direct BaseChatModel execution must use ModelRuntime.ainvoke or an explicit graph/tool-loop boundary: " + ", ".join(violations)
    assert stale_allowlist == [], "Remove stale raw-model execution allowlist entries: " + ", ".join(f"{path} ({method})" for path, method in stale_allowlist)


def test_vision_package_has_no_second_provider_client_stack() -> None:
    vision_root = _BACKEND_ROOT / "packages" / "harness" / "deerflow" / "vision"

    assert not (vision_root / "client.py").exists()
    assert not (vision_root / "openai_compatible.py").exists()
    assert not (vision_root / "compatibility.py").exists()
    for path in vision_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "httpx" not in source
        assert "ChatOpenAI" not in source
        assert "ChatAnthropic" not in source
