from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from langchain.tools import BaseTool

from deerflow.reflection import resolve_variable
from deerflow.sandbox import tools as legacy
from deerflow.sandbox.search import GrepMatch
from deerflow.sandbox.tooling.files import (
    _truncate_ls_output,
    _truncate_read_file_output,
)
from deerflow.sandbox.tooling.search_tools import (
    _format_glob_results,
    _format_grep_results,
)
from deerflow.sandbox.tools import write_file_tool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness"
TOOLING_ROOT = HARNESS_ROOT / "deerflow" / "sandbox" / "tooling"
LEGACY_MODULE = "deerflow.sandbox.tools"

EXPECTED_TOOL_SCHEMA_DIGESTS = {
    "bash_tool": "b07e70b944148a375b93af1750b64a8fae16c00d3299bfdc345518f75258e666",
    "ls_tool": "55c2718f133879b103d0387c00161c8a978a7b8ec269409c06dd6a1ce199a4a1",
    "glob_tool": "485f0916c26adf27eb0188e73a3524fff1d9149d76cdc2fc4fa2cbb199244f6c",
    "grep_tool": "d05bada5d19417c6e19e9ef025ff7e30ebc47f7d369d3c9fb3f6bc179bcd516e",
    "read_file_tool": "9b816fddbf8f58765692cd4e2e36ce7d3cbbdca75b3961aad08f0321c57efcd7",
    "write_file_tool": "a7040794be5144ae7d16e2437b1b5f89b551e288c9bc6780b7f85611c16f147a",
    "str_replace_tool": "3103e6f0c11943ed38de597478648bd9f809a5b41a1d70fb5ed1f00c0641fdbd",
}

EXPECTED_TOOL_SIGNATURES = {
    "bash_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, command: str) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, command: str) -> str | langgraph.types.Command"),
    ),
    "ls_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str) -> str"),
    ),
    "glob_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, pattern: str, path: str, include_dirs: bool = False, max_results: int = 200) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, pattern: str, path: str, include_dirs: bool = False, max_results: int = 200) -> str"),
    ),
    "grep_tool": (
        (
            "(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], "
            "deerflow.agents.thread_state.ThreadState], description: str, pattern: str, "
            "path: str, glob: str | None = None, literal: bool = False, "
            "case_sensitive: bool = False, max_results: int = 100) -> str"
        ),
        (
            "(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], "
            "deerflow.agents.thread_state.ThreadState], description: str, pattern: str, "
            "path: str, glob: str | None = None, literal: bool = False, "
            "case_sensitive: bool = False, max_results: int = 100) -> str"
        ),
    ),
    "read_file_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str"),
    ),
    "write_file_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, content: str, append: bool = False) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, content: str, append: bool = False) -> str"),
    ),
    "str_replace_tool": (
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str"),
        ("(runtime: langgraph.prebuilt.tool_node.ToolRuntime[dict[str, typing.Any], deerflow.agents.thread_state.ThreadState], description: str, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str"),
    ),
}


