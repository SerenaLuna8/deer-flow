from __future__ import annotations

import ast
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS = (
    _BACKEND_ROOT / "app",
    _BACKEND_ROOT / "packages" / "harness" / "deerflow",
)


def _python_sources() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple((path, ast.parse(path.read_text(encoding="utf-8"))) for root in _PRODUCTION_ROOTS for path in root.rglob("*.py"))


def _qualified_owner(tree: ast.Module, target: ast.AST) -> str:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names: list[str] = []
    node = target
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return ".".join(reversed(names))


def _is_select_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id == "select" or isinstance(node.func, ast.Attribute) and node.func.attr == "select")


def test_run_asset_snapshot_reads_have_a_closed_explicit_column_allowlist() -> None:
    whole_entity_reads: list[str] = []
    implicit_repository_calls: list[str] = []
    snapshot_json_loaders: set[str] = set()

    for path, tree in _python_sources():
        relative = path.relative_to(_BACKEND_ROOT).as_posix()
        for node in ast.walk(tree):
            if _is_select_call(node):
                assert isinstance(node, ast.Call)
                if any(isinstance(argument, ast.Name) and argument.id == "RunAssetVersionRow" for argument in node.args):
                    whole_entity_reads.append(
                        f"{relative}:{_qualified_owner(tree, node)}",
                    )
                if any(isinstance(descendant, ast.Attribute) and descendant.attr == "snapshot_json" for argument in node.args for descendant in ast.walk(argument)):
                    snapshot_json_loaders.add(
                        f"{relative}:{_qualified_owner(tree, node)}",
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "list_assets_in_session":
                implicit_repository_calls.append(
                    f"{relative}:{_qualified_owner(tree, node)}",
                )

    assert whole_entity_reads == []
    assert implicit_repository_calls == []
    assert snapshot_json_loaders == {
        "app/private_work/asset_runtime.py:PrivateAssetRuntime._pinned_skill_plan",
        "app/private_work/asset_runtime.py:PrivateAssetRuntime._small_snapshot",
        "app/private_work/run_skill_tree_materializer.py:LegacyInlineRunSkillSourceAdapter._read_exact_snapshot",
        "app/private_work/run_skill_tree_materializer.py:PinnedSkillVersionSourceAdapter._assert_exact_metadata",
    }
