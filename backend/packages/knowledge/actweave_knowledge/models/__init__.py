"""Internal model-access layer: the SiliconFlow-compatible HTTP client.

Model governance (providers, typed models, keys) lives in the host registry;
this package only turns host-materialized credentials into provider calls.
Nothing here is exported from the root public API; hosts reach the
functionality through :class:`actweave_knowledge.KnowledgeModule`.
"""

from .client import KnowledgeModelClient, RerankScore

__all__ = [
    "KnowledgeModelClient",
    "RerankScore",
]
