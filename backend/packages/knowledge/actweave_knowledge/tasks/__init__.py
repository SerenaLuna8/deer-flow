"""Internal task execution: the claim/lease worker and delete handlers."""

from .deletion import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    purge_project_knowledge,
)
from .extraction_deletion import (
    KnowledgeExtractionDeletionHandler,
    delete_registered_extraction,
)
from .worker import (
    KnowledgeTaskClaim,
    KnowledgeTaskWorker,
    ProjectActiveCheck,
    TaskHandler,
)

__all__ = [
    "KnowledgeBaseDeletionHandler",
    "KnowledgeDocumentDeletionHandler",
    "KnowledgeDocumentObjectDeletionHandler",
    "KnowledgeExtractionDeletionHandler",
    "KnowledgeTaskClaim",
    "KnowledgeTaskWorker",
    "ProjectActiveCheck",
    "TaskHandler",
    "delete_registered_extraction",
    "purge_project_knowledge",
]
