"""Unit tests for the Setup Wizard (scripts/wizard/).

Run from repo root:
    cd backend && uv run pytest tests/test_setup_wizard.py -v
"""

from __future__ import annotations

import yaml
from wizard import ui as wizard_ui
from wizard.providers import SEARCH_PROVIDERS, WEB_FETCH_PROVIDERS
from wizard.steps import channels as channels_step
from wizard.steps import llm as llm_step
from wizard.steps import search as search_step
from wizard.writer import (
    build_minimal_config,
    read_env_file,
    write_config_yaml,
    write_env_file,
)


class TestProviders:
    def test_search_providers_have_required_fields(self):
        for sp in SEARCH_PROVIDERS:
            assert sp.name
            assert sp.display_name
            assert sp.use
            assert ":" in sp.use

    def test_search_and_fetch_include_firecrawl(self):
        assert any(provider.name == "firecrawl" for provider in SEARCH_PROVIDERS)
        assert any(provider.name == "firecrawl" for provider in WEB_FETCH_PROVIDERS)

    def test_web_fetch_providers_have_required_fields(self):
        for provider in WEB_FETCH_PROVIDERS:
            assert provider.name
            assert provider.display_name
            assert provider.use
            assert ":" in provider.use
            assert provider.tool_name == "web_fetch"

    def test_at_least_one_free_search_provider(self):
        """At least one search provider needs no API key."""
        free = [sp for sp in SEARCH_PROVIDERS if sp.env_var is None]
        assert free, "Expected at least one free (no-key) search provider"

    def test_at_least_one_free_web_fetch_provider(self):
        free = [provider for provider in WEB_FETCH_PROVIDERS if provider.env_var is None]
        assert free, "Expected at least one free (no-key) web fetch provider"


class TestBuildMinimalConfig:
    def test_produces_valid_model_free_yaml(self):
        content = build_minimal_config()
        data = yaml.safe_load(content)
        assert data is not None
        assert content.startswith("# ActWeave Configuration\n")
        assert data["config_version"] == 35
        assert "models" not in data
        assert "OPENAI_API_KEY" not in content
        assert "langchain_openai:ChatOpenAI" not in content

    def test_removes_legacy_models_from_base_config(self):
        content = build_minimal_config(
            base_config={
                "config_version": 33,
                "models": [
                    {
                        "name": "legacy",
                        "use": "langchain_openai:ChatOpenAI",
                        "model": "gpt-4o",
                        "api_key": "$OPENAI_API_KEY",
                    }
                ],
            }
        )

        data = yaml.safe_load(content)
        assert "models" not in data
        assert "OPENAI_API_KEY" not in content

    def test_search_tool_included(self):
        content = build_minimal_config(
            search_use="deerflow.community.tavily.tools:web_search_tool",
            search_extra_config={"max_results": 5},
        )
        data = yaml.safe_load(content)
        search_tool = next(t for t in data.get("tools", []) if t["name"] == "web_search")
        assert search_tool["max_results"] == 5

    def test_web_fetch_tool_included(self):
        content = build_minimal_config(
            web_fetch_use="deerflow.community.jina_ai.tools:web_fetch_tool",
            web_fetch_extra_config={"timeout": 10},
        )
        data = yaml.safe_load(content)
        fetch_tool = next(t for t in data.get("tools", []) if t["name"] == "web_fetch")
        assert fetch_tool["timeout"] == 10

    def test_no_search_tool_when_not_configured(self):
        content = build_minimal_config()
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "web_search" not in tool_names
        assert "web_fetch" not in tool_names

    def test_sandbox_included(self):
        content = build_minimal_config()
        data = yaml.safe_load(content)
        assert "sandbox" in data
        assert "use" in data["sandbox"]
        assert data["sandbox"]["use"] == "deerflow.sandbox.local:LocalSandboxProvider"
        assert data["sandbox"]["allow_host_bash"] is False

    def test_bash_tool_disabled_by_default(self):
        content = build_minimal_config()
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "bash" not in tool_names

    def test_can_enable_container_sandbox_and_bash(self):
        content = build_minimal_config(
            sandbox_use="deerflow.community.aio_sandbox:AioSandboxProvider",
            include_bash_tool=True,
        )
        data = yaml.safe_load(content)
        assert data["sandbox"]["use"] == "deerflow.community.aio_sandbox:AioSandboxProvider"
        assert "allow_host_bash" not in data["sandbox"]
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "bash" in tool_names

    def test_can_disable_write_tools(self):
        content = build_minimal_config(
            include_write_tools=False,
        )
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "write_file" not in tool_names
        assert "str_replace" not in tool_names

    def test_config_version_present(self):
        content = build_minimal_config(config_version=34)
        data = yaml.safe_load(content)
        assert data["config_version"] == 34

    def test_can_enable_selected_channel_connections(self):
        content = build_minimal_config(
            channel_connection_providers=["feishu", "slack"],
        )

        data = yaml.safe_load(content)
        channel_connections = data["channel_connections"]

        assert channel_connections["enabled"] is True
        assert channel_connections["feishu"]["enabled"] is True
        assert channel_connections["slack"]["enabled"] is True
        assert channel_connections["telegram"]["enabled"] is False
        assert channel_connections["discord"]["enabled"] is False
        assert channel_connections["dingtalk"]["enabled"] is False
        assert channel_connections["wechat"]["enabled"] is False
        assert channel_connections["wecom"]["enabled"] is False

    def test_channel_connections_disabled_when_no_channels_selected(self):
        content = build_minimal_config(
            channel_connection_providers=[],
        )

        data = yaml.safe_load(content)
        channel_connections = data["channel_connections"]

        assert channel_connections["enabled"] is False
        assert all(not config["enabled"] for provider, config in channel_connections.items() if provider != "enabled")