def _schema_digest(tool: BaseTool) -> str:
    payload = json.dumps(
        tool.tool_call_schema.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_batch4_tool_call_schemas_and_signatures_are_frozen() -> None:
    for attribute, expected_digest in EXPECTED_TOOL_SCHEMA_DIGESTS.items():
        tool = getattr(legacy, attribute)
        expected_func, expected_coroutine = EXPECTED_TOOL_SIGNATURES[attribute]
        assert _schema_digest(tool) == expected_digest
        assert str(inspect.signature(tool.func)) == expected_func
        assert str(inspect.signature(tool.coroutine)) == expected_coroutine


def test_batch4_legacy_tool_references_resolve_exact_objects() -> None:
    for attribute in EXPECTED_TOOL_SCHEMA_DIGESTS:
        resolved = resolve_variable(f"{LEGACY_MODULE}:{attribute}", BaseTool)
        assert resolved is getattr(legacy, attribute)


@pytest.mark.parametrize(
    "truncate",
    [_truncate_ls_output, _truncate_read_file_output],
)
def test_file_read_output_truncators_honor_the_exact_limit(truncate) -> None:
    result = truncate("A" * 300, 160)
    assert len(result) <= 160
    assert result.startswith("A")
    assert "[truncated: showing first" in result


def test_write_file_non_append_limit_counts_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACT_WEAVE_WRITE_FILE_MAX_BYTES", "4")
    result = write_file_tool.func(
        runtime=object(),
        description="verify byte limit",
        path="/mnt/user-data/outputs/report.txt",
        content="你好",
        append=False,
    )
    assert "write_file content (6 bytes) exceeds the 4-byte single-call limit" in result


def test_search_result_formatting_is_frozen() -> None:
    match = GrepMatch(
        path="/root/a.txt",
        line_number=2,
        line="needle",
    )
    assert _format_glob_results("/root", [], False) == "No files matched under /root"
    assert _format_glob_results("/root", ["/root/a.txt"], True) == ("Found 1 paths under /root (showing first 1)\n1. /root/a.txt\nResults truncated. Narrow the path or pattern to see fewer matches.")
    assert _format_grep_results("/root", [], False) == ("No matches found under /root")
    assert _format_grep_results("/root", [], True) == ("Results truncated while searching under /root; matches may exist beyond the provider scan budget. Narrow the path or add a glob filter.")
    assert _format_grep_results("/root", [match], True) == ("Found 1 matches under /root (showing first 1)\n/root/a.txt:2: needle\nResults truncated. Narrow the path or add a glob filter.")


def test_search_tool_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module(
        "deerflow.sandbox.tooling.search_tools",
    )
    names = (
        "_get_tool_config_int",
        "_clamp_max_results",
        "_resolve_max_results",
        "_format_glob_results",
        "_format_grep_results",
        "glob_tool",
        "_glob_tool_async",
        "grep_tool",
        "_grep_tool_async",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owner, name)
    for name in ("glob_tool", "grep_tool"):
        assert getattr(legacy, name).func is getattr(owner, name).func
        assert getattr(legacy, name).coroutine is getattr(owner, name).coroutine


def test_file_tool_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.sandbox.tooling.files")
    names = (
        "_truncate_write_file_error_detail",
        "_format_write_file_error",
        "_truncate_read_file_output",
        "_truncate_ls_output",
        "ls_tool",
        "_ls_tool_async",
        "read_current_file_content",
        "read_file_tool",
        "_read_file_tool_async",
        "_effective_write_file_max_bytes",
        "write_file_tool",
        "_write_file_tool_async",
        "str_replace_tool",
        "_str_replace_tool_async",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owner, name)
    for name in (
        "ls_tool",
        "read_file_tool",
        "write_file_tool",
        "str_replace_tool",
    ):
        assert getattr(legacy, name).func is getattr(owner, name).func
        assert getattr(legacy, name).coroutine is getattr(owner, name).coroutine


def test_path_mapping_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module(
        "deerflow.sandbox.tooling.path_mapping",
    )
    names = (
        "_get_skills_container_path",
        "_get_skills_host_path",
        "_is_skills_path",
        "_extract_skill_name_from_skills_path",
        "_is_disabled_skill_path",
        "_is_trusted_run_scoped_skill_path",
        "_resolve_skills_path",
        "_is_acp_workspace_path",
        "_get_custom_mounts",
        "_is_custom_mount_path",
        "_get_custom_mount_for_path",
        "_extract_thread_id_from_thread_data",
        "_get_acp_workspace_host_path",
        "_resolve_acp_workspace_path",
        "_resolve_local_read_path",
        "_path_variants",
        "_path_separator_for_style",
        "_join_path_preserving_style",
        "replace_virtual_path",
        "delegated_output_root",
        "resolve_delegated_tool_path",
        "_delegated_result_exposes_hidden_runtime",
        "_thread_virtual_to_actual_mappings",
        "_thread_actual_to_virtual_mappings",
        "_compiled_mask_patterns",
        "mask_local_paths_in_output",
        "_reject_path_traversal",
        "validate_local_tool_path",
        "_validate_resolved_user_data_path",
        "_resolve_and_validate_user_data_path",
        "resolve_and_validate_user_data_path",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owner, name)
    assert legacy.VIRTUAL_PATH_PREFIX == owner.VIRTUAL_PATH_PREFIX


def test_bash_policy_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module(
        "deerflow.sandbox.tooling.bash_policy",
    )
    names = (
        "_get_mcp_allowed_paths",
        "_is_non_file_url_token",
        "_non_file_url_spans",
        "_is_in_spans",
        "_has_dotdot_path_segment",
        "_split_shell_tokens",
        "_is_shell_command_separator",
        "_is_shell_redirection_operator",
        "_is_shell_assignment",
        "_is_allowed_local_bash_absolute_path",
        "_next_cd_target",
        "_validate_local_bash_cwd_target",
        "_validate_local_bash_root_path_args",
        "_validate_local_bash_shell_tokens",
        "_braces_are_identifier_placeholders_only",
        "_is_non_path_literal_fragment",
        "validate_local_bash_command_paths",
        "replace_virtual_paths_in_command",
        "_apply_cwd_prefix",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owner, name)


def test_bash_tool_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.sandbox.tooling.bash")
    assert legacy.bash_tool is owner.bash_tool
    assert legacy._bash_tool_async is owner._bash_tool_async
    assert legacy.bash_tool.func is owner.bash_tool.func
    assert legacy.bash_tool.coroutine is owner.bash_tool.coroutine


def test_runtime_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.sandbox.tooling.runtime")
    names = (
        "_sanitize_error",
        "get_thread_data",
        "is_local_sandbox",
        "sandbox_from_runtime",
        "ensure_sandbox_initialized",
        "ensure_sandbox_initialized_async",
        "_run_sync_tool_after_async_sandbox_init",
        "_RuntimeContextOverlay",
        "ensure_thread_directories_exist",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owner, name)


def test_host_execution_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module(
        "deerflow.sandbox.tooling.host_execution",
    )
    exact_names = (
        "CHANNEL_USER_ID_ENV",
        "mask_secret_values",
        "_truncate_bash_output",
        "_is_windows",
        "_channel_identity_state",
        "_channel_identity_prefix_from_state",
        "_channel_identity_prefix",
        "_github_env_from_runtime",
        "_runtime_app_config",
        "_runtime_host_bash_execution_mode",
        "_host_execution_agent_path",
        "_host_execution_environment_keys",
        "_host_execution_skill_secret_sources",
        "_host_execution_legacy_environment_keys",
        "_approval_scan_secrets",
        "_command_contains_secret",
        "_prepare_local_host_execution",
        "_approval_required_bash",
    )
    for name in exact_names:
        assert getattr(legacy, name) is getattr(owner, name)
    assert owner.prepare_local_host_execution is owner._prepare_local_host_execution
    assert owner.truncate_bash_output is owner._truncate_bash_output


EXPECTED_LEGACY_PRODUCTION_CONSUMERS = frozenset()


def _imports_legacy_tools(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == LEGACY_MODULE:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "deerflow.sandbox" and any(alias.name == "tools" for alias in node.names):
            return True
        if isinstance(node, ast.Import) and any(alias.name == LEGACY_MODULE for alias in node.names):
            return True
    return False


def _legacy_production_consumers() -> frozenset[str]:
    legacy_path = HARNESS_ROOT / "deerflow" / "sandbox" / "tools.py"
    return frozenset(path.relative_to(HARNESS_ROOT).as_posix() for path in HARNESS_ROOT.rglob("*.py") if path != legacy_path and _imports_legacy_tools(path))


def test_batch4_production_consumer_inventory_is_frozen() -> None:
    assert _legacy_production_consumers() == EXPECTED_LEGACY_PRODUCTION_CONSUMERS


def test_host_execution_runner_imports_public_owner_apis() -> None:
    runner_path = HARNESS_ROOT / "deerflow" / "runtime" / "host_execution_runner.py"
    tree = ast.parse(runner_path.read_text(encoding="utf-8"), filename=str(runner_path))
    imports = {(node.module, alias.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}

    assert {
        ("deerflow.sandbox.tooling.host_execution", "mask_secret_values"),
        (
            "deerflow.sandbox.tooling.host_execution",
            "prepare_local_host_execution",
        ),
        ("deerflow.sandbox.tooling.host_execution", "truncate_bash_output"),
        (
            "deerflow.sandbox.tooling.path_mapping",
            "mask_local_paths_in_output",
        ),
    } <= imports
    assert not any(module == LEGACY_MODULE for module, _name in imports)


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def test_sandbox_tools_facade_is_imports_only_and_tools_are_created_once() -> None:
    facade_path = HARNESS_ROOT / "deerflow" / "sandbox" / "tools.py"
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )
    assert all(isinstance(node, (ast.Import, ast.ImportFrom)) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)) for node in facade_tree.body)
    tool_decorators = [
        decorator
        for path in TOOLING_ROOT.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "tool"
    ]
    assert len(tool_decorators) == 7
    assert _legacy_production_consumers() == frozenset()


