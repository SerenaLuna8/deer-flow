#!/usr/bin/env python3
"""Create a redacted ActWeave support bundle for private troubleshooting."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - exercised only in broken environments
    yaml = None


SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key|private[_-]?key"
    r"|(?<![a-zA-Z])key(?![a-zA-Z])"
    r"|token|secret|password|passwd|pwd"
    r"|(?<![a-zA-Z])pass(?!port)"
    r"|authorization|cookie|credential"
    r"|(?<![a-zA-Z])dsn(?![a-zA-Z]))",
    re.IGNORECASE,
)
# Provider configuration is deliberately open-ended. Boundary-aware bare
# key/pass/dsn matching covers unknown secret-bearing names without treating
# ordinary words such as ``keyboard``, ``passport``, or ``compass`` as secret
# keys. These exact no-flag names mirror common credential sources whose names
# do not otherwise contain an unambiguous secret token.
NO_FLAG_CREDENTIAL_KEY_NAMES = frozenset(
    {
        "gh_pat",
        "github_pat",
        "redis_auth",
        "rediscli_auth",
        "pgservicefile",
    }
)
ENV_KEY_RE = re.compile(r"(?i)^env$")
VAR_REFERENCE_RE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")
ENV_SECRET_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTHORIZATION|COOKIE|CREDENTIAL)[A-Z0-9_]*\s*=\s*)(.+)$"
)
YAML_SECRET_RE = re.compile(
    r"(?im)^(\s*[\w.-]*(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential|private[_-]?key)[\w.-]*\s*:\s*)(.+)$"
)
BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
URL_USERINFO_RE = re.compile(r"([a-zA-Z][\w+.-]*://)([^/?#\s@]+)@")
URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&][\w.-]*(?:api[_-]?key|token|secret|password|passwd|authorization|access[_-]?token|credential)[\w.-]*=)([^&\s#]+)"
)
CLI_INLINE_SECRET_RE = re.compile(
    r"(?i)(--?[\w.-]*(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)[\w.-]*=)(\S+)"
)
INLINE_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![\w.-])"
    r"((?:api[_-]?key|access[_-]?key|token|secret|password|passwd|pwd|authorization|cookie|credential|dsn)"
    r"\s*=\s*)"
    r"([^\s,;&#]+)"
)
SECRET_FLAG_RE = re.compile(
    r"(?i)^--?[\w.-]*(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)[\w.-]*$"
)
HEADER_KEY_RE = re.compile(r"(?i)header")
POSIX_HOME_RE = re.compile(r"(?<![\w.-])(/Users|/home)/([^/\s:]+)")
WINDOWS_HOME_RE = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\\s:]+)")
SAFE_THREAD_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
DOCTOR_STATUS_RE = re.compile(
    r"Status:\s*(\d+)\s+error\(s\),\s*(\d+)\s+warning\(s\)", re.IGNORECASE
)
ATTENTION_SIGNAL_NAMES = {
    "doctor_failed",
    "config_missing",
    "config_error",
    "node_missing",
    "node_version_too_old",
    "nginx_missing",
    "dirty_worktree",
}


def _redact_yaml_secret_match(match: re.Match[str]) -> str:
    prefix = match.group(1)
    value = match.group(2)
    if "authorization" in prefix.lower() and value.lstrip().lower().startswith(
        "bearer "
    ):
        return prefix + BEARER_RE.sub(r"\1<redacted>", value)
    return prefix + "<redacted>"


def redact_text(text: str) -> str:
    """Redact common secret patterns from free-form text."""
    text = POSIX_HOME_RE.sub(r"\1/<user>", text)
    text = WINDOWS_HOME_RE.sub(r"\1<user>", text)
    text = URL_USERINFO_RE.sub(r"\1<redacted>@", text)
    text = URL_QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    text = CLI_INLINE_SECRET_RE.sub(r"\1<redacted>", text)
    text = INLINE_SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", text)
    text = ENV_SECRET_RE.sub(r"\1<redacted>", text)
    text = YAML_SECRET_RE.sub(_redact_yaml_secret_match, text)
    text = BEARER_RE.sub(r"\1<redacted>", text)
    return OPENAI_KEY_RE.sub("sk-<redacted>", text)


def _redact_secret_flag_list(items: list[Any]) -> list[Any]:
    """Mask the value that follows a secret-like CLI flag (e.g. ['--api-key', 'X'])."""
    redacted: list[Any] = []
    mask_next = False
    for item in items:
        if mask_next:
            redacted.append(
                "<redacted>" if isinstance(item, str) else redact_data(item)
            )
            mask_next = False
            continue
        if isinstance(item, str) and SECRET_FLAG_RE.fullmatch(item):
            redacted.append(item)
            mask_next = True
            continue
        redacted.append(redact_data(item))
    return redacted


def _redact_env_value(value: Any) -> Any:
    """Mask env values by default; keep only ``$VAR`` / ``${VAR}`` references visible."""
    if isinstance(value, str) and VAR_REFERENCE_RE.fullmatch(value.strip()):
        return value
    if isinstance(value, (dict, list, tuple)):
        return redact_data(value)
    return "<redacted>"


def redact_data(value: Any) -> Any:
    """Recursively redact secret-like mapping keys while preserving structure."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if (
                SECRET_KEY_RE.search(key_text)
                or key_text.lower() in NO_FLAG_CREDENTIAL_KEY_NAMES
            ):
                redacted[key] = "<redacted>"
            elif ENV_KEY_RE.fullmatch(key_text) and isinstance(item, dict):
                redacted[key] = {k: _redact_env_value(v) for k, v in item.items()}
            elif HEADER_KEY_RE.search(key_text) and isinstance(item, dict):
                redacted[key] = {k: "<redacted>" for k in item}
            else:
                redacted[key] = redact_data(item)
        return redacted
    if isinstance(value, list):
        return _redact_secret_flag_list(value)
    if isinstance(value, tuple):
        return _redact_secret_flag_list(list(value))
    if isinstance(value, str):
        return redact_text(value)
    return value


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return {"present": False}
    if yaml is None:
        return {"present": True, "error": "PyYAML is not available"}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"present": True, "error": "yaml_parse_failed"}


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {"present": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"present": True, "error": "json_parse_failed"}