class TestLLMStep:
    def test_defers_model_setup_to_database_backed_admin_page(self, monkeypatch):
        messages: list[str] = []
        monkeypatch.setattr(llm_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_info", messages.append)

        result = llm_step.run_llm_step()

        assert result.admin_path == "/admin/settings/models"
        assert any("PostgreSQL" in message for message in messages)
        assert any("/admin/settings/models" in message for message in messages)


class TestChannelsStep:
    def test_returns_selected_channel_keys(self, monkeypatch):
        monkeypatch.setattr(channels_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "print_success", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "ask_multi_choice", lambda *_args, **_kwargs: [0, 3, 6])

        result = channels_step.run_channels_step()

        assert result.enabled_providers == ["telegram", "feishu", "wecom"]

    def test_empty_selection_disables_channel_connections(self, monkeypatch):
        monkeypatch.setattr(channels_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "print_success", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(channels_step, "ask_multi_choice", lambda *_args, **_kwargs: [])

        result = channels_step.run_channels_step()

        assert result.enabled_providers == []


class TestWizardUi:
    def test_multi_choice_blank_requires_input_without_default(self, monkeypatch):
        answers = iter(["", "2"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert wizard_ui.ask_multi_choice("Pick", ["First", "Second"], default=None) == [1]

    def test_multi_choice_blank_accepts_empty_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "")

        assert wizard_ui.ask_multi_choice("Pick", ["First", "Second"], default=[]) == []


# ---------------------------------------------------------------------------
# writer.py — env file helpers
# ---------------------------------------------------------------------------


class TestEnvFileHelpers:
    def test_write_and_read_new_file(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_file(env_file, {"OPENAI_API_KEY": "sk-test123"})
        pairs = read_env_file(env_file)
        assert pairs["OPENAI_API_KEY"] == "sk-test123"

    def test_update_existing_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=old-key\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "new-key"})
        pairs = read_env_file(env_file)
        assert pairs["OPENAI_API_KEY"] == "new-key"
        # Should not duplicate
        content = env_file.read_text()
        assert content.count("OPENAI_API_KEY") == 1

    def test_preserve_existing_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TAVILY_API_KEY=tavily-val\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "sk-new"})
        pairs = read_env_file(env_file)
        assert pairs["TAVILY_API_KEY"] == "tavily-val"
        assert pairs["OPENAI_API_KEY"] == "sk-new"

    def test_preserve_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# My .env file\nOPENAI_API_KEY=old\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "new"})
        content = env_file.read_text()
        assert "# My .env file" in content

    def test_read_ignores_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nKEY=value\n")
        pairs = read_env_file(env_file)
        assert "# comment" not in pairs
        assert pairs["KEY"] == "value"


# ---------------------------------------------------------------------------
# writer.py — write_config_yaml
# ---------------------------------------------------------------------------


