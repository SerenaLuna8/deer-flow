from types import SimpleNamespace

from app.shared_assets.model_refs import ConfiguredModelRefResolver, resolve_model_ref
from deerflow.config.app_config import AppConfig


def _config(*model_names: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "models": [{"name": name, "use": "pkg:Model", "model": f"provider/{name}"} for name in model_names],
        }
    )


def test_default_model_ref_resolves_to_first_configured_logical_name() -> None:
    config = _config("primary-logical", "secondary-logical")

    assert resolve_model_ref(config, "default") is config.models[0]
    assert ConfiguredModelRefResolver(config).resolve("default") == "primary-logical"


def test_explicit_model_ref_requires_an_exact_logical_name() -> None:
    config = _config("primary-logical", "secondary-logical")
    resolver = ConfiguredModelRefResolver(config)

    assert resolve_model_ref(config, "secondary-logical") is config.models[1]
    assert resolver.resolve("secondary-logical") == "secondary-logical"
    assert resolve_model_ref(config, "provider/secondary-logical") is None
    assert resolver.resolve("missing-logical") is None


def test_default_model_ref_fails_closed_without_configured_models() -> None:
    config = _config()

    assert resolve_model_ref(config, "default") is None
    assert ConfiguredModelRefResolver(config).resolve("default") is None


def test_non_default_resolution_only_requires_the_config_lookup_contract() -> None:
    model = object()
    config = SimpleNamespace(get_model_config=lambda name: model if name == "exact" else None)

    assert resolve_model_ref(config, "exact") is model
    assert resolve_model_ref(config, "missing") is None