def _run_command(args: list[str], cwd: Path, timeout_s: int = 10) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": redact_text((result.stdout or "").strip()),
            "stderr": redact_text((result.stderr or "").strip()),
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"{args[0]} not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{args[0]} timed out after {timeout_s}s"}
    except Exception as exc:
        return {
            "ok": False,
            "error": redact_text(f"{type(exc).__name__}: {exc}"),
        }


def _version_command(name: str, args: list[str], cwd: Path) -> dict[str, Any]:
    result = _run_command(args, cwd=cwd, timeout_s=5)
    return {"name": name, **result}


def collect_environment(project_root: Path) -> dict[str, Any]:
    """Collect non-secret environment and toolchain metadata."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "commands": [
            _version_command("node", ["node", "--version"], project_root),
            _version_command(
                "pnpm",
                [
                    sys.executable,
                    str(project_root / "scripts" / "pnpm.py"),
                    "--version",
                ],
                project_root / "frontend",
            ),
            _version_command("uv", ["uv", "--version"], project_root),
            _version_command("nginx", ["nginx", "-v"], project_root),
            _version_command("docker", ["docker", "--version"], project_root),
        ],
    }


def collect_config_summary(config_path: Path) -> Any:
    # Never export the open-ended config tree. Unknown provider/plugin keys
    # make denylist redaction intrinsically incomplete.
    return _config_summary(_read_yaml(config_path))


def collect_git_summary(project_root: Path) -> dict[str, Any]:
    """Collect best-effort git metadata without requiring a git checkout."""
    commands = {
        "branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        "head": ["git", "rev-parse", "HEAD"],
        "upstream": [
            "git",
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{u}",
        ],
        "status_short": ["git", "status", "--short", "--branch"],
        "diff_stat": ["git", "diff", "--stat"],
    }
    return {
        name: _run_command(command, cwd=project_root)
        for name, command in commands.items()
    }


def _validate_thread_id(thread_id: str) -> None:
    if (
        not thread_id
        or thread_id in {".", ".."}
        or ".." in thread_id
        or not SAFE_THREAD_ID_RE.fullmatch(thread_id)
    ):
        raise ValueError("Invalid thread_id")


def _file_manifest(root: Path, *, max_files: int = 500) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if len(entries) >= max_files:
            entries.append(
                {"path": "<truncated>", "reason": f"file limit {max_files} reached"}
            )
            break
        try:
            stat = path.stat()
        except OSError as exc:
            entries.append(
                {
                    "path": redact_text(path.relative_to(root).as_posix()),
                    "error": redact_text(f"{type(exc).__name__}: {exc}"),
                }
            )
            continue
        entries.append(
            {
                "path": redact_text(path.relative_to(root).as_posix()),
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }
        )
    return entries


def collect_thread_summary(project_root: Path, thread_id: str) -> dict[str, Any]:
    """Reject legacy thread-only manifests without project and owner scope."""

    del project_root
    _validate_thread_id(thread_id)
    raise ValueError(
        "Unscoped thread summaries are disabled; trusted project and owner scope are required"
    )


def collect_doctor_output(project_root: Path) -> dict[str, Any]:
    backend_dir = project_root / "backend"
    cwd = backend_dir if backend_dir.exists() else project_root
    return _run_command(
        [sys.executable, str(project_root / "scripts" / "doctor.py")],
        cwd=cwd,
        timeout_s=60,
    )


def _command_output(command: dict[str, Any] | None) -> str | None:
    if not command:
        return None
    for key in ("stdout", "stderr", "error"):
        value = command.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _environment_versions(environment: dict[str, Any]) -> dict[str, str | None]:
    closed_versions = environment.get("versions")
    if isinstance(closed_versions, dict):
        return {
            str(name): value if isinstance(value, str) else None
            for name, value in closed_versions.items()
        }
    platform_info = environment.get("platform", {})
    python_version = (
        platform_info.get("python") if isinstance(platform_info, dict) else None
    )
    versions: dict[str, str | None] = {
        "python": python_version if isinstance(python_version, str) else None
    }
    for command in environment.get("commands", []):
        if isinstance(command, dict) and isinstance(command.get("name"), str):
            versions[command["name"]] = _command_output(command)
    return versions


def _parse_major_version(version_text: str | None) -> int | None:
    if not version_text:
        return None
    match = re.search(r"v?(\d+)(?:\.\d+)?", version_text)
    return int(match.group(1)) if match else None


def _git_stdout(git_summary: dict[str, Any], key: str) -> str | None:
    closed = git_summary.get(key)
    if closed is None or isinstance(closed, str):
        return closed
    value = git_summary.get(key)
    return _command_output(value) if isinstance(value, dict) else None


def _doctor_counts(doctor: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not doctor:
        return (None, None)
    if isinstance(doctor.get("errors"), int) and isinstance(
        doctor.get("warnings"),
        int,
    ):
        return (doctor["errors"], doctor["warnings"])
    output = "\n".join(
        value
        for value in (
            _command_output(doctor),
            doctor.get("stdout"),
            doctor.get("stderr"),
        )
        if isinstance(value, str)
    )
    match = DOCTOR_STATUS_RE.search(output)
    if not match:
        return (None, None)
    return (int(match.group(1)), int(match.group(2)))


def _enabled_mapping_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    keys: list[str] = []
    for key, item in value.items():
        if isinstance(item, dict) and item.get("enabled") is False:
            continue
        keys.append(str(key))
    return sorted(keys)


_CONFIG_ERROR_CODE_BY_INPUT = {
    "yaml_parse_failed": "yaml_parse_failed",
    "json_parse_failed": "json_parse_failed",
    "PyYAML is not available": "pyyaml_unavailable",
    "pyyaml_unavailable": "pyyaml_unavailable",
    "invalid_config_shape": "invalid_config_shape",
}


def _config_summary(config_summary: Any) -> dict[str, Any]:
    if not isinstance(config_summary, dict):
        return {
            "present": True,
            "config_version": None,
            "error": "invalid_config_shape",
            "tools": 0,
            "channels": 0,
        }
    present = config_summary.get("present", True)
    if present is False:
        return {
            "present": False,
            "config_version": None,
            "error": None,
            "tools": 0,
            "channels": 0,
        }
    raw_config_version = config_summary.get("config_version")
    safe_config_version = (
        raw_config_version
        if type(raw_config_version) is int and 0 <= raw_config_version <= 1_000_000
        else None
    )
    raw_error = config_summary.get("error")
    safe_error = (
        _CONFIG_ERROR_CODE_BY_INPUT.get(raw_error)
        if isinstance(raw_error, str)
        else None
    )
    if all(
        type(config_summary.get(field)) is int and config_summary[field] >= 0
        for field in ("tools", "channels")
    ):
        return {
            "present": True,
            "config_version": safe_config_version,
            "error": safe_error,
            "tools": config_summary["tools"],
            "channels": config_summary["channels"],
        }
    tools = config_summary.get("tools")
    channels = config_summary.get("channels")
    return {
        "present": True,
        "config_version": safe_config_version,
        "error": safe_error,
        "tools": (
            sum(1 for tool in tools if isinstance(tool, dict))
            if isinstance(tools, list)
            else 0
        ),
        "channels": len(_enabled_mapping_keys(channels)),
    }


_VERSION_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])v?\d+(?:\.\d+){1,3}"
    r"(?:[-+][A-Za-z0-9.-]+)?"
)
_KNOWN_MACHINES = frozenset(
    {
        "aarch64",
        "amd64",
        "arm64",
        "i386",
        "i686",
        "x86",
        "x86_64",
    }
)


def _version_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_TOKEN_RE.search(value)
    return match.group(0) if match else None


def _closed_environment_summary(
    environment: dict[str, Any],
) -> dict[str, Any]:
    raw_platform = environment.get("platform")
    raw_platform = raw_platform if isinstance(raw_platform, dict) else {}
    raw_system = raw_platform.get("system")
    system = raw_system if raw_system in {"Darwin", "Linux", "Windows"} else "Other"
    raw_machine = raw_platform.get("machine")
    machine = (
        raw_machine.lower()
        if isinstance(raw_machine, str) and raw_machine.lower() in _KNOWN_MACHINES
        else None
    )
    platform_summary = {
        "system": system,
        "release": _version_token(raw_platform.get("release")),
        "machine": machine,
        "python": _version_token(raw_platform.get("python")),
    }

    versions: dict[str, str | None] = {
        "python": platform_summary["python"],
    }
    for command in environment.get("commands", []):
        if not isinstance(command, dict):
            continue
        name = command.get("name")
        if name not in {"node", "pnpm", "uv", "nginx", "docker"}:
            continue
        output = _command_output(command)
        versions[name] = _version_token(output) or (
            "not_found"
            if isinstance(output, str) and "not found" in output.lower()
            else None
        )
    for name in ("node", "pnpm", "uv", "nginx", "docker"):
        versions.setdefault(name, None)
    return {
        "platform": platform_summary,
        "versions": versions,
    }


def _closed_git_summary(
    git_summary: dict[str, Any],
) -> dict[str, Any]:
    raw_head = _git_stdout(git_summary, "head")
    head = (
        raw_head.lower()
        if isinstance(raw_head, str) and re.fullmatch(r"[0-9a-fA-F]{40}", raw_head)
        else None
    )
    return {
        # Branch/upstream names and status paths are user-controlled and can
        # contain opaque secrets; expose only the commit digest and dirty bit.
        "branch": None,
        "head": head,
        "upstream": None,
        "dirty_worktree": _dirty_worktree(_git_stdout(git_summary, "status_short")),
    }


def _closed_doctor_summary(
    doctor: dict[str, Any],
) -> dict[str, Any]:
    errors, warnings = _doctor_counts(doctor)
    raw_returncode = doctor.get("returncode")
    return {
        "included": True,
        "ok": doctor.get("ok") is True,
        "returncode": (
            raw_returncode
            if isinstance(raw_returncode, int) and -255 <= raw_returncode <= 255
            else None
        ),
        "errors": errors,
        "warnings": warnings,
    }


def _dirty_worktree(status_short: str | None) -> bool:
    if not status_short:
        return False
    return any(line and not line.startswith("##") for line in status_short.splitlines())


def _status_from_signals(signals: dict[str, bool]) -> str:
    if signals["config_missing"] or signals["config_error"]:
        return "needs_user_setup"
    if (
        signals["node_missing"]
        or signals["node_version_too_old"]
        or signals["nginx_missing"]
    ):
        return "environment_mismatch"
    if not signals["doctor_included"]:
        return "insufficient_evidence"
    if signals["doctor_failed"]:
        return "likely_runtime_issue"
    return "ok"


def _active_signal_names(signals: dict[str, bool]) -> list[str]:
    return [
        name
        for name, enabled in signals.items()
        if enabled and name in ATTENTION_SIGNAL_NAMES
    ]


def _maintainer_next_steps(status: str, signals: dict[str, bool]) -> list[str]:
    steps: list[str] = []
    if status == "needs_user_setup":
        steps.append(
            "Ask the reporter to complete local setup with `make setup`, then rerun `make doctor` and `make support-bundle`."
        )
    if signals["node_missing"] or signals["node_version_too_old"]:
        steps.append(
            "Ask the reporter to install Node.js 22+ before treating this as an application bug."
        )
    if signals["config_missing"]:
        steps.append("Do not triage runtime behavior until `config.yaml` exists.")
    if signals["config_error"]:
        steps.append(
            "Ask the reporter to fix `config.yaml` syntax or regenerate it with `make setup`."
        )
    if signals["doctor_failed"] and status == "likely_runtime_issue":
        steps.append(
            "Use `doctor.json` plus the reproduction steps in the internal incident record to identify the failing subsystem."
        )
    if signals["thread_summary_included"]:
        steps.append(
            "Use `thread-summary.json` to inspect workspace/upload/output file shape; raw file contents are intentionally absent."
        )
    if not steps:
        steps.append(
            "Use the incident reproduction steps and evidence JSON files to continue triage."
        )
    return steps


def _reporter_next_steps(status: str, signals: dict[str, bool]) -> list[str]:
    steps: list[str] = []
    if status == "needs_user_setup":
        steps.append(
            "Run `make setup`, then rerun `make doctor` and `make support-bundle` before creating an internal incident record if the problem changes."
        )
    if signals["node_missing"] or signals["node_version_too_old"]:
        steps.append("Install Node.js 22+ and rerun `make doctor`.")
    if signals["config_missing"]:
        steps.append("Create or repair `config.yaml` with `make setup`.")
    if signals["config_error"]:
        steps.append("Fix `config.yaml` syntax or regenerate it with `make setup`.")
    if signals["doctor_failed"] and status == "likely_runtime_issue":
        steps.append(
            "Add the generated diagnostic summary to the internal incident record. Attach the zip if a maintainer asks for the evidence bundle."
        )
    if not steps:
        steps.append(
            "Add the generated diagnostic summary to an internal incident record if the problem still reproduces. Attach the zip if a maintainer asks for the evidence bundle."
        )
    return steps


def _evidence_files(
    *, include_doctor: bool, include_thread_summary: bool
) -> list[dict[str, str]]:
    files = [
        ("README.md", "Human-readable entrypoint for the support bundle."),
        (
            "diagnostic-summary.md",
            "Markdown diagnostic summary for an internal incident record.",
        ),
        (
            "ai-incident-draft.md",
            "Internal incident draft for AI-assisted triage with required placeholders for unknown user facts.",
        ),
        (
            "triage.json",
            "Stable machine-readable summary for AI or script-assisted triage.",
        ),
        ("manifest.json", "Bundle schema, generation time, and privacy declaration."),
        ("environment.json", "OS, Python, and toolchain version probes."),
        ("config-summary.json", "Redacted config.yaml structure."),
        ("git.json", "Branch, commit, upstream, status, and diff-stat metadata."),
    ]
    if include_thread_summary:
        files.append(
            (
                "thread-summary.json",
                "Optional thread workspace/upload/output file manifests only.",
            )
        )
    if include_doctor:
        files.append(("doctor.json", "Redacted make doctor output."))
    return [{"path": path, "description": description} for path, description in files]


def build_triage_report(
    *,
    manifest: dict[str, Any],
    environment: dict[str, Any],
    config_summary: Any,
    git_summary: dict[str, Any],
    doctor: dict[str, Any] | None,
    thread_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the stable machine-readable summary that maintainers and AI read first."""
    versions = _environment_versions(environment)
    config = _config_summary(config_summary)
    node_major = _parse_major_version(versions.get("node"))
    doctor_errors, doctor_warnings = _doctor_counts(doctor)
    dirty_worktree = bool(git_summary.get("dirty_worktree"))
    signals = {
        "doctor_included": doctor is not None,
        "doctor_failed": bool(doctor and not doctor.get("ok")),
        "config_missing": config.get("present") is False,
        "config_error": bool(config.get("error")),
        "node_missing": versions.get("node") is not None
        and "not found" in versions["node"].lower(),
        "node_version_too_old": node_major is not None and node_major < 22,
        "nginx_missing": versions.get("nginx") is not None
        and "not found" in versions["nginx"].lower(),
        "dirty_worktree": dirty_worktree,
        "thread_summary_included": thread_summary is not None,
        "thread_summary_found": bool(thread_summary and thread_summary.get("found")),
    }
    status = _status_from_signals(signals)
    return {
        "schema_version": 1,
        "generated_at": manifest["generated_at"],
        "status": status,
        "active_signals": _active_signal_names(signals),
        "signals": signals,
        "versions": versions,
        "platform": environment.get("platform", {}),
        "config": config,
        "git": git_summary,
        "doctor": (
            doctor
            if doctor is not None
            else {
                "included": False,
                "ok": False,
                "returncode": None,
                "errors": doctor_errors,
                "warnings": doctor_warnings,
            }
        ),
        "thread": {
            "included": thread_summary is not None,
            "found": bool(thread_summary and thread_summary.get("found")),
        },
        "reporter_next_steps": _reporter_next_steps(status, signals),
        "maintainer_next_steps": _maintainer_next_steps(status, signals),
        "evidence_files": _evidence_files(
            include_doctor=doctor is not None,
            include_thread_summary=thread_summary is not None,
        ),
        "privacy": manifest["privacy"],
    }


