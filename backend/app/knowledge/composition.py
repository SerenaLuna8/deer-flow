"""Composition root assembling the Knowledge Package onto host resources.

:func:`create_knowledge_module_from_app_config` is the single entry point for
Gateway and Worker once they wire the feature in (M2+); it must run after the
persistence engine is initialized. A disabled feature yields ``None`` and no
Knowledge resource is constructed.
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


class KnowledgeStorageNotReady(RuntimeError):
    """Startup failure: the enabled module cannot reach its MinIO bucket."""


async def require_knowledge_storage_ready(module: KnowledgeModule) -> None:
    """Fail fast at process startup when object storage is unreachable.

    An enabled Knowledge module without a reachable bucket is an operator
    misconfiguration (the bucket is provisioned by the administrator, never
    auto-created), so Gateway and Worker refuse to start instead of failing
    lazily on the first upload. The health message is locator-free.
    """

    health = await module.health()
    if not health.storage_ok:
        raise KnowledgeStorageNotReady(f"Knowledge 对象存储启动检查失败：{health.message}")
