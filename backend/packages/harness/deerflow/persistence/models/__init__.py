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
    ProjectChannelCredentialBindingRow,
    ProjectChannelInstanceLeaseRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.execution_approvals import (
    ExecutionApprovalOutputDeliveryCandidateRow,
    ExecutionApprovalOutputDeliveryObligationRow,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.jobs import DeadJobRow, JobAttemptRow, JobRow, WorkerNodeRow
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
    RunMcpGrantSnapshotRow,
    RunMemoryContextSnapshotRow,
    RunSkillCredentialSnapshotRow,
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
    AgentDesignOperationRow,
    AgentDesignSessionRow,
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    AssetCatalogStateRow,
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
    McpToolDiscoveryAttemptRow,
    ProjectMcpToolInventoryRow,
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
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
    SystemModelConfigVersionRow,
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
    "ProjectChannelCredentialBindingRow",
    "ProjectChannelGroupBindingChallengeRow",
    "ProjectChannelGroupBindingRow",
    "ProjectChannelInstanceLeaseRow",
    "ProjectChannelInstanceRow",
    "AgentRow",
    "AgentDesignOperationRow",
    "AgentDesignSessionRow",
    "AgentVersionMcpRefRow",
    "AgentVersionRow",
    "AgentVersionSkillRefRow",
    "AssetCatalogStateRow",
    "CredentialEnvelopeRow",
    "CredentialGrantRow",
    "CredentialRow",
    "CredentialVersionRow",
    "FeedbackRow",
    "ExecutionApprovalOutputDeliveryCandidateRow",
    "ExecutionApprovalOutputDeliveryObligationRow",
    "ExecutionApprovalRequestRow",
    "ExecutionApprovalResultReceiptRow",
    "DeadJobRow",
    "JobAttemptRow",
    "JobRow",
    "McpCredentialSlotRow",
    "McpServerRow",
    "McpServerVersionRow",
    "McpToolDiscoveryAttemptRow",
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
    "ProjectSkillCredentialBindingRow",
    "ProjectSkillCredentialConfigRow",
    "RunEventRow",
    "RunEventInvariantRow",
    "RunEventPartitionStateRow",
    "ThreadEventSequenceRow",
    "RunAssetVersionRow",
    "RunMcpGrantSnapshotRow",
    "RunMemoryContextSnapshotRow",
    "RunModelConfigSnapshotRow",
    "RunRuntimePolicySnapshotRow",
    "RunSkillCredentialSnapshotRow",
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
    "SystemModelConfigVersionRow",
    "SystemRuntimePolicyCatalogStateRow",
    "SystemRuntimePolicyRow",
    "SystemRuntimePolicyVersionRow",
    "ThreadMetaRow",
    "UserNotificationRow",
    "UserRow",
    "WorkerNodeRow",
]
