import inspect
from types import SimpleNamespace
from unittest.mock import patch

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount
from deerflow.sandbox.search import GrepMatch, find_glob_matches, find_grep_matches
from deerflow.sandbox.tools import (
    _format_grep_results,
    glob_tool,
    grep_tool,
    ls_tool,
)


def _make_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    workspace.mkdir()
    uploads.mkdir()
    outputs.mkdir()
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local"},
            "thread_data": {
                "workspace_path": str(workspace),
                "uploads_path": str(uploads),
                "outputs_path": str(outputs),
            },
        },
        context={"thread_id": "thread-1"},
    )


def test_glob_tool_returns_virtual_paths_and_ignores_common_dirs(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "util.py").write_text("print('util')\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "skip.py").write_text("ignored\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = glob_tool.func(
        runtime=runtime,
        description="find python files",
        pattern="**/*.py",
        path="/mnt/user-data/workspace",
    )

    assert "/mnt/user-data/workspace/app.py" in result
    assert "/mnt/user-data/workspace/pkg/util.py" in result
    assert "node_modules" not in result
    assert str(workspace) not in result


def test_glob_tool_supports_skills_virtual_paths(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    skills_dir = tmp_path / "skills"
    (skills_dir / "public" / "demo").mkdir(parents=True)
    (skills_dir / "public" / "demo" / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    sandbox = LocalSandbox(
        id="local",
        path_mappings=[
            PathMapping(container_path="/mnt/skills", local_path=str(skills_dir), read_only=True),
        ],
    )
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: sandbox)

    result = glob_tool.func(
        runtime=runtime,
        description="find skills",
        pattern="**/SKILL.md",
        path="/mnt/skills",
    )

    assert "/mnt/skills/public/demo/SKILL.md" in result
    assert str(skills_dir) not in result


def test_grep_tool_filters_by_glob_and_skips_binary_files(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "main.py").write_text("TODO = 'ship it'\nprint(TODO)\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("TODO in txt should be filtered\n", encoding="utf-8")
    (workspace / "image.bin").write_bytes(b"\0binary TODO")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="find todo references",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        glob="**/*.py",
    )

    assert "/mnt/user-data/workspace/main.py:1: TODO = 'ship it'" in result
    assert "notes.txt" not in result
    assert "image.bin" not in result
    assert str(workspace) not in result


def test_grep_tool_accepts_single_file_path(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    uploads = tmp_path / "uploads"
    report = uploads / "report.md"
    report.write_text("Revenue grew 20%\n", encoding="utf-8")
    monkeypatch.setattr(
        "deerflow.sandbox.tools.ensure_sandbox_initialized",
        lambda runtime: LocalSandbox(id="local"),
    )

    result = grep_tool.func(
        runtime=runtime,
        description="find revenue in the uploaded report",
        pattern="Revenue",
        path="/mnt/user-data/uploads/report.md",
    )

    assert "/mnt/user-data/uploads/report.md:1: Revenue grew 20%" in result
    assert "Path is not a directory" not in result
    assert str(uploads) not in result


def test_grep_model_contract_describes_file_or_directory_path() -> None:
    assert "text file or files under a directory" in inspect.getdoc(Sandbox.grep)
    assert "text file or files under a directory" in grep_tool.description
    path_description = grep_tool.args_schema.model_fields["path"].description
    assert path_description is not None
    assert "file or root directory" in path_description


def test_grep_tool_uses_generic_path_error_for_missing_file(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _make_runtime(tmp_path)
    monkeypatch.setattr(
        "deerflow.sandbox.tools.ensure_sandbox_initialized",
        lambda runtime: LocalSandbox(id="local"),
    )

    result = grep_tool.func(
        runtime=runtime,
        description="search missing file",
        pattern="TODO",
        path="/mnt/user-data/workspace/missing.txt",
    )

    assert result == "Error: Path not found: /mnt/user-data/workspace/missing.txt"


def test_grep_tool_truncates_results(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "main.py").write_text("TODO one\nTODO two\nTODO three\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    # Prevent config.yaml tool config from overriding the caller-supplied max_results=2.
    monkeypatch.setattr("deerflow.sandbox.tools.get_app_config", lambda: SimpleNamespace(get_tool_config=lambda name: None))

    result = grep_tool.func(
        runtime=runtime,
        description="limit matches",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        max_results=2,
    )

    assert "Found 2 matches under /mnt/user-data/workspace (showing first 2)" in result
    assert "TODO one" in result
    assert "TODO two" in result
    assert "TODO three" not in result
    assert "Results truncated." in result


def test_glob_tool_include_dirs_filters_nested_ignored_paths(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("x\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "lib").mkdir()

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = glob_tool.func(
        runtime=runtime,
        description="find dirs",
        pattern="**",
        path="/mnt/user-data/workspace",
        include_dirs=True,
    )

    assert "src" in result
    assert "node_modules" not in result


def test_grep_tool_literal_mode(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "file.py").write_text("price = (a+b)\nresult = a+b\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    # literal=True should treat (a+b) as a plain string, not a regex group
    result = grep_tool.func(
        runtime=runtime,
        description="literal search",
        pattern="(a+b)",
        path="/mnt/user-data/workspace",
        literal=True,
    )

    assert "price = (a+b)" in result
    assert "result = a+b" not in result


def test_grep_tool_case_sensitive(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "file.py").write_text("TODO: fix\ntodo: also fix\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="case sensitive search",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        case_sensitive=True,
    )

    assert "TODO: fix" in result
    assert "todo: also fix" not in result


def test_grep_tool_invalid_regex_returns_error(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="bad pattern",
        pattern="[invalid",
        path="/mnt/user-data/workspace",
    )

    assert "Invalid regex pattern" in result


def test_aio_sandbox_glob_include_dirs_filters_nested_ignored(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(name="src", path="/mnt/workspace/src"),
                    SimpleNamespace(name="node_modules", path="/mnt/workspace/node_modules"),
                    # child of node_modules — should be filtered via should_ignore_path
                    SimpleNamespace(name="lib", path="/mnt/workspace/node_modules/lib"),
                ]
            )
        ),
    )

    matches, truncated = sandbox.glob("/mnt/workspace", "**", include_dirs=True)

    assert "/mnt/workspace/src" in matches
    assert "/mnt/workspace/node_modules" not in matches
    assert "/mnt/workspace/node_modules/lib" not in matches
    assert truncated is False


def test_aio_sandbox_grep_invalid_regex_raises() -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")

    import re

    try:
        sandbox.grep("/mnt/workspace", "[invalid")
        assert False, "Expected re.error"
    except re.error:
        pass


def test_aio_sandbox_glob_parses_json(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "find_files",
        lambda **kwargs: SimpleNamespace(data=SimpleNamespace(files=["/mnt/user-data/workspace/app.py", "/mnt/user-data/workspace/node_modules/skip.py"])),
    )

    matches, truncated = sandbox.glob("/mnt/user-data/workspace", "**/*.py")

    assert matches == ["/mnt/user-data/workspace/app.py"]
    assert truncated is False


def test_aio_sandbox_grep_parses_json(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "grep_files",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                matches=[
                    SimpleNamespace(
                        file="/mnt/user-data/workspace/app.py",
                        line_number=7,
                        line_content="TODO = True",
                    )
                ],
                truncated=False,
            )
        ),
    )

    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "TODO")

    assert matches == [GrepMatch(path="/mnt/user-data/workspace/app.py", line_number=7, line="TODO = True")]
    assert truncated is False


def test_aio_sandbox_grep_accepts_single_file_path(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(
            id="test-sandbox",
            base_url="http://localhost:8080",
        )
    monkeypatch.setattr(
        sandbox._client.file,
        "grep_files",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                matches=[
                    SimpleNamespace(
                        file="/mnt/user-data/uploads/report.md",
                        line_number=3,
                        line_content="Revenue grew 20%",
                    )
                ],
                truncated=False,
            )
        ),
    )
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("single-file grep must not list the path as a directory")),
    )

    matches, truncated = sandbox.grep(
        "/mnt/user-data/uploads/report.md",
        "Revenue",
    )

    assert matches == [
        GrepMatch(
            path="/mnt/user-data/uploads/report.md",
            line_number=3,
            line="Revenue grew 20%",
        )
    ]
    assert truncated is False


