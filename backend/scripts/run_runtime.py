#!/usr/bin/env python3
"""Run a backend role with the repository's filtered local environment.

Backend module commands are valid entry points on their own, so they cannot
depend on the root ``serve.sh`` having sourced ``.env`` first. This launcher
loads non-provider settings explicitly, preserves caller-supplied values, and
removes ambient model-provider keys before replacing itself with the role.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODEL_PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "MIMO_API_KEY",
        "MINIMAX_API_KEY",
        "MOONSHOT_API_KEY",
        "NOVITA_API_KEY",
        "OPENCODE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "STEPFUN_API_KEY",
        "VLLM_API_KEY",
        "VOLCENGINE_API_KEY",
    }
)


class RuntimeHomeMigrationRequired(RuntimeError):
    """The new default would hide an existing legacy native runtime home."""


def _resolve_alias_pair(
    environment: Mapping[str, str],
    canonical_name: str,
    legacy_name: str,
    *,
    default: Path | None = None,
    base: Path,
) -> Path | None:
    def normalized(name: str) -> Path | None:
        value = environment.get(name)
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve(strict=False)

    canonical = normalized(canonical_name)
    legacy = normalized(legacy_name)
    if canonical is not None and legacy is not None and canonical != legacy:
        raise ValueError(f"{canonical_name} resolves to '{canonical}', but {legacy_name} resolves to '{legacy}'; refusing conflicting local runtime paths")
    return canonical or legacy or (default.resolve(strict=False) if default is not None else None)


def _export_alias_pair(
    environment: dict[str, str],
    canonical_name: str,
    legacy_name: str,
    path: Path,
) -> None:
    normalized = str(path)
    environment[canonical_name] = normalized
    environment[legacy_name] = normalized


def _require_explicit_legacy_migration(project_root: Path, runtime_home: Path) -> None:
    if runtime_home.exists():
        return
    legacy_candidates = (
        project_root / ".deer-flow",
        project_root / "backend" / ".deer-flow",
    )
    existing = [candidate for candidate in legacy_candidates if candidate.exists()]
    if not existing:
        return
    rendered = ", ".join(str(path) for path in existing)
    raise RuntimeHomeMigrationRequired(
        "legacy native runtime data exists at "
        f"{rendered}, while the canonical runtime home '{runtime_home}' is absent; "
        "run `make migrate-runtime-home` (dry-run by default) or explicitly set "
        "ACT_WEAVE_HOME/DEER_FLOW_HOME to the intended existing directory"
    )


def build_runtime_environment(
    env_file: Path,
    *,
    base_environment: dict[str, str] | None = None,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, str]:
    """Return the role environment without ambient model-provider keys."""

    environment = dict(os.environ if base_environment is None else base_environment)
    if env_file.is_file():
        for name, value in dotenv_values(env_file).items():
            if name not in MODEL_PROVIDER_ENV_NAMES and value is not None:
                environment.setdefault(name, value)

    for name in MODEL_PROVIDER_ENV_NAMES:
        environment.pop(name, None)

    cwd = Path.cwd()
    project_root = _resolve_alias_pair(
        environment,
        "ACT_WEAVE_PROJECT_ROOT",
        "DEER_FLOW_PROJECT_ROOT",
        default=repository_root,
        base=cwd,
    )
    assert project_root is not None
    if not project_root.is_dir():
        raise ValueError(f"local runtime project root is not a directory: {project_root}")
    _export_alias_pair(
        environment,
        "ACT_WEAVE_PROJECT_ROOT",
        "DEER_FLOW_PROJECT_ROOT",
        project_root,
    )

    home_was_configured = bool(environment.get("ACT_WEAVE_HOME") or environment.get("DEER_FLOW_HOME"))
    runtime_home = _resolve_alias_pair(
        environment,
        "ACT_WEAVE_HOME",
        "DEER_FLOW_HOME",
        default=project_root / ".act-weave",
        base=project_root,
    )
    assert runtime_home is not None
    if not home_was_configured:
        _require_explicit_legacy_migration(project_root, runtime_home)
    _export_alias_pair(environment, "ACT_WEAVE_HOME", "DEER_FLOW_HOME", runtime_home)

    config_path = _resolve_alias_pair(
        environment,
        "ACT_WEAVE_CONFIG_PATH",
        "DEER_FLOW_CONFIG_PATH",
        default=project_root / "config.yaml",
        base=project_root,
    )
    assert config_path is not None
    _export_alias_pair(
        environment,
        "ACT_WEAVE_CONFIG_PATH",
        "DEER_FLOW_CONFIG_PATH",
        config_path,
    )

    skills_path = _resolve_alias_pair(
        environment,
        "ACT_WEAVE_SKILLS_PATH",
        "DEER_FLOW_SKILLS_PATH",
        base=project_root,
    )
    if skills_path is not None:
        _export_alias_pair(
            environment,
            "ACT_WEAVE_SKILLS_PATH",
            "DEER_FLOW_SKILLS_PATH",
            skills_path,
        )
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a backend role with filtered repository environment settings.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPOSITORY_ROOT / ".env",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")

    try:
        environment = build_runtime_environment(args.env_file)
    except (RuntimeHomeMigrationRequired, ValueError) as exc:
        print(f"run_runtime: {exc}", file=sys.stderr)
        return 2
    os.execvpe(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
