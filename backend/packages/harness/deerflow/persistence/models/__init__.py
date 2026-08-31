"""ORM model registration entry point.

Importing this module ensures all ORM models are registered with
``Base.metadata`` so schema-parity checks cover every application table.

The actual ORM classes have moved to entity-specific subpackages:
- ``deerflow.persistence.thread_meta``
- ``deerflow.persistence.run``
- ``deerflow.persistence.feedback``
- ``deerflow.persistence.user``

``RunEventRow`` remains in ``deerflow.persistence.models.run_event`` because
its storage implementation lives in ``deerflow.runtime.events.store.db`` and
there is no matching entity directory.
"""

from deerflow.persistence.audit import AuditLogRow
from deerflow.persistence.auth_sessions import AuthSessionRow
from deerflow.persistence.channel_connections.group_challenge_model import (
    ProjectChannelGroupBindingChallengeRow,
)
from deerflow.persistence.channel_connections.group_model import (
    ChannelExternalPrincipalRow,
    ProjectChannelGroupBindingRow,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ChannelCredentialRow,
    ChannelInboundDeliveryRow,
    ChannelOAuthStateRow,
    ProjectChannelInstanceLeaseRow,
    ProjectChannelInstanceRow,
    ProjectChannelSecretGenerationRow,
    ProjectChannelSecretStateRow,
    ProjectChannelSecretTombstoneRow,
)
from deerflow.persistence.context_evidence import (
    ContextEvidenceRow,
    ContextEvidenceSequenceRow,
    ContextProjectionHeadRow,
)
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryCandidateRow,
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.jobs import DeadJobRow, JobAttemptRow, JobRow, WorkerNodeRow
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.model_registry import (
    ModelProviderModelRow,
    ModelProviderRow,
)
from deerflow.persistence.models.run_event import (
    RunEventInvariantRow,
    RunEventPartitionStateRow,
    RunEventRow,
    ThreadEventSequenceRow,
)
from deerflow.persistence.notifications import UserNotificationRow
from deerflow.persistence.private_work import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamPrepareRunRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    RunAssetVersionRow,
    RunMcpSecretSnapshotRow,
    RunMemoryContextSnapshotRow,
    RunSkillSecretSnapshotRow,
    RunSkillVersionRefRow,
)
from deerflow.persistence.projects.invitation_model import ProjectInvitationRow
from deerflow.persistence.projects.invitation_rate_limit_model import (
    ProjectInvitationRateLimitRow,
)
from deerflow.persistence.projects.model import (
    ProjectDefaultAgentRow,
    ProjectMembershipRow,
    ProjectRow,
)
from deerflow.persistence.quotas import ProjectQuotaRow, ProjectUsageCounterRow, ProjectUsageLedgerRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.shared_assets import (
    AgentDesignActivityRow,
    AgentDesignOperationRow,
    AgentDesignSessionRow,
    AgentMcpRefRow,
    AgentRow,
    AgentSkillRefRow,
    AssetCatalogStateRow,
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    McpToolDiscoveryAttemptRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
    ProjectMcpToolInventoryRow,
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
    ProjectSkillSecretTombstoneRow,
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillDesignDraftFileRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyCatalogStateRow,
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

__all__ = [
    "AuditLogRow",
    "AuthSessionRow",
    "ChannelConnectionRow",
    "ChannelConversationRow",
    "ChannelCredentialRow",
    "ChannelExternalPrincipalRow",
    "ChannelInboundDeliveryRow",
    "ChannelOAuthStateRow",
    "ContextEvidenceRow",
    "ContextEvidenceSequenceRow",
    "ContextProjectionHeadRow",
    "ProjectChannelSecretGenerationRow",
    "ProjectChannelSecretStateRow",
    "ProjectChannelSecretTombstoneRow",
    "ProjectChannelGroupBindingChallengeRow",
    "ProjectChannelGroupBindingRow",
    "ProjectChannelInstanceLeaseRow",
    "ProjectChannelInstanceRow",
    "AgentRow",
    "AgentDesignActivityRow",
    "AgentDesignOperationRow",
    "AgentDesignSessionRow",
    "AgentMcpRefRow",
    "AgentSkillRefRow",
    "AssetCatalogStateRow",
    "FeedbackRow",
    "ExecutionApprovalOutputDeliveryCandidateRow",
    "ExecutionApprovalOutputDeliveryObligationRow",
    "ExecutionApprovalRequestRow",
    "ExecutionApprovalResultReceiptRow",
    "DeadJobRow",
    "JobAttemptRow",
    "JobRow",
    "KnowledgeSystemSettingsRow",
    "McpSecretSlotRow",
    "McpServerRow",
    "McpServerVersionRow",
    "McpToolDiscoveryAttemptRow",
    "ModelProviderModelRow",
    "ModelProviderRow",
    "ProjectMcpSecretGenerationRow",
    "ProjectMcpSecretStateRow",
    "ProjectMcpSecretTombstoneRow",
    "MemoryDocumentRow",
    "MemoryDocumentVersionRow",
    "MemoryDreamPrepareRunRow",
    "MemoryDreamRunRow",
    "MemoryEpisodeRow",
    "MemoryHistoryEntryRow",
    "ProjectMcpToolInventoryRow",
    "ProjectInvitationRow",
    "ProjectInvitationRateLimitRow",
    "ProjectDefaultAgentRow",
    "ProjectMembershipRow",
    "ProjectRow",
    "ProjectQuotaRow",
    "ProjectUsageCounterRow",
    "ProjectUsageLedgerRow",
    "PrivateArtifactRow",
    "PrivateFileChunkRow",
    "PrivateFileRow",
    "ProjectSystemAgentBindingRow",
    "ProjectSystemMcpBindingRow",
    "ProjectSystemSkillBindingRow",
    "ProjectSkillSecretGenerationRow",
    "ProjectSkillSecretStateRow",
    "ProjectSkillSecretTombstoneRow",
    "RunEventRow",
    "RunEventInvariantRow",
    "RunEventPartitionStateRow",
    "ThreadEventSequenceRow",
    "RunAssetVersionRow",
    "RunMcpSecretSnapshotRow",
    "RunMemoryContextSnapshotRow",
    "RunModelConfigSnapshotRow",
    "RunRuntimePolicySnapshotRow",
    "RunSkillVersionRefRow",
    "RunSkillSecretSnapshotRow",
    "RunRow",
    "ScheduledTaskRow",
    "ScheduledTaskRunRow",
    "SkillRow",
    "SkillDesignDraftFileRow",
    "SkillDesignOperationRow",
    "SkillDesignSessionRow",
    "SkillVersionFileRow",
    "SkillVersionRow",
    "SystemModelCatalogStateRow",
    "SystemModelConfigRow",
    "SystemModelSecretGenerationRow",
    "SystemModelSecretTombstoneRow",
    "SystemRuntimePolicyCatalogStateRow",
    "SystemRuntimePolicyRow",
    "SystemRuntimePolicyVersionRow",
    "ThreadMetaRow",
    "UserNotificationRow",
    "UserRow",
    "WorkerNodeRow",
]
