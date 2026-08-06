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


def build_runtime_environment(
    env_file: Path,
    *,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the role environment without ambient model-provider keys."""

    environment = dict(os.environ if base_environment is None else base_environment)
    if env_file.is_file():
        for name, value in dotenv_values(env_file).items():
            if name not in MODEL_PROVIDER_ENV_NAMES and value is not None:
                environment.setdefault(name, value)

    for name in MODEL_PROVIDER_ENV_NAMES:
        environment.pop(name, None)
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

    os.execvpe(
        command[0],
        command,
        build_runtime_environment(args.env_file),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
