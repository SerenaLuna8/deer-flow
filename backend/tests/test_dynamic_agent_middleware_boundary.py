"""Fail-closed boundary for main's dynamic configured Agent middleware loader."""

from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.gateway.routers.project_assets import AgentInstructionsRequest
from app.reliability.process_readiness import ProcessReadinessSnapshot
from app.shared_assets.models import AgentPayload
from deerflow.agents.lead_agent.agent import build_middlewares
from deerflow.config.app_config import AppConfig
from deerflow.persistence.shared_assets.agent_model import AgentVersionRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
UNSUPPORTED_CONFIG_KEYS = (
    "agent_middlewares",
    "configured_middlewares",
    "trusted_middlewares",
)
FORBIDDEN_AGENT_FIELD_FRAGMENTS = ("middleware", "module_path")


def _config_with(**extra: object) -> dict[str, object]:
    return {
        "sandbox": {
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
        },
        **extra,
    }


def _assert_no_dynamic_middleware_fields(names: set[str]) -> None:
    assert not {name for name in names if any(fragment in name.lower() for fragment in FORBIDDEN_AGENT_FIELD_FRAGMENTS)}


def test_legacy_extensions_middlewares_config_remains_fail_closed() -> None:
    with pytest.raises(
        ValidationError,
        match=r"LEGACY_CONFIG_REMOVED: extensions",
    ):
        AppConfig.model_validate(
            _config_with(
                extensions={
                    "middlewares": [
                        "untrusted.module:Middleware",
                    ]
                }
            )
        )


@pytest.mark.parametrize("config_key", UNSUPPORTED_CONFIG_KEYS)
def test_dynamic_middleware_config_aliases_are_rejected(config_key: str) -> None:
    with pytest.raises(
        ValidationError,
        match=rf"DYNAMIC_MIDDLEWARE_CONFIG_UNSUPPORTED: {config_key}",
    ):
        AppConfig.model_validate(
            _config_with(
                **{
                    config_key: [
                        "untrusted.module:Middleware",
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "field_name,value",
    (
        ("middlewares", ["untrusted.module:Middleware"]),
        ("middleware", "untrusted.module:Middleware"),
        ("middleware_class", "untrusted.module:Middleware"),
        ("module_path", "untrusted.module"),
    ),
)
def test_project_agent_instruction_api_rejects_dynamic_code_fields(
    field_name: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "agents_instructions": "",
        "soul": "Be precise.",
        "identity": "",
        "user_context": "",
        "expected_asset_version": 1,
        field_name: value,
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentInstructionsRequest.model_validate(payload)


def test_agent_version_payload_and_row_have_no_dynamic_code_fields() -> None:
    _assert_no_dynamic_middleware_fields({field.name for field in fields(AgentPayload)})
    _assert_no_dynamic_middleware_fields(set(AgentVersionRow.__table__.columns.keys()))
    _assert_no_dynamic_middleware_fields(set(AgentInstructionsRequest.model_fields))


def test_production_has_no_main_dynamic_configured_loader() -> None:
    assert not (BACKEND_ROOT / "packages/harness/deerflow/agents/middlewares/configured_extensions.py").exists()

    forbidden_tokens = (
        "load_configured_extension_middlewares",
        "extensions.middlewares",
        "configured_extensions",
    )
    production_roots = (
        BACKEND_ROOT / "app",
        BACKEND_ROOT / "packages/harness/deerflow",
    )
    matches: list[str] = []
    for root in production_roots:
        for source_path in root.rglob("*.py"):
            source = source_path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in source:
                    matches.append(f"{source_path.relative_to(BACKEND_ROOT)}:{token}")
    assert matches == []


def test_custom_middlewares_remain_explicit_code_side_injection() -> None:
    signature = inspect.signature(build_middlewares)
    assert signature.parameters["custom_middlewares"].default is None

    source = inspect.getsource(build_middlewares)
    assert "if custom_middlewares:" in source
    assert "middlewares.extend(custom_middlewares)" in source
    assert "custom_middlewares=resolved_app_config" not in source
    assert "custom_middlewares=app_config" not in source

    production_call_sites: list[str] = []
    for source_path in (BACKEND_ROOT / "packages/harness/deerflow").rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if "custom_middlewares=" in source:
            production_call_sites.append(str(source_path.relative_to(BACKEND_ROOT)))
    assert production_call_sites == [
        "packages/harness/deerflow/client.py",
    ]
    client_source = (BACKEND_ROOT / "packages/harness/deerflow/client.py").read_text(encoding="utf-8")
    assert "custom_middlewares=self._middlewares" in client_source


def test_process_readiness_exposes_no_dynamic_code_identifiers() -> None:
    _assert_no_dynamic_middleware_fields({field.name for field in fields(ProcessReadinessSnapshot)})
