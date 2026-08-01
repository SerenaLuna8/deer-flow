from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_TEMPLATES = REPO_ROOT / "deploy" / "helm" / "deer-flow" / "templates"
MODEL_PROVIDER_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "MIMO_API_KEY",
    "MINIMAX_API_KEY",
    "MOONSHOT_API_KEY",
    "NOVITA_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "STEPFUN_API_KEY",
    "VLLM_API_KEY",
    "VOLCENGINE_API_KEY",
)

BACKEND_SECRET_NAMES = {
    "AUTH_JWT_SECRET",
    "BETTER_AUTH_SECRET",
    "DATABASE_URL",
    "DEER_FLOW_AUDIT_ACTIVE_KEY_ID",
    "DEER_FLOW_AUDIT_KEYRING_JSON",
    "DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID",
    "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
    "DEER_FLOW_INTERNAL_AUTH_TOKEN",
    "DEER_FLOW_PROXY_AUTH_TOKEN",
    "PROVISIONER_API_KEY",
}


def _compose_environment_names(service: dict) -> set[str]:
    names: set[str] = set()
    for item in service.get("environment", []):
        if isinstance(item, str):
            names.add(item.split("=", 1)[0])
    return names


def test_compose_does_not_broadcast_root_env_to_backend_roles() -> None:
    for filename in ("docker-compose.yaml", "docker-compose-dev.yaml"):
        compose = yaml.safe_load((REPO_ROOT / "docker" / filename).read_text(encoding="utf-8"))
        services = compose["services"]

        for component in ("gateway", "worker", "scheduler"):
            service = services[component]
            assert "../.env" not in service.get("env_file", [])
            assert BACKEND_SECRET_NAMES <= _compose_environment_names(service)

        assert "../.env" not in services["provisioner"].get("env_file", [])


def test_compose_has_no_ambient_model_provider_keys() -> None:
    source = "\n".join((REPO_ROOT / "docker" / filename).read_text(encoding="utf-8") for filename in ("docker-compose.yaml", "docker-compose-dev.yaml"))

    for variable in MODEL_PROVIDER_ENV_NAMES:
        assert variable not in source


def test_chart_does_not_render_a_provider_secret_into_runtime_roles() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(CHART_TEMPLATES.glob("*.yaml")))
    helpers = (CHART_TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")

    assert ".Values.secrets" not in source
    assert ".Values.existingSecret" not in source
    assert "deer-flow.providerSecret" not in f"{source}\n{helpers}"
    assert not (CHART_TEMPLATES / "secret-provider.yaml").exists()

    for filename in (
        "gateway-deployment.yaml",
        "worker-deployment.yaml",
        "scheduler-deployment.yaml",
    ):
        deployment = (CHART_TEMPLATES / filename).read_text(encoding="utf-8")
        assert "DEER_FLOW_CREDENTIAL_KEYRING_JSON" in (helpers), "Credential keyring must remain in the explicit backend secret contract"
        assert "envFrom:" not in deployment


def test_chart_configmap_rejects_retired_yaml_models() -> None:
    source = (CHART_TEMPLATES / "configmap-config.yaml").read_text(encoding="utf-8")

    assert 'hasKey $appConfig "models"' in source
    assert "models are stored in PostgreSQL" in source


def test_chart_values_do_not_offer_ambient_provider_secrets_or_yaml_models() -> None:
    values_path = REPO_ROOT / "deploy" / "helm" / "deer-flow" / "values.yaml"
    values_source = values_path.read_text(encoding="utf-8")
    values = yaml.safe_load(values_source)

    assert "secrets" not in values
    assert "existingSecret" not in values
    assert "models:" not in values["config"]
    for variable in MODEL_PROVIDER_ENV_NAMES:
        assert variable not in values_source


def test_setup_wizard_does_not_embed_a_model_provider_catalog() -> None:
    providers_source = (REPO_ROOT / "scripts" / "wizard" / "providers.py").read_text(encoding="utf-8")

    assert "LLMProvider" not in providers_source
    assert "LLM_PROVIDERS" not in providers_source
    for variable in MODEL_PROVIDER_ENV_NAMES:
        assert variable not in providers_source


def test_local_launcher_does_not_broadcast_model_provider_keys() -> None:
    source = (REPO_ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")

    for variable in MODEL_PROVIDER_ENV_NAMES:
        assert f'unset "{variable}"' in source


def test_backend_module_runtime_commands_use_filtered_root_environment_entry() -> None:
    for target in ("dev", "gateway", "worker", "scheduler", "check-db"):
        completed = subprocess.run(
            ["make", "-n", target],
            cwd=REPO_ROOT / "backend",
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert "scripts/run_runtime.py --" in completed.stdout
        assert "--env-file" not in completed.stdout


def test_filtered_runtime_environment_loads_safe_settings_and_drops_provider_keys(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://from-file@localhost/deerflow",
                "AUTH_JWT_SECRET=from-file",
                *(f"{name}=provider-from-file" for name in MODEL_PROVIDER_ENV_NAMES),
            ]
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["AUTH_JWT_SECRET"] = "explicit-process-value"
    environment.update({name: "provider-from-process" for name in MODEL_PROVIDER_ENV_NAMES})
    probe = f'import json, os; print(json.dumps({{"database": os.getenv("DATABASE_URL"), "auth": os.getenv("AUTH_JWT_SECRET"), "providers": [name for name in {MODEL_PROVIDER_ENV_NAMES!r} if name in os.environ]}}))'

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_runtime.py",
            "--env-file",
            str(env_file),
            "--",
            sys.executable,
            "-c",
            probe,
        ],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "database": "postgresql://from-file@localhost/deerflow",
        "auth": "explicit-process-value",
        "providers": [],
    }


@pytest.mark.parametrize(
    "runtime_import",
    [
        "import deerflow.config.app_config",
        ("from app.gateway.auth import config as auth_config; auth_config._auth_config = None; auth_config.get_auth_config()"),
    ],
)
def test_runtime_config_imports_do_not_reinject_dotenv_provider_keys(
    tmp_path: Path,
    runtime_import: str,
) -> None:
    """A runtime import must not undo serve.sh's provider-key cleanup."""

    (tmp_path / "sitecustomize.py").write_text(
        "\n".join(
            [
                "import os",
                "import dotenv",
                "",
                "def _reinject_root_dotenv(*_args, **_kwargs):",
                '    os.environ["DEEPSEEK_API_KEY"] = "must-not-reappear"',
                "    return True",
                "",
                "dotenv.load_dotenv = _reinject_root_dotenv",
            ]
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("DEEPSEEK_API_KEY", None)
    environment["AUTH_JWT_SECRET"] = "runtime-import-test-secret"
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(tmp_path),
            str(REPO_ROOT / "backend"),
            environment.get("PYTHONPATH", ""),
        )
    )
    script = f'import os; os.environ.pop("DEEPSEEK_API_KEY", None); {runtime_import}; assert "DEEPSEEK_API_KEY" not in os.environ'

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_pytest_collection_bootstraps_explicit_test_database_environment() -> None:
    """Collection must not depend on a repository dotenv side effect."""

    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["PYTEST_ADDOPTS"] = ""

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_automation_app_wiring.py",
        ],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


def test_active_install_guide_requires_database_backed_model_setup() -> None:
    source = (REPO_ROOT / "Install.md").read_text(encoding="utf-8")

    assert "/admin/settings/models" in source
    assert "entry under `models` in `config.yaml`" not in source
    assert "missing model entries" not in source
