from __future__ import annotations

import os
from collections import UserDict
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import deerflow.config.app_config as app_config_module
from deerflow.config.acp_config import load_acp_config_from_dict
from deerflow.config.app_config import AppConfig, get_app_config, reset_app_config
from deerflow.config.guardrails_config import get_guardrails_config, load_guardrails_config_from_dict
from deerflow.config.memory_config import load_memory_config_from_dict
from deerflow.config.subagents_config import get_subagents_app_config, load_subagents_config_from_dict
from deerflow.config.summarization_config import get_summarization_config, load_summarization_config_from_dict
from deerflow.config.title_config import get_title_config, load_title_config_from_dict
from deerflow.config.tool_search_config import load_tool_search_config_from_dict
from deerflow.runtime.checkpointer import reset_checkpointer
from deerflow.runtime.store import reset_store


class _ConfigMapping(Mapping[str, object]):
    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _reset_config_singletons() -> None:
    load_title_config_from_dict({})
    load_summarization_config_from_dict({})
    load_memory_config_from_dict({})
    load_subagents_config_from_dict({})
    load_tool_search_config_from_dict({})
    load_guardrails_config_from_dict({})
    load_acp_config_from_dict({})
    reset_checkpointer()
    reset_store()
    reset_app_config()


def _write_config(path: Path, *, log_level: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                "log_level": log_level,
            }
        ),
        encoding="utf-8",
    )


def _write_config_with_sections(path: Path, sections: dict | None = None) -> None:
    config = {
        "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    }
    if sections:
        config.update(sections)

    path.write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.mark.parametrize("mapping_type", [UserDict, _ConfigMapping])
def test_app_config_rejects_checkpointer_from_any_mapping(mapping_type):
    data = mapping_type(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "database": {"url": "postgresql://localhost/deerflow"},
            "checkpointer": {"type": "postgres", "connection_string": "postgresql://localhost/other"},
        }
    )

    with pytest.raises(ValidationError, match="checkpointer.*removed|removed.*checkpointer"):
        AppConfig.model_validate(data)


def test_app_config_defaults_missing_database_url_from_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, log_level="info")

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/deerflow")

    config = AppConfig.from_file(str(config_path))

    assert config.database.url == "postgresql://localhost/deerflow"


def test_app_config_defaults_empty_database_url_from_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "database": {},
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/deerflow")

    config = AppConfig.from_file(str(config_path))

    assert config.database.url == "postgresql://localhost/deerflow"


def test_app_config_coerces_commented_out_list_sections(tmp_path, monkeypatch):
    """Commenting out every entry under a list key makes PyYAML parse it as None.

    Regression for the documented ``cp config.example.yaml config.yaml`` flow
    (issue #1444): such a config must load with empty lists instead of raising
    ``Input should be a valid list``.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                "tools": None,
                "tool_groups": None,
            }
        ),
        encoding="utf-8",
    )

    config = AppConfig.from_file(str(config_path))

    assert config.tools == []
    assert config.tool_groups == []


def test_app_config_coerces_commented_out_object_sections(tmp_path, monkeypatch):
    """Commenting out every entry under an object key makes PyYAML parse it as None.

    Same documented ``cp config.example.yaml config.yaml`` flow as the list
    sections: deployment-owned object sections must fall back to
    their defaults instead of raising ``Input should be a valid dictionary``.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                "guardrails": None,
                "logging": None,
                "circuit_breaker": None,
            }
        ),
        encoding="utf-8",
    )

    config = AppConfig.from_file(str(config_path))

    # Each present-but-null object section falls back to a real default config
    # object of the expected type (not merely non-None).
    assert type(config.guardrails).__name__ == "GuardrailsConfig"
    assert type(config.logging).__name__ == "LoggingConfig"
    assert type(config.circuit_breaker).__name__ == "CircuitBreakerConfig"