def test_aio_sandbox_grep_pages_past_filtered_provider_results(
    monkeypatch,
) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(
            id="test-sandbox",
            base_url="http://localhost:8080",
        )

    provider_matches = [
        SimpleNamespace(
            file=f"/mnt/user-data/workspace/node_modules/pkg-{index}/ignored.py",
            line_number=1,
            line_content="TODO ignored",
        )
        for index in range(120)
    ]
    provider_matches.append(
        SimpleNamespace(
            file="/mnt/user-data/workspace/app.py",
            line_number=7,
            line_content="TODO visible",
        )
    )
    offsets: list[int] = []

    def grep_files(**kwargs):
        offset = kwargs["offset"]
        page_size = kwargs["max_results"]
        offsets.append(offset)
        page = provider_matches[offset : offset + page_size]
        return SimpleNamespace(
            data=SimpleNamespace(
                matches=page,
                truncated=offset + len(page) < len(provider_matches),
            )
        )

    monkeypatch.setattr(sandbox._client.file, "grep_files", grep_files)

    matches, truncated = sandbox.grep(
        "/mnt/user-data/workspace",
        "TODO",
        glob="**/*.py",
        max_results=10,
    )

    assert offsets == [0, 100]
    assert matches == [
        GrepMatch(
            path="/mnt/user-data/workspace/app.py",
            line_number=7,
            line="TODO visible",
        )
    ]
    assert truncated is False


