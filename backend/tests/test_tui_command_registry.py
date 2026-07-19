"""Tests for the slash-command registry (pure)."""

from deerflow.tui.command_registry import (
    BUILTIN_COMMANDS,
    build_registry,
    filter_commands,
    resolve,
)


def test_build_registry_includes_all_builtins():
    registry = build_registry()
    names = {c.name for c in registry}
    for builtin in BUILTIN_COMMANDS:
        assert builtin.name in names


def test_build_registry_excludes_removed_global_commands():
    names = {command.name for command in build_registry()}
    assert names.isdisjoint({"skills", "mcp", "memory"})


def test_filter_empty_query_returns_all():
    registry = build_registry()
    assert filter_commands(registry, "") == registry


def test_filter_matches_name_substring_case_insensitive():
    registry = build_registry()
    results = filter_commands(registry, "MOD")
    assert any(c.name == "model" for c in results)


def test_filter_matches_description():
    registry = build_registry()
    results = filter_commands(registry, "keybindings")
    assert any(c.name == "help" for c in results)


def test_filter_ranks_prefix_matches_before_substring():
    registry = build_registry()
    results = filter_commands(registry, "re")
    # "resume" (prefix) should rank above "threads" (substring).
    names = [c.name for c in results]
    assert "resume" in names
    assert "threads" in names
    assert names.index("resume") < names.index("threads")


def test_resolve_plain_text_is_message():
    res = resolve("hello there")
    assert res.kind == "message"
    assert res.text == "hello there"


def test_resolve_builtin_command():
    res = resolve("/model")
    assert res.kind == "builtin"
    assert res.name == "model"


def test_resolve_builtin_with_args():
    res = resolve("/resume thread-123")
    assert res.kind == "builtin"
    assert res.name == "resume"
    assert res.args == "thread-123"


def test_resolve_removed_dynamic_skill_activation_is_unknown():
    res = resolve("/tdd write the test first")
    assert res.kind == "unknown"
    assert res.name == "tdd"


def test_resolve_unknown_command():
    res = resolve("/definitely-not-a-command")
    assert res.kind == "unknown"
    assert res.name == "definitely-not-a-command"


def test_resolve_bare_slash_is_unknown_empty():
    res = resolve("/")
    assert res.kind == "unknown"


def test_goal_is_builtin_command():
    resolved = resolve("/goal finish the implementation")

    assert resolved.kind == "builtin"
    assert resolved.name == "goal"
    assert resolved.args == "finish the implementation"


def test_goal_occurs_once_in_builtin_registry():
    registry = build_registry()

    assert [command.name for command in registry].count("goal") == 1