def test_app_config_null_required_section_still_errors(tmp_path, monkeypatch):
    """A present-but-null *required* section still errors.

    ``sandbox`` has no default, so dropping a ``sandbox: null`` key leaves the
    required field absent — there is nothing to fall back to (per
    ``_drop_null_config_sections``), unlike the optional object sections above.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"sandbox": None}), encoding="utf-8")

    with pytest.raises(ValidationError):
        AppConfig.from_file(str(config_path))


def test_app_config_loads_without_yaml_models(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            }
        ),
        encoding="utf-8",
    )

    config = AppConfig.from_file(str(config_path))

    assert config.models == []


def test_get_app_config_reloads_when_file_changes(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, log_level="info")

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_path))
    reset_app_config()

    try:
        initial = get_app_config()
        assert initial.log_level == "info"

        _write_config(config_path, log_level="warning")
        next_mtime = config_path.stat().st_mtime + 5
        os.utime(config_path, (next_mtime, next_mtime))

        reloaded = get_app_config()
        assert reloaded.log_level == "warning"
        assert reloaded is not initial
    finally:
        reset_app_config()


def test_get_app_config_reloads_when_content_digest_changes_without_metadata(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, log_level="info")

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_path))
    _reset_config_singletons()

    try:
        initial = get_app_config()
        initial_mtime = app_config_module._app_config_mtime
        initial_signature = app_config_module._app_config_signature
        assert initial.log_level == "info"
        assert initial_signature is not None

        _write_config(config_path, log_level="warning")

        real_get_config_signature = app_config_module._get_config_signature

        def stale_metadata_signature(path: Path):
            current_signature = real_get_config_signature(path)
            assert current_signature is not None
            return (initial_signature[0], initial_signature[1], current_signature[2])

        monkeypatch.setattr(app_config_module, "_get_config_mtime", lambda _path: initial_mtime)
        monkeypatch.setattr(app_config_module, "_get_config_signature", stale_metadata_signature)

        reloaded = get_app_config()
        assert reloaded.log_level == "warning"
        assert reloaded is not initial
        assert app_config_module._app_config_signature is not None
        assert app_config_module._app_config_signature[:2] == initial_signature[:2]
        assert app_config_module._app_config_signature[2] != initial_signature[2]
    finally:
        _reset_config_singletons()


def test_get_app_config_reloads_when_config_path_changes(tmp_path, monkeypatch):
    config_a = tmp_path / "config-a.yaml"
    config_b = tmp_path / "config-b.yaml"
    _write_config(config_a, log_level="info")
    _write_config(config_b, log_level="warning")

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_a))
    reset_app_config()

    try:
        first = get_app_config()
        assert first.log_level == "info"

        monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_b))
        second = get_app_config()
        assert second.log_level == "warning"
        assert second is not first
    finally:
        reset_app_config()


def test_get_app_config_resets_singleton_configs_when_sections_removed(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config_with_sections(
        config_path,
        {
            "title": {"prompt_template": "Custom title: {conversation}"},
            "summarization": {"summary_prompt": "Custom summary: {messages}"},
            "subagents": {"timeout_seconds": 42, "agents": {"reviewer": {"max_turns": 2}}},
            "guardrails": {"enabled": True, "fail_closed": False},
        },
    )

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_path))
    reset_app_config()

    try:
        get_app_config()
        assert get_title_config().prompt_template == "Custom title: {conversation}"
        assert get_summarization_config().summary_prompt == "Custom summary: {messages}"
        assert get_subagents_app_config().timeout_seconds == 42
        assert get_guardrails_config().enabled is True

        _write_config_with_sections(config_path)
        next_mtime = config_path.stat().st_mtime + 5
        os.utime(config_path, (next_mtime, next_mtime))

        get_app_config()
        assert get_title_config().enabled is True
        assert get_summarization_config().enabled is False
        assert get_subagents_app_config().timeout_seconds == 1800
        assert get_guardrails_config().enabled is False
    finally:
        _reset_config_singletons()


def test_get_app_config_rejects_removed_checkpointer_section(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config_with_sections(
        config_path,
        {"checkpointer": {"type": "postgres", "connection_string": "postgresql://localhost/other"}},
    )

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_path))
    reset_app_config()

    try:
        with pytest.raises(ValidationError, match="checkpointer.*removed|removed.*checkpointer"):
            get_app_config()
    finally:
        _reset_config_singletons()


def test_get_app_config_does_not_mutate_singletons_when_reload_validation_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    _write_config_with_sections(
        config_path,
        {
            "title": {"prompt_template": "Custom title: {conversation}"},
            "guardrails": {"enabled": True},
        },
    )

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(config_path))
    _reset_config_singletons()

    try:
        previous_app_config = get_app_config()

        _write_config_with_sections(
            config_path,
            {
                "title": False,
                "guardrails": False,
            },
        )
        next_mtime = config_path.stat().st_mtime + 5
        os.utime(config_path, (next_mtime, next_mtime))

        with pytest.raises(ValidationError):
            get_app_config()

        assert app_config_module._app_config is previous_app_config
        assert get_title_config().prompt_template == "Custom title: {conversation}"
        assert get_guardrails_config().enabled is True
    finally:
        _reset_config_singletons()
