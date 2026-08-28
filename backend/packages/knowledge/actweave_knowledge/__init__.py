"""ActWeave Knowledge Package public API.

Host adapters must import only from this root module. ORM models,
repositories, the MinIO object store, and provider clients are internal.
"""

from .contracts import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeCitation,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeHealth,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeModelConfigurationView,
    KnowledgeModelConnectionResult,
    KnowledgeModelOption,
    KnowledgeProtectedSecret,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSecretPort,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from .module import KnowledgeModule, create_knowledge_module

__all__ = [
    "KnowledgeBaseCreate",
    "KnowledgeBaseUpdate",
    "KnowledgeBaseView",
    "KnowledgeCitation",
    "KnowledgeDocumentUpload",
    "KnowledgeDocumentView",
    "KnowledgeError",
    "KnowledgeHealth",
    "KnowledgeModelConfigurationCreate",
    "KnowledgeModelConfigurationUpdate",
    "KnowledgeModelConfigurationView",
    "KnowledgeModelConnectionResult",
    "KnowledgeModelOption",
    "KnowledgeModule",
    "KnowledgeProtectedSecret",
    "KnowledgeSearchRequest",
    "KnowledgeSearchResult",
    "KnowledgeSecretPort",
    "KnowledgeSegmentView",
    "KnowledgeSettings",
    "create_knowledge_module",
]
