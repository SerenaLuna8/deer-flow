"""Internal model-access layer: SiliconFlow client and configuration service.

Nothing in this package is exported from the root public API; hosts reach the
functionality through :class:`actweave_knowledge.KnowledgeModule`.
"""

from .client import KnowledgeModelClient, KnowledgeModelMaterial, RerankScore
from .service import KnowledgeModelConfigurationService, materialize_model_material

__all__ = [
    "KnowledgeModelClient",
    "KnowledgeModelConfigurationService",
    "KnowledgeModelMaterial",
    "RerankScore",
    "materialize_model_material",
]
