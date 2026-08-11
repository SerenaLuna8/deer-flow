from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channels.discord import DiscordChannel
from app.channels.message_bus import MessageBus
from app.gateway.routers import privacy_center
from deerflow.agents.lead_agent.prompt import SYSTEM_PROMPT_TEMPLATE
from deerflow.config.tracing_config import get_tracing_config, reset_tracing_config


class _PrivacyExportService:
    def __init__(self, _session) -> None:
        pass

    async def open_case_export(self, _user_id, _project_id, *, now):
        del now

        async def stream():
            yield b'{"record_type":"manifest"}\n'

        return stream()


class _FakeDiscordHTTPException(Exception):
    code = 0


class _FakeDiscordModule:
    class ChannelType:
        text = object()
        news = object()

    class errors:
        HTTPException = _FakeDiscordHTTPException


class _FakeDiscordMessage:
    def __init__(self) -> None:
        self.id = 42
        self.author = SimpleNamespace(display_name="Builder")
        self.channel = SimpleNamespace(type=_FakeDiscordModule.ChannelType.text)
        self.created_name: str | None = None

    async def create_thread(self, *, name: str):
        self.created_name = name
        return SimpleNamespace(id=7)


def test_lead_prompt_uses_actweave_in_public_research_examples() -> None:
    assert "ActWeave is an open-source AI agent system" in SYSTEM_PROMPT_TEMPLATE
    assert "[citation:Upstream Repository](https://github.com/bytedance/deer-flow)" in SYSTEM_PROMPT_TEMPLATE
    assert "[citation:Project Documentation](https://github.com/SerenaLuna8/deer-flow/tree/main/docs)" in SYSTEM_PROMPT_TEMPLATE
    assert "[Upstream Repository](https://github.com/bytedance/deer-flow) - Upstream source code" in SYSTEM_PROMPT_TEMPLATE
    assert "DeerFlow is an open-source" not in SYSTEM_PROMPT_TEMPLATE
    assert "DeerFlow Documentation" not in SYSTEM_PROMPT_TEMPLATE
    assert "deer-flow.dev" not in SYSTEM_PROMPT_TEMPLATE


def test_gateway_health_uses_public_actweave_service_name() -> None:
    source = (Path(__file__).resolve().parents[1] / "app/gateway/app.py").read_text(encoding="utf-8")

    assert '"service": "act-weave-gateway"' in source
    assert '"service": "deer-flow-gateway"' not in source


@pytest.mark.asyncio
async def test_privacy_download_filename_uses_actweave_brand(monkeypatch) -> None:
    project_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(privacy_center, "PrivacyCenterService", _PrivacyExportService)

    response = await privacy_center.export_privacy_case(
        project_id,
        user=SimpleNamespace(id="22222222-2222-4222-8222-222222222222"),
        session=object(),
    )

    assert response.headers["content-disposition"] == f'attachment; filename="act-weave-privacy-{project_id}.ndjson"'


def test_langsmith_default_project_uses_actweave_brand(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    reset_tracing_config()
    try:
        assert get_tracing_config().langsmith.project == "act-weave"
    finally:
        reset_tracing_config()


@pytest.mark.asyncio
async def test_discord_new_thread_uses_actweave_prefix_and_legacy_mapping_survives(tmp_path) -> None:
    channel_store = SimpleNamespace(_path=tmp_path / "channel_store.json")
    channel = DiscordChannel(MessageBus(), {"channel_store": channel_store})
    channel._discord_module = _FakeDiscordModule
    message = _FakeDiscordMessage()

    await channel._create_thread(message)

    assert message.created_name == "act-weave-Builder-42"

    channel._thread_store_path.write_text(json.dumps({"legacy-channel": "123456"}), encoding="utf-8")
    channel._load_active_threads()
    assert channel._active_threads == {"legacy-channel": "123456"}
    assert channel._active_thread_ids == {"123456"}
