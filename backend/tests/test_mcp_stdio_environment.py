from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.mcp_secret_store import _canonical_payload
from deerflow.mcp.client import build_server_params
from deerflow.mcp.config import McpServerConfig


def test_stdio_mcp_receives_a_fixed_environment_without_worker_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "worker-secret-must-not-cross")
    monkeypatch.setenv("HOME", "/private/worker-home")
    monkeypatch.setenv("PATH", "/private/worker-bin")

    params = build_server_params(
        "isolated",
        McpServerConfig(command="mcp", env={"PUBLIC_MODE": "read"}),
    )

    assert params["env"]["PUBLIC_MODE"] == "read"
    assert params["env"]["HOME"] == "/tmp"
    assert params["env"]["PATH"] != "/private/worker-bin"
    assert "ACT_WEAVE_SECRET_KEY" not in params["env"]


def test_stdio_mcp_rejects_runtime_host_environment_expansion() -> None:
    with pytest.raises(ValueError, match="cannot reference Worker host"):
        build_server_params(
            "expanding",
            McpServerConfig(
                command="mcp",
                env={"API_KEY": "${ACT_WEAVE_SECRET_KEY}"},
            ),
        )


def test_stdio_mcp_secret_slot_rejects_runtime_host_environment_expansion() -> None:
    slot = SimpleNamespace(payload_schema={"env": ["API_KEY"]})

    with pytest.raises(AssetValidationFailed):
        _canonical_payload(
            slot,
            {"env": {"API_KEY": "${ACT_WEAVE_SECRET_KEY}"}},
            request_id="mcp-host-env-expansion",
        )
