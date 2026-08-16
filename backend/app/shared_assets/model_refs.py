from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

DEFAULT_MODEL_REF = "default"


class ModelRefResolver(Protocol):
    """Resolve an Agent model reference to one exact configured model UUID."""

    def resolve(self, model_ref: str) -> str | None: ...


def exact_model_ref(value: object) -> str | None:
    """Return one canonical model UUID string or ``None``.

    Provider model IDs and display names are deliberately not accepted as
    catalog authority.
    """

    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if value == canonical else None


def resolve_model_ref(config: Any, model_ref: str) -> ModelConfig | Any | None:
    """Resolve ``default`` or one exact System Model UUID.

    ``default`` follows the catalog contract: the first configured model is the
    default. Provider model IDs and display names are never used as references.
    """

    if not isinstance(model_ref, str) or not model_ref:
        return None
    if model_ref == DEFAULT_MODEL_REF:
        models = getattr(config, "models", ())
        return models[0] if models else None
    lookup = getattr(config, "get_model_config", None)
    exact_ref = exact_model_ref(model_ref)
    if not callable(lookup) or exact_ref is None:
        return None
    return lookup(exact_ref)


@dataclass(frozen=True, slots=True)
class ConfiguredModelRefResolver:
    """Secret-safe adapter used by every production Run admission path."""

    config: AppConfig = field(repr=False)

    def resolve(self, model_ref: str) -> str | None:
        model = resolve_model_ref(self.config, model_ref)
        exact_ref = getattr(model, "name", None)
        return exact_model_ref(exact_ref)


class ExactModelRefResolver:
    """Exact-only fallback for isolated repository tests and migrations.

    Production admission wiring must inject :class:`ConfiguredModelRefResolver`.
    The reserved ``default`` alias fails closed without an explicit catalog.
    """

    def resolve(self, model_ref: str) -> str | None:
        if model_ref == DEFAULT_MODEL_REF:
            return None
        return exact_model_ref(model_ref)


__all__ = [
    "ConfiguredModelRefResolver",
    "DEFAULT_MODEL_REF",
    "ExactModelRefResolver",
    "ModelRefResolver",
    "exact_model_ref",
    "resolve_model_ref",
]