def _markdown_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- None"


def render_diagnostic_summary(triage: dict[str, Any]) -> str:
    """Render Markdown for a private internal incident record."""
    git = triage["git"]
    doctor = triage["doctor"]
    versions = triage["versions"]
    lines = [
        "## ActWeave support bundle summary",
        "",
        f"- Triage status: {triage['status']}",
        f"- Active signals: {', '.join(triage['active_signals']) or 'none'}",
        f"- Doctor: included={doctor['included']}, ok={doctor['ok']}, errors={doctor['errors']}, warnings={doctor['warnings']}",
        f"- Git: branch={git['branch'] or 'unknown'}, head={git['head'] or 'unknown'}, dirty_worktree={git['dirty_worktree']}",
        f"- Versions: python={versions.get('python') or 'unknown'}, node={versions.get('node') or 'unknown'}, pnpm={versions.get('pnpm') or 'unknown'}, uv={versions.get('uv') or 'unknown'}, nginx={versions.get('nginx') or 'unknown'}",
        "",
        "### Reporter next steps",
        _markdown_list(triage["reporter_next_steps"]),
        "",
        "### Upload guidance",
        "Add this summary to the internal incident record. Attach the zip if a maintainer asks for the evidence bundle, or if the summary alone is not enough to diagnose the problem.",
        "",
        "### Maintainer next steps",
        _markdown_list(triage["maintainer_next_steps"]),
        "",
        "### Evidence files in the attached zip",
        _markdown_list(
            [
                f"`{item['path']}` - {item['description']}"
                for item in triage["evidence_files"]
            ]
        ),
        "",
        "Privacy: this bundle excludes `.env`, raw conversation messages, and user file contents.",
        "",
    ]
    return "\n".join(lines)


