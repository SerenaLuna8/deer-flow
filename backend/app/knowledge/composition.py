"""Composition root assembling the Knowledge Package onto host resources.

Gateway and Worker call :func:`create_knowledge_module_from_app_config` after
the persistence engine is initialized. A disabled feature yields ``None`` and
no Knowledge resource is constructed.
"""

from __future__ import annotations

from typing import Any

from actweave_knowledge import KnowledgeModule, create_knowledge_module

from app.knowledge.config import load_knowledge_settings
from app.knowledge.secret_adapter import EnvelopeKnowledgeSecretAdapter


def create_knowledge_module_from_app_config(app_config: Any) -> KnowledgeModule | None:
    """Build the module when enabled; return ``None`` when the feature is off."""

    settings = load_knowledge_settings(app_config)
    if not settings.enabled:
        return None

    from deerflow.persistence.engine import get_session_factory

    return create_knowledge_module(
        settings=settings,
        session_factory=get_session_factory(),
        secret_port=EnvelopeKnowledgeSecretAdapter.from_environment(),
    )
