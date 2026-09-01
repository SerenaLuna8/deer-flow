#!/usr/bin/env python3
"""Run a backend role with the repository's filtered local environment.

Backend module commands are valid entry points on their own, so they cannot
depend on the root ``serve.sh`` having sourced ``.env`` first. Runtime roles
receive the existing filtered environment; the explicit database-upgrade mode
uses a smaller allowlist containing only ``DATABASE_URL`` and safe process
bootstrap variables.
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
INSTALLATION_ONLY_ENV_NAMES = frozenset(
    {
        # The public setup-db path consumes this once to create encrypted
        # model-owned copies. The internal reset helper is the nonpublic
        # development/test exception. Runtime roles never inherit plaintext.
        "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY",
        # The same public setup path encrypts this into the default Model
        # Provider row; the nonpublic reset helper may reuse that install flow.
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY",
        # The matching install-time skip switch is equally installation-only:
        # runtime behavior is governed by config.yaml, not bootstrap decisions.
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP",
        # Retired M8-era bootstrap names: still filtered so stale .env entries
        # never reach Gateway/Worker/Scheduler, but no bootstrap reads them.
        "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY",
        "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP",
        # The public setup path consumes this complete group to seed encrypted
        # Knowledge storage settings; the nonpublic reset helper is the
        # internal exception. Runtime reads PostgreSQL only.
        "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT",
        "ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET",
        "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY",
        "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY",
        # The public Make entry admitting this superuser connection is
        # setup-db; the internal reset helper remains a nonpublic exception.
        # Runtime and maintenance commands use the application DATABASE_URL.
        "POSTGRES_ADMIN_URL",
    }
)
RUNTIME_BLOCKED_ENV_NAMES = MODEL_PROVIDER_ENV_NAMES | INSTALLATION_ONLY_ENV_NAMES
DATABASE_UPGRADE_SAFE_ENV_NAMES = frozenset(
    {
        "COLORTERM",
        "COMSPEC",
        "DATABASE_URL",
        "FORCE_COLOR",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


def build_runtime_environment(
    env_file: Path,
    *,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the role environment without provider or installation credentials."""

    environment = dict(os.environ if base_environment is None else base_environment)
    if env_file.is_file():
        for name, value in dotenv_values(env_file).items():
            if name not in RUNTIME_BLOCKED_ENV_NAMES and value is not None:
                environment.setdefault(name, value)

    for name in RUNTIME_BLOCKED_ENV_NAMES:
        environment.pop(name, None)
    return environment


def build_database_upgrade_environment(
    env_file: Path,
    *,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal environment admitted to the schema upgrade process."""

    source = os.environ if base_environment is None else base_environment
    if os.name == "nt":
        environment = {name.upper(): value for name, value in source.items() if name.upper() in DATABASE_UPGRADE_SAFE_ENV_NAMES}
    else:
        environment = {name: value for name, value in source.items() if name in DATABASE_UPGRADE_SAFE_ENV_NAMES}
    if "DATABASE_URL" not in environment and env_file.is_file():
        database_url = dotenv_values(env_file).get("DATABASE_URL")
        if database_url is not None:
            environment["DATABASE_URL"] = database_url
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
    parser.add_argument(
        "--database-upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")

    if args.database_upgrade:
        environment = build_database_upgrade_environment(args.env_file)
    else:
        environment = build_runtime_environment(args.env_file)
    os.execvpe(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