def test_empty_truncated_grep_result_never_claims_no_matches() -> None:
    result = _format_grep_results(
        "/mnt/user-data/workspace",
        [],
        truncated=True,
    )

    assert "No matches found" not in result
    assert "Results truncated" in result
    assert "may exist" in result


def test_find_glob_matches_raises_not_a_directory(tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x\n", encoding="utf-8")

    try:
        find_glob_matches(file_path, "**/*.py")
        assert False, "Expected NotADirectoryError"
    except NotADirectoryError:
        pass


def test_find_grep_matches_accepts_single_file(tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("TODO\n", encoding="utf-8")

    matches, truncated = find_grep_matches(file_path, "TODO")

    assert matches == [
        GrepMatch(
            path=str(file_path),
            line_number=1,
            line="TODO",
        )
    ]
    assert truncated is False


def test_find_grep_matches_skips_symlink_outside_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("TODO outside\n", encoding="utf-8")
    (workspace / "outside-link.txt").symlink_to(outside)

    matches, truncated = find_grep_matches(workspace, "TODO")

    assert matches == []
    assert truncated is False


def test_glob_tool_honors_smaller_requested_max_results(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "a.py").write_text("print('a')\n", encoding="utf-8")
    (workspace / "b.py").write_text("print('b')\n", encoding="utf-8")
    (workspace / "c.py").write_text("print('c')\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    monkeypatch.setattr(
        "deerflow.sandbox.tools.get_app_config",
        lambda: SimpleNamespace(get_tool_config=lambda name: SimpleNamespace(model_extra={"max_results": 50})),
    )

    result = glob_tool.func(
        runtime=runtime,
        description="limit glob matches",
        pattern="**/*.py",
        path="/mnt/user-data/workspace",
        max_results=2,
    )

    assert "Found 2 paths under /mnt/user-data/workspace (showing first 2)" in result
    assert "Results truncated." in result


def test_aio_sandbox_glob_include_dirs_enforces_root_boundary(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(name="src", path="/mnt/workspace/src"),
                    SimpleNamespace(name="src2", path="/mnt/workspace2/src2"),
                ]
            )
        ),
    )

    matches, truncated = sandbox.glob("/mnt/workspace", "**", include_dirs=True)

    assert matches == ["/mnt/workspace/src"]
    assert truncated is False


def test_aio_sandbox_grep_drops_matches_outside_requested_root(
    monkeypatch,
) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "grep_files",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                matches=[
                    SimpleNamespace(
                        file="/mnt/user-data/workspace/app.py",
                        line_number=7,
                        line_content="TODO = True",
                    ),
                    SimpleNamespace(
                        file="/mnt/user-data/workspace-sibling/leak.py",
                        line_number=9,
                        line_content="TODO = False",
                    ),
                ],
                truncated=False,
            )
        ),
    )

    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "TODO")

    assert matches == [GrepMatch(path="/mnt/user-data/workspace/app.py", line_number=7, line="TODO = True")]
    assert truncated is False


# ---------------------------------------------------------------------------
# ls_tool — path masking
# ---------------------------------------------------------------------------


def test_ls_tool_masks_user_data_host_paths(tmp_path, monkeypatch) -> None:
    """ls_tool output must not leak host user-data paths; they should be virtual."""
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "report.txt").write_text("hello\n", encoding="utf-8")
    (workspace / "subdir").mkdir()

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = ls_tool.func(
        runtime=runtime,
        description="list workspace",
        path="/mnt/user-data/workspace",
    )

    # Virtual paths must be present
    assert "/mnt/user-data/workspace" in result
    # Host paths must NOT leak
    assert str(workspace) not in result
    assert str(tmp_path) not in result


