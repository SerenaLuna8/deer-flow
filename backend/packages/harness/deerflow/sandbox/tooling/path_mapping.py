import logging
import posixpath
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path, PurePosixPath

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH
from deerflow.file_authority import require_private_file_authority
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.path_patterns import build_output_mask_pattern
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount
from deerflow.tools.types import Runtime

__all__ = [
    "VIRTUAL_PATH_PREFIX",
    "delegated_output_root",
    "mask_local_paths_in_output",
    "replace_virtual_path",
    "resolve_and_validate_user_data_path",
    "resolve_delegated_tool_path",
    "validate_local_tool_path",
]

logger = logging.getLogger(__name__)

_DEFAULT_SKILLS_CONTAINER_PATH = DEFAULT_SKILLS_CONTAINER_PATH
_ACP_WORKSPACE_VIRTUAL_PATH = "/mnt/acp-workspace"


def _get_skills_container_path() -> str:
    """Get the skills container path from config, with fallback to default.

    Result is cached after the first successful config load.  If config loading
    fails the default is returned *without* caching so that a later call can
    pick up the real value once the config is available.
    """
    cached = getattr(_get_skills_container_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        from deerflow.config import get_app_config

        value = get_app_config().skills.container_path
        _get_skills_container_path._cached = value  # type: ignore[attr-defined]
        return value
    except Exception:
        return _DEFAULT_SKILLS_CONTAINER_PATH


def _get_skills_host_path() -> str | None:
    """Global host Skill roots are not runtime authority."""
    return None


def _is_skills_path(path: str) -> bool:
    """Check if a path is under the skills container path."""
    skills_prefix = _get_skills_container_path()
    return path == skills_prefix or path.startswith(f"{skills_prefix}/")


def _extract_skill_name_from_skills_path(path: str) -> str | None:
    """Extract a skill name from a virtual skills path.

    /mnt/skills/public/data-analysis/SKILL.md → "data-analysis"
    /mnt/skills/custom/my-skill/SKILL.md → "my-skill"
    /mnt/skills/legacy/my-skill/references/... → "my-skill"
    /mnt/skills/public/data-analysis/ → "data-analysis"
    Returns None if the path doesn't contain a recognizable skill name pattern.
    """
    skills_prefix = _get_skills_container_path()
    if not _is_skills_path(path):
        return None
    # Strip the skills prefix, e.g. "/mnt/skills/"
    relative = path[len(skills_prefix) :].lstrip("/")
    if not relative:
        return None
    # Expected patterns: "public/<name>/...", "custom/<name>/...", "legacy/<name>/..."
    # or "<name>/..." (direct skill access)
    parts = relative.split("/")
    if len(parts) >= 2 and parts[0] in ("public", "custom", "legacy"):
        return parts[1]
    if len(parts) == 1 and parts[0] in ("public", "custom", "legacy"):
        # Category root like /mnt/skills/custom — not a skill path.
        return None
    if len(parts) >= 1:
        # Direct path like /mnt/skills/my-skill/SKILL.md
        return parts[0]
    return None


def _is_disabled_skill_path(path: str, *, user_id: str | None = None) -> bool:
    """Deny every Skill path not covered by a server-issued run mount."""
    return _is_skills_path(path)


def _is_trusted_run_scoped_skill_path(runtime: Runtime | None, path: str) -> bool:
    """Return whether ``path`` belongs to the server-issued exact Skill mount."""

    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        return False
    run_id = context.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return False

    # PrivateRunFileAuthority owns the sandbox lease and its exact mounts.  In
    # this path the detached context tuple is intentionally absent: trusting it
    # would either reject a valid private lease (Skill Builder) or let a stale
    # context claim outlive that lease.  The reserved authority object itself
    # is installed only by RuntimeContextCarrier at the Worker boundary.
    if "__file_authority" in context:
        try:
            authority = require_private_file_authority(
                context,
                method="authorizes_run_read_only_mount_path",
            )
            if authority is None:
                return False
            checker = getattr(
                authority,
                "authorizes_run_read_only_mount_path",
            )
            return checker(run_id=run_id, path=path) is True
        except Exception:  # noqa: BLE001 - fail closed at the file boundary
            logger.debug(
                "Private file authority rejected a run-scoped Skill path",
                exc_info=True,
            )
            return False

    # Compatibility path for private runtimes that acquire a sandbox directly
    # without PrivateRunFileAuthority.  RuntimeContextCarrier strips this
    # reserved tuple from caller context and installs only Worker-issued mounts.
    mounts = context.get("__run_read_only_mounts")
    if not isinstance(mounts, tuple):
        return False
    for mount in mounts:
        if not isinstance(mount, RunScopedReadOnlyMount) or mount.run_id != run_id:
            continue
        prefix = mount.container_path.rstrip("/") or "/"
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return False


def _resolve_skills_path(path: str) -> str:
    """Reject attempts to resolve a global Skill root."""
    raise PermissionError(f"Skill path is not authorized by this run: {path}")


def _is_acp_workspace_path(path: str) -> bool:
    """Check if a path is under the ACP workspace virtual path."""
    return path == _ACP_WORKSPACE_VIRTUAL_PATH or path.startswith(f"{_ACP_WORKSPACE_VIRTUAL_PATH}/")


def _get_custom_mounts():
    """Get custom volume mounts from sandbox config.

    Result is cached after the first successful config load.  If config loading
    fails an empty list is returned *without* caching so that a later call can
    pick up the real value once the config is available.
    """
    cached = getattr(_get_custom_mounts, "_cached", None)
    if cached is not None:
        return cached
    try:
        from pathlib import Path

        from deerflow.config import get_app_config

        config = get_app_config()
        mounts = []
        if config.sandbox and config.sandbox.mounts:
            # Only include mounts whose host_path exists, consistent with
            # LocalSandboxProvider._setup_path_mappings() which also filters
            # by host_path.exists().
            mounts = [m for m in config.sandbox.mounts if Path(m.host_path).exists()]
        _get_custom_mounts._cached = mounts  # type: ignore[attr-defined]
        return mounts
    except Exception:
        # If config loading fails, return an empty list without caching so that
        # a later call can retry once the config is available.
        return []


def _is_custom_mount_path(path: str) -> bool:
    """Check if path is under a custom mount container_path."""
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            return True
    return False


def _get_custom_mount_for_path(path: str):
    """Get the mount config matching this path (longest prefix first)."""
    best = None
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            if best is None or len(mount.container_path) > len(best.container_path):
                best = mount
    return best


def _extract_thread_id_from_thread_data(thread_data: "ThreadDataState | None") -> str | None:
    """Extract thread_id from thread_data by inspecting workspace_path.

    The workspace_path has the form
    ``{base_dir}/threads/{thread_id}/user-data/workspace``, so
    ``Path(workspace_path).parent.parent.name`` yields the thread_id.
    """
    if thread_data is None:
        return None
    workspace_path = thread_data.get("workspace_path")
    if not workspace_path:
        return None
    try:
        # {base_dir}/threads/{thread_id}/user-data/workspace → parent.parent = threads/{thread_id}
        return Path(workspace_path).parent.parent.name
    except Exception:
        return None


def _get_acp_workspace_host_path(thread_id: str | None = None) -> str | None:
    """Get the ACP workspace host filesystem path.

    When *thread_id* is provided, returns the per-thread workspace
    ``{base_dir}/threads/{thread_id}/acp-workspace/`` (not cached — the
    directory is created on demand by ``invoke_acp_agent_tool``).

    Falls back to the global ``{base_dir}/acp-workspace/`` when *thread_id*
    is ``None``; that result is cached after the first successful resolution.
    Returns ``None`` if the directory does not exist.
    """
    if thread_id is not None:
        try:
            from deerflow.config.paths import get_paths
            from deerflow.runtime.user_context import get_effective_user_id

            host_path = get_paths().acp_workspace_dir(thread_id, user_id=get_effective_user_id())
            if host_path.exists():
                return str(host_path)
        except Exception:
            pass
        return None

    cached = getattr(_get_acp_workspace_host_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        from deerflow.config.paths import get_paths

        host_path = get_paths().base_dir / "acp-workspace"
        if host_path.exists():
            value = str(host_path)
            _get_acp_workspace_host_path._cached = value  # type: ignore[attr-defined]
            return value
    except Exception:
        pass
    return None


def _resolve_acp_workspace_path(path: str, thread_id: str | None = None) -> str:
    """Resolve a virtual ACP workspace path to a host filesystem path.

    Args:
        path: Virtual path (e.g. /mnt/acp-workspace/hello_world.py)
        thread_id: Current thread ID for per-thread workspace resolution.
                   When ``None``, falls back to the global workspace.

    Returns:
        Resolved host path.

    Raises:
        FileNotFoundError: If ACP workspace directory does not exist.
        PermissionError: If path traversal is detected.
    """
    _reject_path_traversal(path)

    host_path = _get_acp_workspace_host_path(thread_id)
    if host_path is None:
        raise FileNotFoundError(f"ACP workspace directory not available for path: {path}")

    if path == _ACP_WORKSPACE_VIRTUAL_PATH:
        return host_path

    relative = path[len(_ACP_WORKSPACE_VIRTUAL_PATH) :].lstrip("/")
    resolved = _join_path_preserving_style(host_path, relative)

    if "/" in host_path and "\\" not in host_path:
        base_path = posixpath.normpath(host_path)
        candidate_path = posixpath.normpath(resolved)
        try:
            if posixpath.commonpath([base_path, candidate_path]) != base_path:
                raise PermissionError("Access denied: path traversal detected")
        except ValueError:
            raise PermissionError("Access denied: path traversal detected") from None
        return resolved

    resolved_path = Path(resolved).resolve()
    try:
        resolved_path.relative_to(Path(host_path).resolve())
    except ValueError:
        raise PermissionError("Access denied: path traversal detected")

    return str(resolved_path)


def _resolve_local_read_path(path: str, thread_data: ThreadDataState) -> str:
    validate_local_tool_path(path, thread_data, read_only=True)
    if _is_skills_path(path) or _is_acp_workspace_path(path):
        # Skills and ACP workspace paths are resolved by the sandbox's
        # PathMapping (which uses the user_id from acquire time), not
        # by _resolve_skills_path / _resolve_acp_workspace_path (which
        # use get_effective_user_id() from contextvar and may differ
        # from the sandbox mapping's user_id).
        return path
    return _resolve_and_validate_user_data_path(path, thread_data)


def _path_variants(path: str) -> set[str]:
    return {path, path.replace("\\", "/"), path.replace("/", "\\")}


def _path_separator_for_style(path: str) -> str:
    return "\\" if "\\" in path and "/" not in path else "/"


def _join_path_preserving_style(base: str, relative: str) -> str:
    if not relative:
        return base
    separator = _path_separator_for_style(base)
    normalized_relative = relative.replace("\\" if separator == "/" else "/", separator).lstrip("/\\")
    stripped_base = base.rstrip("/\\")
    return f"{stripped_base}{separator}{normalized_relative}"


def replace_virtual_path(path: str, thread_data: ThreadDataState | None) -> str:
    """Replace virtual /mnt/user-data paths with actual thread data paths.

    Mapping:
        /mnt/user-data/workspace/* -> thread_data['workspace_path']/*
        /mnt/user-data/uploads/* -> thread_data['uploads_path']/*
        /mnt/user-data/outputs/* -> thread_data['outputs_path']/*

    Args:
        path: The path that may contain virtual path prefix.
        thread_data: The thread data containing actual paths.

    Returns:
        The path with virtual prefix replaced by actual path.
    """
    if thread_data is None:
        return path

    mappings = _thread_virtual_to_actual_mappings(thread_data)
    if not mappings:
        return path

    # Longest-prefix-first replacement with segment-boundary checks.
    for virtual_base, actual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        if path == virtual_base:
            return actual_base
        if path.startswith(f"{virtual_base}/"):
            rest = path[len(virtual_base) :].lstrip("/")
            result = _join_path_preserving_style(actual_base, rest)
            if path.endswith("/") and not result.endswith(("/", "\\")):
                result += _path_separator_for_style(actual_base)
            return result

    return path


_DELEGATED_OUTPUT_RUNTIME_PREFIX = "/mnt/user-data/workspace/.deerflow/subagents/"
_DELEGATED_HIDDEN_RUNTIME_ROOT = "/mnt/user-data/workspace/.deerflow"
_DELEGATED_INTERNAL_ALIAS_ROOT = "/mnt/user-data/workspace/.tool-results"


def delegated_output_root(runtime: Runtime | None) -> str | None:
    """Return the Worker-issued per-Task output view, if this is a delegate."""

    context = getattr(runtime, "context", None)
    authority = require_private_file_authority(context or {})
    if authority is None:
        return None
    raw = getattr(authority, "delegated_output_root", None)
    if raw is None:
        return None
    if type(raw) is not str:
        raise SandboxRuntimeError("Invalid delegated output view")
    path = PurePosixPath(raw)
    relative = raw.removeprefix(_DELEGATED_OUTPUT_RUNTIME_PREFIX)
    parts = relative.split("/")
    if (
        not raw.startswith(_DELEGATED_OUTPUT_RUNTIME_PREFIX)
        or path.as_posix() != raw
        or ".." in path.parts
        or len(parts) != 2
        or len(parts[0]) != 32
        or any(character not in "0123456789abcdef" for character in parts[0])
        or parts[1] != "outputs"
    ):
        raise SandboxRuntimeError("Invalid delegated output view")
    return raw


def resolve_delegated_tool_path(
    runtime: Runtime | None,
    path: str,
) -> str:
    """Map the delegate's virtual outputs to its exact private scratch view."""

    output_root = delegated_output_root(runtime)
    if output_root is None:
        return path
    if type(path) is not str or not path.startswith("/") or "\\" in path:
        raise PermissionError(
            "Delegated private file operations require an absolute path",
        )
    candidate = PurePosixPath(path)
    if candidate.as_posix() != path or ".." in candidate.parts:
        raise PermissionError("Invalid delegated private file path")

    if path == _DELEGATED_HIDDEN_RUNTIME_ROOT or path.startswith(
        f"{_DELEGATED_HIDDEN_RUNTIME_ROOT}/",
    ):
        raise PermissionError(
            "Delegated runtime state is not directly accessible",
        )

    capture_root = str(PurePosixPath(output_root).parent)
    delegated_internal_root = f"{capture_root}/internal/.tool-results"
    if path == _DELEGATED_INTERNAL_ALIAS_ROOT:
        return delegated_internal_root
    if path.startswith(f"{_DELEGATED_INTERNAL_ALIAS_ROOT}/"):
        relative = path.removeprefix(f"{_DELEGATED_INTERNAL_ALIAS_ROOT}/")
        return f"{delegated_internal_root}/{relative}"

    canonical_outputs = "/mnt/user-data/outputs"
    if path == canonical_outputs:
        return output_root
    if path.startswith(f"{canonical_outputs}/"):
        return f"{output_root}/{path.removeprefix(f'{canonical_outputs}/')}"

    return path


def _delegated_result_exposes_hidden_runtime(
    runtime: Runtime | None,
    path: str,
) -> bool:
    """Hide runtime-owned scratch when a delegate scans a workspace ancestor."""

    if delegated_output_root(runtime) is None:
        return False
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized == _DELEGATED_HIDDEN_RUNTIME_ROOT or normalized.startswith(
        f"{_DELEGATED_HIDDEN_RUNTIME_ROOT}/",
    )


def _thread_virtual_to_actual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    """Build virtual-to-actual path mappings for a thread."""
    mappings: dict[str, str] = {}

    workspace = thread_data.get("workspace_path")
    uploads = thread_data.get("uploads_path")
    outputs = thread_data.get("outputs_path")

    if workspace:
        mappings[f"{VIRTUAL_PATH_PREFIX}/workspace"] = workspace
    if uploads:
        mappings[f"{VIRTUAL_PATH_PREFIX}/uploads"] = uploads
    if outputs:
        mappings[f"{VIRTUAL_PATH_PREFIX}/outputs"] = outputs

    # Also map the virtual root when all known dirs share the same parent.
    actual_dirs = [Path(p) for p in (workspace, uploads, outputs) if p]
    if actual_dirs:
        common_parent = str(Path(actual_dirs[0]).parent)
        if all(str(path.parent) == common_parent for path in actual_dirs):
            mappings[VIRTUAL_PATH_PREFIX] = common_parent

    return mappings


def _thread_actual_to_virtual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    """Build actual-to-virtual mappings for output masking."""
    return {actual: virtual for virtual, actual in _thread_virtual_to_actual_mappings(thread_data).items()}


@lru_cache(maxsize=512)
def _compiled_mask_patterns(sources: tuple[tuple[str, str], ...]) -> tuple[tuple[re.Pattern[str], str, str], ...]:
    """Compile the host→virtual masking patterns once per source set.

    ``sources`` is an ordered tuple of ``(host_base, virtual_base)`` pairs
    (skills, then ACP workspace, then per-thread user-data mappings sorted by
    host-path length, longest first). The patterns derive only from
    config-stable + per-thread inputs, so they're cached and reused instead of
    being rebuilt — ``re.escape`` + ``re.compile`` + ``Path.resolve`` (a
    syscall) — on every call. ``mask_local_paths_in_output`` runs once per
    glob/grep match, so without this the same patterns are recompiled per
    match.
    """
    compiled: list[tuple[re.Pattern[str], str, str]] = []
    for host_base, virtual_base in sources:
        seen: set[str] = set()
        # Same base set as ``_path_variants(raw) | _path_variants(resolved)``;
        # ordered deterministically so the cached tuple is stable (variants of
        # one host map to the same virtual and don't overlap after substitution,
        # so order within a source is irrelevant to the result).
        for root in (str(Path(host_base)), str(Path(host_base).resolve())):
            for variant in sorted(_path_variants(root)):
                if variant in seen:
                    continue
                seen.add(variant)
                compiled.append(
                    (
                        build_output_mask_pattern(
                            variant,
                            separator_agnostic=True,
                        ),
                        variant,
                        virtual_base,
                    )
                )
    return tuple(compiled)


def mask_local_paths_in_output(output: str, thread_data: ThreadDataState | None) -> str:
    """Mask host absolute paths from local sandbox output using virtual paths.

    Handles user-data paths (per-thread), skills paths (global + per-user
    custom), and ACP workspace paths (per-thread).
    """
    # Build the ordered (host_base, virtual_base) source list. Order is
    # preserved from the original implementation: skills, then per-user
    # custom skills, then ACP workspace, then user-data mappings (longest
    # host path first). Custom mount host paths are masked by
    # LocalSandbox._reverse_resolve_paths_in_output().
    sources: list[tuple[str, str]] = []

    skills_host = _get_skills_host_path()
    if skills_host:
        sources.append((skills_host, _get_skills_container_path()))

    # Per-user custom skills: mask host paths under the user's custom
    # skills directory back to /mnt/skills/custom. The sandbox's
    # _reverse_resolve_path handles this for its own operations, but
    # mask_local_paths_in_output serves as a safety net for edge cases
    # where host paths appear in output that bypassed sandbox resolution.
    try:
        from deerflow.config.paths import get_paths
        from deerflow.runtime.user_context import get_effective_user_id

        user_id = get_effective_user_id()
        user_custom_dir = get_paths().user_custom_skills_dir(user_id)
        if user_custom_dir.exists():
            skills_container = _get_skills_container_path()
            sources.append((str(user_custom_dir), f"{skills_container}/custom"))
    except Exception:
        pass

    acp_host = _get_acp_workspace_host_path(_extract_thread_id_from_thread_data(thread_data))
    if acp_host:
        sources.append((acp_host, _ACP_WORKSPACE_VIRTUAL_PATH))

    if thread_data is not None:
        mappings = _thread_actual_to_virtual_mappings(thread_data)
        for actual_base, virtual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
            sources.append((actual_base, virtual_base))

    if not sources:
        return output

    result = output
    for pattern, base, virtual in _compiled_mask_patterns(tuple(sources)):

        def replace_match(match: re.Match, _base: str = base, _virtual: str = virtual) -> str:
            matched_path = match.group(0)
            if matched_path == _base:
                return _virtual
            relative = matched_path[len(_base) :].lstrip("/\\")
            return f"{_virtual}/{relative}" if relative else _virtual

        result = pattern.sub(replace_match, result)

    return result


def _reject_path_traversal(path: str) -> None:
    """Reject paths that contain '..' segments to prevent directory traversal."""
    # Normalise to forward slashes, then check for '..' segments.
    normalised = path.replace("\\", "/")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PermissionError("Access denied: path traversal detected")


def validate_local_tool_path(path: str, thread_data: ThreadDataState | None, *, read_only: bool = False) -> None:
    """Validate that a virtual path is allowed for local-sandbox access.

    This function is a security gate — it checks whether *path* may be
    accessed and raises on violation.  It does **not** resolve the virtual
    path to a host path; callers are responsible for resolution via
    ``resolve_and_validate_user_data_path`` or ``_resolve_skills_path``.

    Allowed virtual-path families:
      - ``/mnt/user-data/*``  — always allowed (read + write)
      - ``/mnt/skills/*``     — allowed only when *read_only* is True
      - ``/mnt/acp-workspace/*`` — allowed only when *read_only* is True
      - Custom mount paths (from config.yaml) — respects per-mount ``read_only`` flag

    Args:
        path: The virtual path to validate.
        thread_data: Thread data (must be present for local sandbox).
        read_only: When True, skills and ACP workspace paths are permitted.

    Raises:
        SandboxRuntimeError: If thread data is missing.
        PermissionError: If the path is not allowed or contains traversal.
    """
    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for local sandbox")

    _reject_path_traversal(path)

    # Skills paths — read-only access only
    if _is_skills_path(path):
        if not read_only:
            raise PermissionError(f"Write access to skills path is not allowed: {path}")
        return

    # ACP workspace paths — read-only access only
    if _is_acp_workspace_path(path):
        if not read_only:
            raise PermissionError(f"Write access to ACP workspace is not allowed: {path}")
        return

    # User-data paths
    if path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        return

    # Custom mount paths — respect read_only config
    if _is_custom_mount_path(path):
        mount = _get_custom_mount_for_path(path)
        if mount and mount.read_only and not read_only:
            raise PermissionError(f"Write access to read-only mount is not allowed: {path}")
        return

    raise PermissionError(f"Only paths under {VIRTUAL_PATH_PREFIX}/, {_get_skills_container_path()}/, {_ACP_WORKSPACE_VIRTUAL_PATH}/, or configured mount paths are allowed")


def _validate_resolved_user_data_path(resolved: Path, thread_data: ThreadDataState) -> None:
    """Verify that a resolved host path stays inside allowed per-thread roots.

    Raises PermissionError if the path escapes workspace/uploads/outputs.
    """
    allowed_roots = [
        Path(p).resolve()
        for p in (
            thread_data.get("workspace_path"),
            thread_data.get("uploads_path"),
            thread_data.get("outputs_path"),
        )
        if p is not None
    ]

    if not allowed_roots:
        raise SandboxRuntimeError("No allowed local sandbox directories configured")

    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue

    raise PermissionError("Access denied: path traversal detected")


def _resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """Resolve a /mnt/user-data virtual path and validate it stays in bounds.

    Returns the resolved host path string.
    """
    resolved_str = replace_virtual_path(path, thread_data)
    resolved = Path(resolved_str).resolve()
    _validate_resolved_user_data_path(resolved, thread_data)
    return str(resolved)


def resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """Resolve a /mnt/user-data virtual path and validate it stays in bounds."""
    return _resolve_and_validate_user_data_path(path, thread_data)
