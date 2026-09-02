import os

from langchain.tools import tool

from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.exceptions import SandboxError
from deerflow.sandbox.file_operation_lock import get_file_operation_lock
from deerflow.sandbox.tooling.path_mapping import (
    _delegated_result_exposes_hidden_runtime,
    _extract_skill_name_from_skills_path,
    _extract_thread_id_from_thread_data,
    _is_acp_workspace_path,
    _is_custom_mount_path,
    _is_disabled_skill_path,
    _is_skills_path,
    _is_trusted_run_scoped_skill_path,
    _resolve_acp_workspace_path,
    _resolve_and_validate_user_data_path,
    _resolve_skills_path,
    delegated_output_root,
    mask_local_paths_in_output,
    resolve_delegated_tool_path,
    validate_local_tool_path,
)
from deerflow.sandbox.tooling.runtime import (
    _run_sync_tool_after_async_sandbox_init,
    _sanitize_error,
    ensure_sandbox_initialized,
    ensure_thread_directories_exist,
    get_thread_data,
    is_local_sandbox,
)
from deerflow.tools.types import Runtime

__all__ = [
    "ls_tool",
    "read_current_file_content",
    "read_file_tool",
    "str_replace_tool",
    "write_file_tool",
]

_DEFAULT_WRITE_FILE_ERROR_MAX_CHARS = 2000

# Maximum bytes accepted in a single non-append write_file call (issue #3189).
# Oversized single-shot writes correlate with LLM streaming chunk-gap timeouts
# because the tool-call JSON payload (which the model must emit as one
# continuous stream) grows past the safe window. 80 KB ≈ 20K tokens, a
# comfortable headroom under the factory-default 240s stream_chunk_timeout.
# Deployments can override via env var ACT_WEAVE_WRITE_FILE_MAX_BYTES; set to
# 0 (or negative) to disable the guard entirely.
_WRITE_FILE_CONTENT_MAX_BYTES = 80 * 1024
_WRITE_FILE_MAX_BYTES_ENV = "ACT_WEAVE_WRITE_FILE_MAX_BYTES"


