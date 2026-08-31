"""Read the optional ``knowledge`` block from the root ``config.yaml``.

The block is startup configuration validated by the Knowledge Package's
:class:`~actweave_knowledge.KnowledgeSettings`; it never enters System Runtime
Settings. A missing block keeps the feature disabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from actweave_knowledge import KnowledgeSettings


def load_knowledge_settings(app_config: Any) -> KnowledgeSettings:
    """Validate ``AppConfig.model_extra['knowledge']`` into ``KnowledgeSettings``."""

    extra = getattr(app_config, "model_extra", None) or {}
    raw = extra.get("knowledge")
    if raw is None:
        return KnowledgeSettings()
    if not isinstance(raw, Mapping):
        raise ValueError("knowledge configuration must be a mapping")
    return KnowledgeSettings.model_validate(dict(raw))