def _os_label(platform_info: dict[str, Any]) -> str:
    system = platform_info.get("system")
    if system == "Darwin":
        return "macOS"
    if system == "Linux":
        return "Linux"
    if system == "Windows":
        return "Windows"
    return "Other"


def _platform_details(platform_info: dict[str, Any]) -> str:
    details = [
        platform_info.get("machine"),
        platform_info.get("system"),
        platform_info.get("release"),
    ]
    return ", ".join(str(item) for item in details if item) or "_No response_"


def _draft_affected_areas(triage: dict[str, Any]) -> list[str]:
    signals = triage["signals"]
    areas: list[str] = []
    if (
        signals["config_missing"]
        or signals["config_error"]
        or signals["node_missing"]
        or signals["node_version_too_old"]
        or signals["nginx_missing"]
    ):
        areas.append("Config / setup (make, config.yaml, env)")
    if not areas:
        areas.append("Not sure")
    return areas


def _doctor_excerpt(
    doctor: dict[str, Any] | None, *, max_lines: int = 80, max_chars: int = 12000
) -> str:
    output = _command_output(doctor) if doctor else None
    if not output:
        return "<REQUIRED: paste key log lines. Do not invent if unknown.>"
    output = redact_text(output)
    lines = output.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    excerpt = "\n".join(lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
        truncated = True
    if truncated:
        excerpt += "\n<support bundle doctor output truncated>"
    return excerpt


def render_ai_incident_draft(
    triage: dict[str, Any], diagnostic_summary: str, doctor: dict[str, Any] | None
) -> str:
    """Render an internal incident scaffold for AI-assisted reporters."""
    versions = triage["versions"]
    git = triage["git"]
    platform_info = triage["platform"]
    lines = [
        "# AI incident draft",
        "",
        "Use this when a coding agent or AI assistant prepares an internal ActWeave bug report.",
        "Do not submit this incident record until every REQUIRED placeholder is replaced.",
        "Do not invent if unknown; ask the reporter for missing reproduction facts instead.",
        "",
        "## Incident title",
        "",
        "[bug] <REQUIRED: one-line problem summary>",
        "",
        "### Before you start",
        "",
        "- [ ] I searched the private project's existing incident records and this is not a duplicate.",
        "- [ ] I can reproduce this on the current approved checkout.",
        "",
        "### Problem summary",
        "",
        "<!-- REQUIRED: One sentence describing the bug. Do not invent if unknown. -->",
        "<REQUIRED: one sentence problem summary>",
        "",
        "### Affected area(s)",
        "",
        "\n".join(_draft_affected_areas(triage)),
        "<!-- AI hint: derived from support bundle signals; adjust only if the reporter's reproduction proves a better area. -->",
        "",
        "### What happened?",
        "",
        "<!-- REQUIRED: Actual behavior and key error lines. Do not invent if unknown. -->",
        "<REQUIRED: describe what happened>",
        "",
        "### Expected behavior",
        "",
        "<!-- REQUIRED: What should have happened instead. Do not invent if unknown. -->",
        "<REQUIRED: describe expected behavior>",
        "",
        "### Steps to reproduce",
        "",
        "<!-- REQUIRED: Exact commands and sequence. Do not invent if unknown. -->",
        "1. <REQUIRED: first command or action>",
        "2. <REQUIRED: next command or action>",
        "",
        "### Relevant logs",
        "",
        "<!-- Include additional gateway/frontend/sandbox logs if the reporter has them. Keep secrets redacted. -->",
        "```shell",
        _doctor_excerpt(doctor),
        "```",
        "",
        "### Sandbox runtime",
        "",
        "<REQUIRED: choose Local, Docker, Apple Container, Kubernetes Provisioner, remote provider, or Other>",
        "",
        "### Operating system",
        "",
        _os_label(platform_info),
        "",
        "### Platform details",
        "",
        _platform_details(platform_info),
        "",
        "### Python version",
        "",
        versions.get("python") or "_No response_",
        "",
        "### Node.js version",
        "",
        versions.get("node") or "_No response_",
        "",
        "### pnpm version",
        "",
        versions.get("pnpm") or "_No response_",
        "",
        "### uv version",
        "",
        versions.get("uv") or "_No response_",
        "",
        "### Git state",
        "",
        f"branch: {git['branch'] or 'unknown'}",
        f"commit: {git['head'] or 'unknown'}",
        f"upstream: {git['upstream'] or 'unknown'}",
        f"dirty_worktree: {git['dirty_worktree']}",
        "",
        "### Support bundle summary",
        "",
        diagnostic_summary.rstrip(),
        "",
        "### Additional context",
        "",
        "Attach the zip only if a maintainer asks for the evidence bundle, or if the summary alone is not enough.",
        "",
    ]
    return "\n".join(lines)


def render_bundle_readme(triage: dict[str, Any]) -> str:
    """Render the support bundle README."""
    lines = [
        "# ActWeave Support Bundle",
        "",
        "## Start here",
        "",
        "Add `diagnostic-summary.md` to the private internal incident record.",
        "If an AI assistant is preparing the record, start from `ai-incident-draft.md` and replace every REQUIRED placeholder first.",
        "Maintainers or AI triage tools should read `triage.json` first, then inspect the evidence JSON files only as needed.",
        "",
        "## Triage Summary",
        "",
        f"- Status: {triage['status']}",
        f"- Active signals: {', '.join(triage['active_signals']) or 'none'}",
        "",
        "## Reporter next steps",
        "",
        _markdown_list(triage["reporter_next_steps"]),
        "",
        "## Upload guidance",
        "",
        "Add `diagnostic-summary.md` to the internal incident record. Attach the zip if a maintainer asks for the evidence bundle, or if the summary alone is not enough to diagnose the problem.",
        "",
        "## Maintainer next steps",
        "",
        _markdown_list(triage["maintainer_next_steps"]),
        "",
        "## Files",
        "",
        _markdown_list(
            [
                f"`{item['path']}` - {item['description']}"
                for item in triage["evidence_files"]
            ]
        ),
        "",
        "## Privacy",
        "",
        "- `.env` is not included.",
        "- Raw conversation messages are not included.",
        "- Thread workspace/upload/output file contents are not included; optional thread data is a file manifest only.",
        "",
    ]
    return "\n".join(lines)


def _default_out_path(project_root: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return (
        project_root
        / ".deer-flow"
        / "support-bundles"
        / f"deer-flow-support-bundle-{timestamp}.zip"
    )


def _write_json(zf: zipfile.ZipFile, name: str, data: Any) -> None:
    zf.writestr(f"{name}.json", json.dumps(data, indent=2, sort_keys=True) + "\n")


def _write_text(zf: zipfile.ZipFile, name: str, text: str) -> None:
    zf.writestr(name, text)


def _diagnostic_summary_sidecar_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}-diagnostic-summary.md")


def _incident_draft_sidecar_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}-incident-draft.md")