def test_tooling_has_no_facade_app_or_database_imports() -> None:
    imports = set().union(*(_absolute_imports(path) for path in TOOLING_ROOT.glob("*.py")))
    forbidden = {name for name in imports if name == "deerflow.sandbox.tools" or name == "app" or name.startswith("app.") or name == "sqlalchemy" or name.startswith("sqlalchemy.")}
    assert forbidden == set()


def test_tooling_and_legacy_import_cleanly_in_both_orders() -> None:
    owner_first = dedent(
        """
        from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
        from deerflow.runtime import host_execution_runner as runner
        from deerflow.sandbox import tools as legacy
        assert legacy.bash_tool is bash.bash_tool
        assert legacy.ls_tool is files.ls_tool
        assert legacy.read_file_tool is files.read_file_tool
        assert legacy.write_file_tool is files.write_file_tool
        assert legacy.str_replace_tool is files.str_replace_tool
        assert legacy.glob_tool is search_tools.glob_tool
        assert legacy.grep_tool is search_tools.grep_tool
        assert runner.prepare_local_host_execution is host_execution.prepare_local_host_execution
        assert runner.mask_local_paths_in_output is path_mapping.mask_local_paths_in_output
        """
    )
    legacy_first = dedent(
        """
        from deerflow.sandbox import tools as legacy
        from deerflow.runtime import host_execution_runner as runner
        from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
        assert legacy.bash_tool is bash.bash_tool
        assert legacy.ls_tool is files.ls_tool
        assert legacy.read_file_tool is files.read_file_tool
        assert legacy.write_file_tool is files.write_file_tool
        assert legacy.str_replace_tool is files.str_replace_tool
        assert legacy.glob_tool is search_tools.glob_tool
        assert legacy.grep_tool is search_tools.grep_tool
        assert runner.truncate_bash_output is host_execution.truncate_bash_output
        """
    )
    for source in (owner_first, legacy_first):
        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=BACKEND_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