def _truncate_write_file_error_detail(detail: str, max_chars: int) -> str:
    """Middle-truncate write_file error details, preserving the head and tail."""
    if max_chars == 0:
        return detail
    if len(detail) <= max_chars:
        return detail
    total = len(detail)
    marker_max_len = len(f"\n... [write_file error truncated: {total} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return detail[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total - kept
    marker = f"\n... [write_file error truncated: {skipped} chars skipped] ...\n"
    return f"{detail[:head_len]}{marker}{detail[-tail_len:] if tail_len > 0 else ''}"


def _format_write_file_error(
    requested_path: str,
    error: Exception,
    runtime: Runtime | None = None,
    *,
    max_chars: int = _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
) -> str:
    """Return a bounded, sanitized error string for write_file failures."""
    header = f"Error: Failed to write file '{requested_path}'"
    detail = _sanitize_error(error, runtime)
    if max_chars == 0:
        return f"{header}: {detail}"
    detail_budget = max_chars - len(header) - 2
    if detail_budget <= 0:
        return _truncate_write_file_error_detail(f"{header}: {detail}", max_chars)
    return f"{header}: {_truncate_write_file_error_detail(detail, detail_budget)}"


def _truncate_read_file_output(output: str, max_chars: int) -> str:
    """Head-truncate read_file output, preserving the beginning of the file.

    Source code and documents are read top-to-bottom; the head contains the
    most context (imports, class definitions, function signatures).

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total = len(output)
    # Compute the exact worst-case marker length: both numeric fields are at
    # their maximum (total chars), so this is a tight upper bound.
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. Use start_line/end_line to read a specific range] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. Use start_line/end_line to read a specific range] ..."
    return f"{output[:kept]}{marker}"


def _truncate_ls_output(output: str, max_chars: int) -> str:
    """Head-truncate ls output, preserving the beginning of the listing.

    Directory listings are read top-to-bottom; the head shows the most
    relevant structure.

    The returned string (including the truncation marker) is guaranteed to be
    no longer than max_chars characters. Pass max_chars=0 to disable truncation
    and return the full output unchanged.
    """
    if max_chars == 0:
        return output
    if len(output) <= max_chars:
        return output
    total = len(output)
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. Use a more specific path to see fewer results] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. Use a more specific path to see fewer results] ..."
    return f"{output[:kept]}{marker}"


@tool("ls", parse_docstring=True)
def ls_tool(runtime: Runtime, description: str, path: str) -> str:
    """List the contents of a directory up to 2 levels deep in tree format.

    Args:
        description: Explain why you are listing this directory in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the directory to list.
    """
    requested_path = path
    try:
        path = resolve_delegated_tool_path(runtime, path)
        # Block access to disabled skill directories
        if not _is_trusted_run_scoped_skill_path(runtime, path) and _is_disabled_skill_path(path, user_id=resolve_runtime_user_id(runtime)):
            skill_name = _extract_skill_name_from_skills_path(path) or "unknown"
            return f"Error: Skill '{skill_name}' is disabled. Access to its files is blocked. Enable the skill in settings before using it."
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        thread_data = get_thread_data(runtime) if is_local_sandbox(runtime) or delegated_output_root(runtime) is not None else None
        if is_local_sandbox(runtime):
            validate_local_tool_path(path, thread_data, read_only=True)
            if _is_skills_path(path) or _is_acp_workspace_path(path):
                # Skills and ACP workspace paths are resolved by the sandbox's
                # PathMapping (which uses the user_id from acquire time), not
                # by _resolve_skills_path / _resolve_acp_workspace_path (which
                # use get_effective_user_id() from contextvar and may differ
                # from the sandbox mapping's user_id).
                pass
            elif not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths and skills/ACP paths are resolved by LocalSandbox._resolve_path()
        children = sandbox.list_dir(path)
        if not children:
            return "(empty)"
        if thread_data is not None:
            children = [mask_local_paths_in_output(child, thread_data) for child in children]
        children = [child for child in children if not _delegated_result_exposes_hidden_runtime(runtime, child)]
        if not children:
            return "(empty)"
        output = "\n".join(children)
        try:
            from deerflow.config.app_config import get_app_config

            sandbox_cfg = get_app_config().sandbox
            max_chars = sandbox_cfg.ls_output_max_chars if sandbox_cfg else 20000
        except Exception:
            max_chars = 20000
        return _truncate_ls_output(output, max_chars)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error listing directory: {_sanitize_error(e, runtime)}"


async def _ls_tool_async(runtime: Runtime, description: str, path: str) -> str:
    return await _run_sync_tool_after_async_sandbox_init(ls_tool.func, runtime, description, path)


ls_tool.coroutine = _ls_tool_async


def read_current_file_content(runtime: Runtime | None, path: str) -> str:
    """Read the full current content of ``path`` using read_file's resolution rules.

    Shared by ``read_file_tool`` and ``ReadBeforeWriteMiddleware`` (issue #3857)
    so the gate hashes exactly the bytes the read tool would see. Raises
    ``FileNotFoundError`` when the file does not exist; other sandbox errors
    propagate to the caller.
    """
    path = resolve_delegated_tool_path(runtime, path)
    sandbox = ensure_sandbox_initialized(runtime)
    ensure_thread_directories_exist(runtime)
    if is_local_sandbox(runtime):
        thread_data = get_thread_data(runtime)
        validate_local_tool_path(path, thread_data, read_only=True)
        if _is_skills_path(path):
            if not _is_trusted_run_scoped_skill_path(runtime, path):
                path = _resolve_skills_path(path)
        elif _is_acp_workspace_path(path):
            path = _resolve_acp_workspace_path(path, _extract_thread_id_from_thread_data(thread_data))
        elif not _is_custom_mount_path(path):
            path = _resolve_and_validate_user_data_path(path, thread_data)
        # Custom mount paths are resolved by LocalSandbox._resolve_path()
    return sandbox.read_file(path)


@tool("read_file", parse_docstring=True)
def read_file_tool(
    runtime: Runtime,
    description: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read the contents of a text file. Use this to examine source code, configuration files, logs, or any text-based file.

    Args:
        description: Explain why you are reading this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to read.
        start_line: Optional starting line number (1-indexed, inclusive). Use with end_line to read a specific range.
        end_line: Optional ending line number (1-indexed, inclusive). Use with start_line to read a specific range.
    """
    try:
        # Block access to disabled skill files
        if not _is_trusted_run_scoped_skill_path(runtime, path) and _is_disabled_skill_path(path, user_id=resolve_runtime_user_id(runtime)):
            skill_name = _extract_skill_name_from_skills_path(path) or "unknown"
            return f"Error: Skill '{skill_name}' is disabled. Access to its files is blocked. Enable the skill in settings before using it."
        requested_path = path
        content = read_current_file_content(runtime, path)
        if not content:
            return "(empty)"
        if start_line is not None or end_line is not None:
            lines = content.splitlines()
            start = max(start_line, 1) if start_line is not None else 1
            end = end_line if end_line is not None else len(lines)
            if end < 1:
                return "(end_line must be >= 1)"
            if start > len(lines):
                return "(start_line exceeds file length)"
            if start > end:
                return "(start_line > end_line — no lines in range)"
            content = "\n".join(lines[start - 1 : end])
        try:
            from deerflow.config.app_config import get_app_config

            sandbox_cfg = get_app_config().sandbox
            max_chars = sandbox_cfg.read_file_output_max_chars if sandbox_cfg else 50000
        except Exception:
            max_chars = 50000
        return _truncate_read_file_output(content, max_chars)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied reading file: {requested_path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {requested_path}"
    except UnicodeDecodeError:
        return (
            f"Error: cannot read '{requested_path}' as text — it appears to be a binary file "
            "(e.g. .xlsx, .pdf, or an image). read_file only supports UTF-8 text. Use bash with a "
            "suitable library instead (pandas/openpyxl for spreadsheets), or view_image for images."
        )
    except Exception as e:
        return f"Error: Unexpected error reading file: {_sanitize_error(e, runtime)}"


async def _read_file_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(read_file_tool.func, runtime, description, path, start_line, end_line)


read_file_tool.coroutine = _read_file_tool_async


def _effective_write_file_max_bytes() -> int:
    """Return the active size cap for non-append write_file calls.

    Reads ``ACT_WEAVE_WRITE_FILE_MAX_BYTES`` at call time (not import time)
    so tests and runtime tweaks take effect without restart. Falls back to
    the default on missing/malformed values. A non-positive value disables
    the guard.
    """
    raw = os.environ.get(_WRITE_FILE_MAX_BYTES_ENV)
    if raw is None:
        return _WRITE_FILE_CONTENT_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return _WRITE_FILE_CONTENT_MAX_BYTES


@tool("write_file", parse_docstring=True)
def write_file_tool(
    runtime: Runtime,
    description: str,
    path: str,
    content: str,
    append: bool = False,
) -> str:
    """Write text content to a file. By default this overwrites the target file; set append=True to add content to the end without replacing existing content.

    READ-BEFORE-WRITE (issue #3857): if the target file already exists (including
    append=True), you must have read its CURRENT version with read_file first.
    Any write invalidates earlier reads, so re-read between consecutive
    modifications — a ranged read of the relevant section is enough. Writes
    that fail this check are rejected with an error.

    FINAL DELIVERABLES:
    If the user asks you to create or write a file, including a source-code file,
    script, configuration, or document, write the completed file under
    `/mnt/user-data/outputs`. Use `/mnt/user-data/workspace` only for temporary or
    intermediate files. Follow the current Agent's system instructions for the
    role-specific handoff after writing; writing a file alone does not publish it.

    SIZE POLICY (issue #3189):
    A single non-append write_file call must not exceed 80 KB of UTF-8 content.
    Oversized single-shot writes correlate with LLM streaming chunk-gap
    timeouts because the tool-call JSON payload — which the model must emit as
    one continuous stream — grows past the safe window. For larger documents,
    use ONE of these strategies (write_file rejects oversized payloads with an
    actionable error):

      1. INCREMENTAL EDIT (preferred for revisions): after the initial write,
         use `str_replace` to surgically update sections. This is the same
         pattern Claude Code's Write+Edit and OpenAI Codex's apply_patch use,
         and keeps each tool call's payload small.
      2. APPEND-IN-CHUNKS (for new long-form content): split the document into
         sections, each well under 80 KB. First call uses append=False to
         create the file; subsequent calls use append=True. The 80 KB cap does
         NOT apply to append=True calls.

    Operators can override the cap via env var `ACT_WEAVE_WRITE_FILE_MAX_BYTES`
    (0 disables the guard entirely). Raising it risks streaming timeouts.

    Args:
        description: Explain why you are writing to this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to write to. ALWAYS PROVIDE THIS PARAMETER SECOND.
        content: The content to write to the file. ALWAYS PROVIDE THIS PARAMETER THIRD.
        append: Whether to append content to the end of the file instead of overwriting it. Defaults to False.
    """
    if not append:
        max_bytes = _effective_write_file_max_bytes()
        if max_bytes > 0:
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > max_bytes:
                return (
                    f"Error: write_file content ({content_bytes} bytes) exceeds the "
                    f"{max_bytes}-byte single-call limit. Split the content into smaller "
                    "pieces: either (a) write the first section now, then use `str_replace` "
                    "for further edits, or (b) call write_file again with append=True "
                    "carrying the next section. See SIZE POLICY in the tool docstring "
                    "or issue #3189 for the rationale."
                )
    try:
        requested_path = path
        path = resolve_delegated_tool_path(runtime, path)
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            if not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()
        with get_file_operation_lock(sandbox, path):
            sandbox.write_file(path, content, append)
        return "OK"
    except SandboxError as e:
        return _format_write_file_error(requested_path, e, runtime)
    except PermissionError:
        return _truncate_write_file_error_detail(
            f"Error: Permission denied writing to file: {requested_path}",
            _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
        )
    except IsADirectoryError:
        return _truncate_write_file_error_detail(
            f"Error: Path is a directory, not a file: {requested_path}",
            _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS,
        )
    except OSError as e:
        return _format_write_file_error(requested_path, e, runtime)
    except Exception as e:
        return _format_write_file_error(requested_path, e, runtime)


async def _write_file_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    content: str,
    append: bool = False,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        write_file_tool.func,
        runtime,
        description,
        path,
        content,
        append,
        authorization_operation="before_sandbox_write",
    )


write_file_tool.coroutine = _write_file_tool_async


@tool("str_replace", parse_docstring=True)
def str_replace_tool(
    runtime: Runtime,
    description: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    """Replace a substring in a file with another substring.
    If `replace_all` is False (default), the substring to replace must appear **exactly once** in the file.

    READ-BEFORE-WRITE (issue #3857): you must have read the file's CURRENT
    version with read_file first; any write invalidates earlier reads.

    Args:
        description: Explain why you are replacing the substring in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to replace the substring in. ALWAYS PROVIDE THIS PARAMETER SECOND.
        old_str: The substring to replace. ALWAYS PROVIDE THIS PARAMETER THIRD.
        new_str: The new substring. ALWAYS PROVIDE THIS PARAMETER FOURTH.
        replace_all: Whether to replace all occurrences of the substring. If False, only the first occurrence will be replaced. Default is False.
    """
    requested_path = path
    try:
        path = resolve_delegated_tool_path(runtime, path)
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            if not _is_custom_mount_path(path):
                path = _resolve_and_validate_user_data_path(path, thread_data)
            # Custom mount paths are resolved by LocalSandbox._resolve_path()
        with get_file_operation_lock(sandbox, path):
            content = sandbox.read_file(path)
            if not old_str:
                return "OK"
            if not content or old_str not in content:
                return f"Error: String to replace not found in file: {requested_path}"
            if replace_all:
                content = content.replace(old_str, new_str)
            else:
                content = content.replace(old_str, new_str, 1)
            sandbox.write_file(path, content)
        return "OK"
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied accessing file: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error replacing string: {_sanitize_error(e, runtime)}"


async def _str_replace_tool_async(
    runtime: Runtime,
    description: str,
    path: str,
    old_str: str,
    new_str: str,
    replace_all: bool = False,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        str_replace_tool.func,
        runtime,
        description,
        path,
        old_str,
        new_str,
        replace_all,
        authorization_operation="before_sandbox_write",
    )


str_replace_tool.coroutine = _str_replace_tool_async