def create_support_bundle(
    *,
    project_root: Path,
    out_path: Path | None = None,
    config_path: Path | None = None,
    thread_id: str | None = None,
    include_doctor: bool = False,
) -> Path:
    """Create a redacted support bundle and return the zip path."""
    project_root = project_root.resolve()
    config_path = (config_path or project_root / "config.yaml").resolve()
    out_path = (out_path or _default_out_path(project_root)).resolve()
    if thread_id is not None:
        _validate_thread_id(thread_id)
        raise ValueError(
            "Unscoped --thread-id support bundles are disabled; trusted project and owner scope are required"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "project": "<redacted>",
        "includes": {
            "doctor": include_doctor,
            "thread_summary": thread_id is not None,
        },
        "privacy": {
            "redacted_secret_fields": True,
            "raw_thread_messages": False,
            "raw_user_files": False,
            "raw_env_file": False,
        },
    }

    environment = _closed_environment_summary(collect_environment(project_root))
    config_summary = collect_config_summary(config_path)
    git_summary = _closed_git_summary(collect_git_summary(project_root))
    thread_summary = (
        collect_thread_summary(project_root, thread_id) if thread_id else None
    )
    doctor = (
        _closed_doctor_summary(collect_doctor_output(project_root))
        if include_doctor
        else None
    )
    triage = build_triage_report(
        manifest=manifest,
        environment=environment,
        config_summary=config_summary,
        git_summary=git_summary,
        doctor=doctor,
        thread_summary=thread_summary,
    )

    diagnostic_summary = render_diagnostic_summary(triage)
    incident_draft = render_ai_incident_draft(triage, diagnostic_summary, doctor)
    with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_text(zf, "README.md", render_bundle_readme(triage))
        _write_text(zf, "diagnostic-summary.md", diagnostic_summary)
        _write_text(zf, "ai-incident-draft.md", incident_draft)
        _write_json(zf, "triage", triage)
        _write_json(zf, "manifest", manifest)
        _write_json(zf, "environment", environment)
        _write_json(zf, "config-summary", config_summary)
        _write_json(zf, "git", git_summary)
        if thread_summary is not None:
            _write_json(zf, "thread-summary", thread_summary)
        if doctor is not None:
            _write_json(zf, "doctor", doctor)

    _diagnostic_summary_sidecar_path(out_path).write_text(
        diagnostic_summary, encoding="utf-8"
    )
    _incident_draft_sidecar_path(out_path).write_text(incident_draft, encoding="utf-8")
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    repo_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--project-root", type=Path, default=repo_root, help="ActWeave project root"
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--thread-id",
        default=None,
        help=(
            "Deprecated and disabled: thread manifests require trusted project and owner scope"
        ),
    )
    parser.add_argument("--out", type=Path, default=None, help="Output zip path")
    parser.add_argument(
        "--include-doctor",
        action="store_true",
        help="Include redacted make doctor output",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bundle_path = create_support_bundle(
            project_root=args.project_root,
            out_path=args.out,
            config_path=args.config,
            thread_id=args.thread_id,
            include_doctor=args.include_doctor,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with zipfile.ZipFile(bundle_path) as zf:
        triage = json.loads(zf.read("triage.json").decode("utf-8"))
    print(f"Support bundle: {bundle_path}")
    print(f"Diagnostic summary: {_diagnostic_summary_sidecar_path(bundle_path)}")
    print(f"Incident draft: {_incident_draft_sidecar_path(bundle_path)}")
    print("Suggested next steps:")
    for step in triage["reporter_next_steps"]:
        print(f"- {step}")
    print(
        "If the problem still reproduces, add the diagnostic summary to the internal incident record."
    )
    print(
        "If an AI assistant prepares the record, start from the incident draft and replace every REQUIRED placeholder."
    )
    print(
        "Attach the zip if a maintainer asks for the evidence bundle, or if the summary alone is not enough."
    )
    print("Maintainers or AI triage tools should read triage.json first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
