"""Knowledge startup configuration is projected from its database singleton."""

from app.knowledge_settings.service import load_knowledge_settings_from_db

__all__ = ["load_knowledge_settings_from_db"]
