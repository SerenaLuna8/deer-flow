#!/usr/bin/env python3
"""ActWeave Health Check (make doctor).

Checks system requirements, configuration, the PostgreSQL model catalog, and
optional components, then prints an actionable report.

Exit codes:
  0 — all required checks passed (warnings allowed)
  1 — one or more required checks failed
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Literal


def _ensure_backend_import_path(project_root: Path) -> None:
    """Expose backend packages without relying on POSIX shell assignments."""
    backend_root = str(project_root / "backend")
    if backend_root in sys.path:
        sys.path.remove(backend_root)
    sys.path.insert(0, backend_root)


_ensure_backend_import_path(Path(__file__).resolve().parents[1])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Status = Literal["ok", "warn", "fail", "skip"]
PNPM_SCRIPT_PATH = Path(__file__).with_name("pnpm.py")
FRONTEND_DIR = PNPM_SCRIPT_PATH.parent.parent / "frontend"
COREPACK_NOTICE = "Using pnpm via Corepack."


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if _supports_color():
        return f"\033[{code}m{text}\033[0m"
    return text


def green(t: str) -> str:
    return _c(t, "32")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


def cyan(t: str) -> str:
    return _c(t, "36")


def bold(t: str) -> str:
    return _c(t, "1")


def _icon(status: Status) -> str:
    icons = {"ok": green("✓"), "warn": yellow("!"), "fail": red("✗"), "skip": "—"}
    return icons[status]


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return (r.stdout or r.stderr).strip()
    except Exception:
        return None


def _parse_major(version_text: str) -> int | None:
    v = version_text.lstrip("v").split(".", 1)[0]
    return int(v) if v.isdigit() else None


def _load_yaml_file(path: Path) -> dict:
    import yaml

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("top-level config must be a YAML mapping")
    return data


def _load_app_config(config_path: Path) -> object:
    from deerflow.config.app_config import AppConfig

    return AppConfig.from_file(str(config_path))


def _split_use_path(use: str) -> tuple[str, str] | None:
    if ":" not in use:
        return None
    module_name, attr_name = use.split(":", 1)
    if not module_name or not attr_name:
        return None
    return module_name, attr_name


# ---------------------------------------------------------------------------
# Check result container
# ---------------------------------------------------------------------------


class CheckResult:
    def __init__(
        self,
        label: str,
        status: Status,
        detail: str = "",
        fix: str | None = None,
    ) -> None:
        self.label = label
        self.status = status
        self.detail = detail
        self.fix = fix

    def print(self) -> None:
        icon = _icon(self.status)
        detail_str = f"  ({self.detail})" if self.detail else ""
        print(f"  {icon} {self.label}{detail_str}")
        if self.fix:
            for line in self.fix.splitlines():
                print(f"      {cyan('→')} {line}")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python() -> CheckResult:
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 12):
        return CheckResult("Python", "ok", version_str)
    return CheckResult(
        "Python",
        "fail",
        version_str,
        fix="Python 3.12+ required. Install from https://www.python.org/",
    )


def check_node() -> CheckResult:
    node = shutil.which("node")
    if not node:
        return CheckResult(
            "Node.js",
            "fail",
            fix="Install Node.js 22+: https://nodejs.org/",
        )
    out = _run(["node", "-v"]) or ""
    major = _parse_major(out)
    if major is None or major < 22:
        return CheckResult(
            "Node.js",
            "fail",
            out or "unknown version",
            fix="Node.js 22+ required. Install from https://nodejs.org/",
        )
    return CheckResult("Node.js", "ok", out.lstrip("v"))


def check_pnpm() -> CheckResult:
    try:
        result = subprocess.run(
            [sys.executable, str(PNPM_SCRIPT_PATH), "-v"],
            cwd=FRONTEND_DIR,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except OSError as exc:
        return CheckResult(
            "pnpm",
            "fail",
            f"Unable to run pnpm resolver: {exc}",
            fix="Install pnpm, or install Corepack and ensure it is on PATH",
        )

    stdout = (result.stdout or "").strip()
    stderr_lines = (result.stderr or "").splitlines()
    via_corepack = COREPACK_NOTICE in stderr_lines
    stderr = "\n".join(line for line in stderr_lines if line != COREPACK_NOTICE).strip()
    if result.returncode != 0:
        detail = "\n".join(part for part in (stderr, stdout) if part)
        return CheckResult(
            "pnpm",
            "fail",
            detail or f"pnpm resolver exited with status {result.returncode}",
            fix="Install pnpm, or install Corepack and ensure it is on PATH",
        )
    if not stdout:
        return CheckResult(
            "pnpm",
            "fail",
            stderr or "pnpm resolver returned no version",
            fix="Install pnpm, or install Corepack and ensure it is on PATH",
        )
    resolution_hint = " (via Corepack)" if via_corepack else ""
    return CheckResult("pnpm", "ok", f"{stdout}{resolution_hint}")


def check_uv() -> CheckResult:
    if not shutil.which("uv"):
        return CheckResult(
            "uv",
            "fail",
            fix="curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
    out = _run(["uv", "--version"]) or ""
    parts = out.split()
    version = parts[1] if len(parts) > 1 else out
    return CheckResult("uv", "ok", version)


def check_nginx() -> CheckResult:
    if shutil.which("nginx"):
        out = _run(["nginx", "-v"]) or ""
        version = out.split("/", 1)[-1] if "/" in out else out
        return CheckResult("nginx", "ok", version)
    return CheckResult(
        "nginx",
        "fail",
        fix=("macOS:   brew install nginx\nUbuntu:  sudo apt install nginx\nWindows: use WSL and install nginx there"),
    )


def check_config_exists(config_path: Path) -> CheckResult:
    if config_path.exists():
        return CheckResult("config.yaml found", "ok")
    return CheckResult(
        "config.yaml found",
        "fail",
        fix="Run 'make setup' to create it",
    )


def check_config_version(config_path: Path, project_root: Path) -> CheckResult:
    if not config_path.exists():
        return CheckResult("config.yaml version", "skip")

    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            user_data = yaml.safe_load(f) or {}
        user_ver = int(user_data.get("config_version", 0))
    except Exception as exc:
        return CheckResult("config.yaml version", "fail", str(exc))

    example_path = project_root / "config.example.yaml"
    if not example_path.exists():
        return CheckResult("config.yaml version", "skip", "config.example.yaml not found")

    try:
        import yaml

        with open(example_path, encoding="utf-8") as f:
            example_data = yaml.safe_load(f) or {}
        example_ver = int(example_data.get("config_version", 0))
    except Exception:
        return CheckResult("config.yaml version", "skip")

    if user_ver < example_ver:
        return CheckResult(
            "config.yaml version",
            "warn",
            f"v{user_ver} < v{example_ver} (latest)",
            fix="make config-upgrade",
        )
    if user_ver > example_ver:
        return CheckResult(
            "config.yaml version",
            "fail",
            f"v{user_ver} > v{example_ver} (supported)",
            fix="Use a checkout that supports this config.yaml version",
        )
    return CheckResult("config.yaml version", "ok", f"v{user_ver}")


def check_config_loadable(config_path: Path) -> CheckResult:
    if not config_path.exists():
        return CheckResult("config.yaml loadable", "skip")

    try:
        _load_app_config(config_path)
        return CheckResult("config.yaml loadable", "ok")
    except Exception as exc:
        return CheckResult(
            "config.yaml loadable",
            "fail",
            str(exc),
            fix="Run 'make setup' again, or compare with config.example.yaml",
        )


def _run_model_catalog_query(database_url: str) -> dict[str, int | bool]:
    """Return a closed, read-only model-catalog readiness summary."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from deerflow.config.database_config import DatabaseConfig

    async def _query() -> dict[str, int | bool]:
        engine = create_async_engine(
            DatabaseConfig(url=database_url).sqlalchemy_url,
            poolclass=NullPool,
        )
        try:
            async with engine.connect() as connection:
                table_exists = bool(await connection.scalar(text("SELECT to_regclass('system_model_configs') IS NOT NULL")))
                if not table_exists:
                    return {"table_exists": False, "active_count": 0}
                active_count = await connection.scalar(
                    text("SELECT count(*) FROM system_model_configs WHERE status = 'active'")
                )
                return {
                    "table_exists": True,
                    "active_count": int(active_count or 0),
                }
        finally:
            await engine.dispose()

    return asyncio.run(_query())


