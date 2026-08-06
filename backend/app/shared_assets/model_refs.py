from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

DEFAULT_MODEL_REF = "default"


class ModelRefResolver(Protocol):
    """Resolve an Agent model reference to one exact configured logical name."""

    def resolve(self, model_ref: str) -> str | None: ...


def resolve_model_ref(config: Any, model_ref: str) -> ModelConfig | Any | None:
    """Resolve the stable ``default`` alias or an exact logical model name.

    ``default`` follows ActWeave's existing model-selection contract: the first
    configured logical model is the default. Provider model identifiers are
    deliberately ignored; Agent and Run records only carry logical names.
    """

    if not isinstance(model_ref, str) or not model_ref:
        return None
    if model_ref == DEFAULT_MODEL_REF:
        models = getattr(config, "models", ())
        return models[0] if models else None
    lookup = getattr(config, "get_model_config", None)
    if not callable(lookup):
        return None
    return lookup(model_ref)


@dataclass(frozen=True, slots=True)
class ConfiguredModelRefResolver:
    """Secret-safe adapter used by every production Run admission path."""

    config: AppConfig = field(repr=False)

    def resolve(self, model_ref: str) -> str | None:
        model = resolve_model_ref(self.config, model_ref)
        logical_name = getattr(model, "name", None)
        return logical_name if isinstance(logical_name, str) and logical_name else None


class ExactModelRefResolver:
    """Exact-only fallback for isolated repository tests and migrations.

    Production admission wiring must inject :class:`ConfiguredModelRefResolver`.
    The reserved ``default`` alias fails closed without an explicit catalog.
    """

    def resolve(self, model_ref: str) -> str | None:
        if not isinstance(model_ref, str) or not model_ref or model_ref == DEFAULT_MODEL_REF:
            return None
        return model_ref


__all__ = [
    "ConfiguredModelRefResolver",
    "DEFAULT_MODEL_REF",
    "ExactModelRefResolver",
    "ModelRefResolver",
    "resolve_model_ref",
]