def test_ls_tool_masks_skills_host_paths(tmp_path, monkeypatch) -> None:
    """ls_tool output must not leak host skills paths; they should be virtual."""
    runtime = _make_runtime(tmp_path)
    skills_dir = tmp_path / "skills"
    (skills_dir / "public").mkdir(parents=True)
    (skills_dir / "public" / "SKILL.md").write_text("# Skill\n", encoding="utf-8")

    sandbox = LocalSandbox(
        id="local",
        path_mappings=[
            PathMapping(container_path="/mnt/skills", local_path=str(skills_dir), read_only=True),
        ],
    )
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: sandbox)
    mount = RunScopedReadOnlyMount(
        run_id="run-exact",
        container_path="/mnt/skills",
        host_path=str(skills_dir),
    )
    runtime.context.update({"run_id": "run-exact", "__run_read_only_mounts": (mount,)})

    result = ls_tool.func(
        runtime=runtime,
        description="list skills",
        path="/mnt/skills",
    )

    # Virtual paths must be present
    assert "/mnt/skills" in result
    # Host paths must NOT leak
    assert str(skills_dir) not in result
    assert str(tmp_path) not in result


def test_ls_tool_returns_empty_for_empty_directory(tmp_path, monkeypatch) -> None:
    """ls_tool should return '(empty)' for an empty directory."""
    runtime = _make_runtime(tmp_path)

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = ls_tool.func(
        runtime=runtime,
        description="list empty dir",
        path="/mnt/user-data/workspace",
    )

    assert result == "(empty)"


def test_ls_tool_exact_skill_mount_ignores_unrelated_contextvar(tmp_path, monkeypatch) -> None:
    """An exact run mount, not ambient user state, authorizes Skill reads.

    Regression: when the contextvar user_id differs from the sandbox mapping's
    user_id (e.g., contextvar unset → "default", but sandbox uses authenticated
    "user-abc"), _resolve_skills_path would resolve to the wrong directory,
    making /mnt/skills/custom appear empty. The fix delegates resolution to the
    sandbox's PathMapping which always uses the acquire-time user_id.
    """
    from deerflow.runtime.user_context import reset_current_user, set_current_user

    # Create two user-specific custom skill directories:
    # - user-abc: has a skill "my-skill"
    # - default: empty (the fallback when contextvar is unset)
    base_dir = tmp_path / ".deer-flow"
    user_abc_custom = base_dir / "users" / "user-abc" / "skills" / "custom"
    user_abc_custom.mkdir(parents=True)
    (user_abc_custom / "my-skill").mkdir()
    (user_abc_custom / "my-skill" / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")

    default_custom = base_dir / "users" / "default" / "skills" / "custom"
    default_custom.mkdir(parents=True)  # exists but empty

    # The host path resembles an old user bucket, but authority comes only
    # from the server-issued exact run mount below.
    mount = RunScopedReadOnlyMount(
        run_id="run-exact",
        container_path="/mnt/skills/custom",
        host_path=str(user_abc_custom),
    )
    sandbox = LocalSandbox(
        id="local:user-abc:thread-1",
        path_mappings=[
            PathMapping(container_path="/mnt/skills/custom", local_path=str(user_abc_custom), read_only=True),
        ],
    )
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: sandbox)

    # Leave contextvar unset → get_effective_user_id() returns "default"
    # Before the fix, _resolve_skills_path would resolve to default_custom (empty)
    # After the fix, the sandbox PathMapping resolves to user-abc_custom (has my-skill)
    token = set_current_user(SimpleNamespace(id="default"))  # contextvar says "default"
    try:
        runtime = _make_runtime(tmp_path)
        runtime.context.update({"run_id": "run-exact", "__run_read_only_mounts": (mount,)})
        result = ls_tool.func(
            runtime=runtime,
            description="list custom skills",
            path="/mnt/skills/custom",
        )

        # Must show user-abc's skill (sandbox mapping), NOT default's empty dir (contextvar)
        assert "my-skill" in result
        assert str(user_abc_custom) not in result  # host paths must not leak
    finally:
        reset_current_user(token)


def test_ls_tool_filters_upload_staging_files(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    uploads = tmp_path / "uploads"
    (uploads / "report.txt").write_text("ready\n", encoding="utf-8")
    (uploads / ".upload-active.part").write_text("partial\n", encoding="utf-8")
    (uploads / ".upload-note.txt").write_text("intentional\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = ls_tool.func(
        runtime=runtime,
        description="list uploads",
        path="/mnt/user-data/uploads",
    )

    assert "/mnt/user-data/uploads/report.txt" in result
    assert "/mnt/user-data/uploads/.upload-note.txt" in result
    assert ".upload-active.part" not in result
