"""ORM model registration entry point.

Importing this module ensures all ORM models are registered with
``Base.metadata`` so Alembic autogenerate detects every table.

The actual ORM classes have moved to entity-specific subpackages:
- ``deerflow.persistence.thread_meta``
- ``deerflow.persistence.run``
- ``deerflow.persistence.feedback``
- ``deerflow.persistence.user``

``RunEventRow`` remains in ``deerflow.persistence.models.run_event`` because
its storage implementation lives in ``deerflow.runtime.events.store.db`` and
there is no matching entity directory.
"""

from deerflow.persistence.automations import (
    AutomationCutoverStateRow,
    AutomationMigrationLedgerRow,
    AutomationMigrationRunRow,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ChannelCredentialRow,
    ChannelOAuthStateRow,
)
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.migration_ledger.model import MigrationLedgerRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.private_work import (
    PrivateArtifactRow,
    PrivateFileChunkRow,
    PrivateFileRow,
    PrivateWorkCutoverStateRow,
    PrivateWorkMigrationLedgerRow,
    PrivateWorkMigrationRunRow,
    RunAssetVersionRow,
    RunMcpGrantSnapshotRow,
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
)
from deerflow.persistence.projects.invitation_model import ProjectInvitationRow
from deerflow.persistence.projects.invitation_rate_limit_model import (
    ProjectInvitationRateLimitRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.shared_assets import (
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
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

__all__ = [
    "AutomationCutoverStateRow",
    "AutomationMigrationLedgerRow",
    "AutomationMigrationRunRow",
    "ChannelConnectionRow",
    "ChannelConversationRow",
    "ChannelCredentialRow",
    "ChannelOAuthStateRow",
    "AgentRow",
    "AgentVersionMcpRefRow",
    "AgentVersionRow",
    "AgentVersionSkillRefRow",
    "AssetCatalogStateRow",
    "CredentialEnvelopeRow",
    "CredentialGrantRow",
    "CredentialRow",
    "CredentialVersionRow",
    "FeedbackRow",
    "MigrationLedgerRow",
    "McpCredentialSlotRow",
    "McpServerRow",
    "McpServerVersionRow",
    "ProjectInvitationRow",
    "ProjectInvitationRateLimitRow",
    "ProjectMembershipRow",
    "ProjectRow",
    "PrivateArtifactRow",
    "PrivateFileChunkRow",
    "PrivateFileRow",
    "PrivateWorkCutoverStateRow",
    "PrivateWorkMigrationLedgerRow",
    "PrivateWorkMigrationRunRow",
    "ProjectSystemAgentBindingRow",
    "ProjectSystemMcpBindingRow",
    "ProjectSystemSkillBindingRow",
    "RunEventRow",
    "RunAssetVersionRow",
    "RunMcpGrantSnapshotRow",
    "RunRow",
    "ScheduledTaskRow",
    "ScheduledTaskRunRow",
    "SkillRow",
    "SkillVersionFileRow",
    "SkillVersionRow",
    "ThreadMetaRow",
    "UserRow",
    "UserProjectMemoryFactRow",
    "UserProjectMemoryRow",
]