def check_model_catalog() -> CheckResult:
    """Check database-backed model readiness without reading YAML or secrets."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return CheckResult(
            "PostgreSQL model catalog",
            "skip",
            "DATABASE_URL not set",
        )

    try:
        readiness = _run_model_catalog_query(database_url)
    except Exception:
        return CheckResult(
            "PostgreSQL model catalog",
            "fail",
            "readiness unavailable",
            fix="Run `make check-db`, then retry `make doctor`.",
        )

    if readiness.get("table_exists") is not True:
        return CheckResult(
            "PostgreSQL model catalog",
            "fail",
            "catalog table missing",
            fix=("Use a new empty PostgreSQL database and run `make setup-db`; older schemas are not upgraded in place."),
        )

    active_count = readiness.get("active_count")
    if type(active_count) is not int or active_count < 1:
        return CheckResult(
            "PostgreSQL model catalog",
            "fail",
            "no active models",
            fix=("Start ActWeave, sign in as a system administrator, and open `/admin/settings/models` to configure and activate a model."),
        )

    return CheckResult(
        "PostgreSQL model catalog",
        "ok",
        f"{active_count} active model(s)",
    )


def check_web_search(config_path: Path) -> CheckResult:
    return check_web_tool(config_path, tool_name="web_search", label="web search configured")


def check_web_tool(config_path: Path, *, tool_name: str, label: str) -> CheckResult:
    """Warn (not fail) if a web capability is not configured."""
    if not config_path.exists():
        return CheckResult(label, "skip")

    try:
        from dotenv import load_dotenv

        env_path = config_path.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        data = _load_yaml_file(config_path)

        tool_entries = [t for t in data.get("tools", []) if t.get("name") == tool_name]
        if not tool_entries:
            return CheckResult(
                label,
                "warn",
                f"no {tool_name} tool in config",
                fix=f"Run 'make setup' to configure {tool_name}",
            )

        free_providers = {
            "web_search": {"ddg_search": "DuckDuckGo (no key needed)"},
            "web_fetch": {"jina_ai": "Jina AI Reader (no key needed)", "crawl4ai": "Crawl4AI (self-hosted, no key needed)"},
            "image_search": {"deerflow.community.image_search.tools": "DuckDuckGo Images (no key needed)"},
        }
        key_providers = {
            "web_search": {
                "tavily": "TAVILY_API_KEY",
                "infoquest": "INFOQUEST_API_KEY",
                "exa": "EXA_API_KEY",
                "firecrawl": "FIRECRAWL_API_KEY",
                "fastcrw": "CRW_API_KEY",
                "brave": "BRAVE_SEARCH_API_KEY",
                "serper": "SERPER_API_KEY",
            },
            "web_fetch": {
                "infoquest": "INFOQUEST_API_KEY",
                "exa": "EXA_API_KEY",
                "firecrawl": "FIRECRAWL_API_KEY",
                "fastcrw": "CRW_API_KEY",
            },
            "image_search": {
                "brave": "BRAVE_SEARCH_API_KEY",
                "infoquest": "INFOQUEST_API_KEY",
                "serper": "SERPER_API_KEY",
            },
            "web_capture": {
                "browserless": "BROWSERLESS_TOKEN",
            },
        }
        key_fields = {
            "web_capture": {
                "browserless": "token",
            },
        }

        def _configured_key_detail(tool: dict, default_var: str, key_field: str = "api_key") -> tuple[Status, str] | None:
            configured_key = tool.get(key_field)
            if isinstance(configured_key, str) and configured_key.strip():
                key = configured_key.strip()
                if key.startswith("$"):
                    env_name = key[1:]
                    val = os.environ.get(env_name)
                    if val and val.strip():
                        return ("ok", f"{env_name} set from config")
                    # The referenced var is unset; fall through to the default
                    # env var below, which tools use as a runtime fallback.
                else:
                    return ("warn", f"literal {key_field} set in config")

            val = os.environ.get(default_var)
            return ("ok", f"{default_var} set") if val and val.strip() else None

        def _browserless_self_hosted(tool: dict) -> bool:
            base_url = str(tool.get("base_url") or "http://localhost:3032").lower()
            return "browserless.io" not in base_url

        for tool in tool_entries:
            use = tool.get("use", "")
            for provider, detail in free_providers.get(tool_name, {}).items():
                if provider in use:
                    return CheckResult(label, "ok", detail)

        for tool in tool_entries:
            use = tool.get("use", "")
            for provider, var in key_providers.get(tool_name, {}).items():
                if provider in use:
                    key_field = key_fields.get(tool_name, {}).get(provider, "api_key")
                    key_status = _configured_key_detail(tool, var, key_field=key_field)
                    if key_status:
                        status, detail = key_status
                        if status == "warn":
                            return CheckResult(
                                label,
                                "warn",
                                f"{provider} ({detail})",
                                fix=f"Move the {key_field} to .env as {var}=<your-key> and reference it as ${var}",
                            )
                        return CheckResult(label, "ok", f"{provider} ({detail})")
                    if tool_name == "web_capture" and provider == "browserless" and _browserless_self_hosted(tool):
                        return CheckResult(label, "ok", "browserless (self-hosted, token optional)")
                    return CheckResult(
                        label,
                        "warn",
                        f"{provider} configured but {var} not set",
                        fix=f"Add {var}=<your-key> to .env, or run 'make setup'",
                    )

        for tool in tool_entries:
            use = tool.get("use", "")
            split = _split_use_path(use)
            if split is None:
                return CheckResult(
                    label,
                    "fail",
                    f"invalid use path: {use}",
                    fix="Use a valid module:path provider from config.example.yaml",
                )
            module_name, attr_name = split
            try:
                module = import_module(module_name)
                getattr(module, attr_name)
            except Exception as exc:
                return CheckResult(
                    label,
                    "fail",
                    f"provider import failed: {use} ({exc})",
                    fix="Install the provider dependency or pick a valid provider in `make setup`",
                )

        return CheckResult(label, "ok")
    except Exception as exc:
        return CheckResult(label, "warn", str(exc))


def check_web_fetch(config_path: Path) -> CheckResult:
    return check_web_tool(config_path, tool_name="web_fetch", label="web fetch configured")


def check_web_capture(config_path: Path) -> CheckResult:
    return check_web_tool(config_path, tool_name="web_capture", label="web capture configured")


def check_image_search(config_path: Path) -> CheckResult:
    return check_web_tool(config_path, tool_name="image_search", label="image search configured")


def check_frontend_env(project_root: Path) -> CheckResult:
    env_path = project_root / "frontend" / ".env"
    if env_path.exists():
        return CheckResult("frontend/.env found", "ok")
    return CheckResult(
        "frontend/.env found",
        "warn",
        fix="Run 'make setup' or copy frontend/.env.example to frontend/.env",
    )


def check_sandbox(config_path: Path) -> list[CheckResult]:
    if not config_path.exists():
        return [CheckResult("sandbox configured", "skip")]

    try:
        data = _load_yaml_file(config_path)
        sandbox = data.get("sandbox")
        if not isinstance(sandbox, dict):
            return [
                CheckResult(
                    "sandbox configured",
                    "fail",
                    "missing sandbox section",
                    fix="Run 'make setup' to choose an execution mode",
                )
            ]

        sandbox_use = sandbox.get("use", "")
        tools = data.get("tools", [])
        tool_names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        results: list[CheckResult] = []

        if "LocalSandboxProvider" in sandbox_use:
            results.append(CheckResult("sandbox configured", "ok", "Local sandbox"))
            has_bash_tool = "bash" in tool_names
            allow_host_bash = bool(sandbox.get("allow_host_bash", False))
            if has_bash_tool and not allow_host_bash:
                results.append(
                    CheckResult(
                        "bash compatibility",
                        "warn",
                        "bash tool configured but host bash is disabled",
                        fix="Enable host bash only in a fully trusted environment, or switch to container sandbox",
                    )
                )
            elif allow_host_bash:
                results.append(
                    CheckResult(
                        "bash compatibility",
                        "warn",
                        "host bash enabled on LocalSandboxProvider",
                        fix="Use container sandbox for stronger isolation when bash is required",
                    )
                )
        elif "AioSandboxProvider" in sandbox_use:
            results.append(CheckResult("sandbox configured", "ok", "Container sandbox"))
            if not sandbox.get("provisioner_url") and not (shutil.which("docker") or shutil.which("container")):
                results.append(
                    CheckResult(
                        "container runtime available",
                        "warn",
                        "no Docker/Apple Container runtime detected",
                        fix="Install Docker Desktop / Apple Container, or switch to local sandbox",
                    )
                )
        elif sandbox_use:
            results.append(CheckResult("sandbox configured", "ok", sandbox_use))
        else:
            results.append(
                CheckResult(
                    "sandbox configured",
                    "fail",
                    "sandbox.use is empty",
                    fix="Run 'make setup' to choose an execution mode",
                )
            )
        return results
    except Exception as exc:
        return [CheckResult("sandbox configured", "fail", str(exc))]


def check_env_file(project_root: Path) -> CheckResult:
    env_path = project_root / ".env"
    if env_path.exists():
        return CheckResult(".env found", "ok")
    return CheckResult(
        ".env found",
        "warn",
        fix="Run 'make setup' or create .env with the required environment variables",
    )


def _run_postgres_check(_project_root: Path, database_url: str) -> dict:
    """Delegate all database SQL/revision checks to the backend read-only checker."""
    from scripts.check_postgres import run_check

    result = run_check(database_url)
    return {**asdict(result), "healthy": result.healthy}


def check_postgres(project_root: Path) -> CheckResult:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return CheckResult(
            "PostgreSQL",
            "fail",
            "DATABASE_URL 未设置",
            fix="设置 DATABASE_URL，运行 make setup-db，然后运行 make check-db",
        )
    try:
        result = _run_postgres_check(project_root, database_url)
    except Exception:
        return CheckResult(
            "PostgreSQL",
            "fail",
            "只读健康检查失败",
            fix="运行 make check-db 查看脱敏状态；旧 revision 或未知非空库必须创建全新的空数据库",
        )
    if not result.get("healthy"):
        return CheckResult(
            "PostgreSQL",
            "fail",
            "连接、Schema marker 或必需表检查未通过",
            fix="运行 make check-db 查看脱敏状态；旧 revision 或未知非空库必须创建全新的空数据库并运行 make setup-db",
        )
    detail = f"{result['host']}:{result['port']}/{result['database']}, revision {result['current_revision']}"
    return CheckResult("PostgreSQL", "ok", detail)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    config_path = project_root / "config.yaml"

    # Load .env early so key checks work
    try:
        from dotenv import load_dotenv

        env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass

    print()
    print(bold("ActWeave Health Check"))
    print("═" * 40)

    sections: list[tuple[str, list[CheckResult]]] = []

    # ── System Requirements ────────────────────────────────────────────────────
    sys_checks = [
        check_python(),
        check_node(),
        check_pnpm(),
        check_uv(),
        check_nginx(),
    ]
    sections.append(("System Requirements", sys_checks))

    # ── Configuration ─────────────────────────────────────────────────────────
    cfg_checks: list[CheckResult] = [
        check_env_file(project_root),
        check_frontend_env(project_root),
        check_config_exists(config_path),
        check_config_version(config_path, project_root),
        check_config_loadable(config_path),
    ]
    sections.append(("Configuration", cfg_checks))

    # ── Database ──────────────────────────────────────────────────────────────
    sections.append(
        (
            "Database",
            [
                check_postgres(project_root),
                check_model_catalog(),
            ],
        )
    )

    # ── Web Capabilities ─────────────────────────────────────────────────────
    search_checks = [
        check_web_search(config_path),
        check_web_fetch(config_path),
        check_web_capture(config_path),
        check_image_search(config_path),
    ]
    sections.append(("Web Capabilities", search_checks))

    # ── Sandbox ──────────────────────────────────────────────────────────────
    sandbox_checks = check_sandbox(config_path)
    sections.append(("Sandbox", sandbox_checks))

    # ── Render ────────────────────────────────────────────────────────────────
    total_fails = 0
    total_warns = 0

    for section_title, checks in sections:
        print()
        print(bold(section_title))
        for cr in checks:
            cr.print()
            if cr.status == "fail":
                total_fails += 1
            elif cr.status == "warn":
                total_warns += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("═" * 40)
    if total_fails == 0 and total_warns == 0:
        print(f"Status: {green('Ready')}")
        print(f"Run {cyan('make dev')} to start ActWeave")
    elif total_fails == 0:
        print(f"Status: {yellow(f'Ready ({total_warns} warning(s))')}")
        print(f"Run {cyan('make dev')} to start ActWeave")
    else:
        print(f"Status: {red(f'{total_fails} error(s), {total_warns} warning(s)')}")
        print("Fix the errors above, then run 'make doctor' again.")

    print()
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