class TestWriteConfigYaml:
    def test_generated_config_loadable_by_appconfig(self, tmp_path):
        """The generated config.yaml must be parseable (basic YAML validity)."""

        config_path = tmp_path / "config.yaml"
        write_config_yaml(config_path)
        assert config_path.exists()
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "models" not in data

    def test_copies_example_defaults_for_unconfigured_sections(self, tmp_path):
        example_path = tmp_path / "config.example.yaml"
        example_path.write_text(
            yaml.safe_dump(
                {
                    "config_version": 5,
                    "log_level": "info",
                    "token_usage": {"enabled": True},
                    "tool_groups": [{"name": "web"}, {"name": "file:read"}, {"name": "file:write"}, {"name": "bash"}],
                    "tools": [
                        {
                            "name": "web_search",
                            "group": "web",
                            "use": "deerflow.community.ddg_search.tools:web_search_tool",
                            "max_results": 5,
                        },
                        {
                            "name": "web_fetch",
                            "group": "web",
                            "use": "deerflow.community.jina_ai.tools:web_fetch_tool",
                            "timeout": 10,
                        },
                        {
                            "name": "image_search",
                            "group": "web",
                            "use": "deerflow.community.image_search.tools:image_search_tool",
                            "max_results": 5,
                        },
                        {"name": "ls", "group": "file:read", "use": "deerflow.sandbox.tools:ls_tool"},
                        {"name": "write_file", "group": "file:write", "use": "deerflow.sandbox.tools:write_file_tool"},
                        {"name": "bash", "group": "bash", "use": "deerflow.sandbox.tools:bash_tool"},
                    ],
                    "sandbox": {
                        "use": "deerflow.sandbox.local:LocalSandboxProvider",
                        "allow_host_bash": False,
                    },
                    "summarization": {"summary_prompt": "deployment prompt"},
                    "tool_output": {"storage_subdir": ".custom-results"},
                },
                sort_keys=False,
            )
        )

        config_path = tmp_path / "config.yaml"
        write_config_yaml(config_path)
        with open(config_path) as f:
            data = yaml.safe_load(f)

        assert data["log_level"] == "info"
        assert "token_usage" not in data
        assert data["tool_groups"][0]["name"] == "web"
        assert data["summarization"]["summary_prompt"] == "deployment prompt"
        assert data["tool_output"]["storage_subdir"] == ".custom-results"
        assert any(tool["name"] == "image_search" and tool["max_results"] == 5 for tool in data["tools"])

    def test_config_version_read_from_example(self, tmp_path):
        """write_config_yaml should read config_version from config.example.yaml if present."""

        example_path = tmp_path / "config.example.yaml"
        example_path.write_text("config_version: 99\n")

        config_path = tmp_path / "config.yaml"
        write_config_yaml(config_path)
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert data["config_version"] == 99

    def test_removes_legacy_models_from_example_defaults(self, tmp_path):
        (tmp_path / "config.example.yaml").write_text(
            yaml.safe_dump(
                {
                    "config_version": 33,
                    "models": [
                        {
                            "name": "legacy",
                            "use": "langchain_openai:ChatOpenAI",
                            "model": "gpt-4o",
                            "api_key": "$OPENAI_API_KEY",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config_path = tmp_path / "config.yaml"
        write_config_yaml(config_path)

        with open(config_path) as f:
            data = yaml.safe_load(f)

        assert "models" not in data
        assert "OPENAI_API_KEY" not in config_path.read_text(encoding="utf-8")


class TestSearchStep:
    def test_reuses_api_key_for_same_provider(self, monkeypatch):
        monkeypatch.setattr(search_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(search_step, "print_success", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(search_step, "print_info", lambda *_args, **_kwargs: None)

        choices = iter([3, 1])
        prompts: list[str] = []

        def fake_choice(_prompt, _options, default=0):
            return next(choices)

        def fake_secret(prompt):
            prompts.append(prompt)
            return "shared-api-key"

        monkeypatch.setattr(search_step, "ask_choice", fake_choice)
        monkeypatch.setattr(search_step, "ask_secret", fake_secret)

        result = search_step.run_search_step()

        assert result.search_provider is not None
        assert result.fetch_provider is not None
        assert result.search_provider.name == "exa"
        assert result.fetch_provider.name == "exa"
        assert result.search_api_key == "shared-api-key"
        assert result.fetch_api_key == "shared-api-key"
        assert prompts == ["EXA_API_KEY"]
