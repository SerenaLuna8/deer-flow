"""Compatibility façade for Project Skill Builder lifecycle."""

from app.shared_assets.skill_design_activity import (
    SkillDesignActivity as SkillDesignActivity,
)
from app.shared_assets.skill_design_contracts import (
    CancelSkillDesignSession as CancelSkillDesignSession,
)
from app.shared_assets.skill_design_contracts import (
    CommitSkillDesignSession as CommitSkillDesignSession,
)
from app.shared_assets.skill_design_contracts import (
    CreateSkillDesignRevisionSession as CreateSkillDesignRevisionSession,
)
from app.shared_assets.skill_design_contracts import (
    CreateSkillDesignSession as CreateSkillDesignSession,
)
from app.shared_assets.skill_design_contracts import (
    SetSkillDesignExecutionPreference as SetSkillDesignExecutionPreference,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignBaseFile as SkillDesignBaseFile,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignClarificationOption as SkillDesignClarificationOption,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignClarificationRequest as SkillDesignClarificationRequest,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignClarificationResponse as SkillDesignClarificationResponse,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignClarificationTurn as SkillDesignClarificationTurn,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignCommitResult as SkillDesignCommitResult,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignDraftUpdateTurn as SkillDesignDraftUpdateTurn,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignExecutionPreference as SkillDesignExecutionPreference,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignFileView as SkillDesignFileView,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignMessage as SkillDesignMessage,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignMessageTurn as SkillDesignMessageTurn,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignProgressItem as SkillDesignProgressItem,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignProgressStatus as SkillDesignProgressStatus,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignSecretRequirement as SkillDesignSecretRequirement,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignServiceErrorCode as SkillDesignServiceErrorCode,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignSessionSummary as SkillDesignSessionSummary,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignSessionView as SkillDesignSessionView,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignStatus as SkillDesignStatus,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignTurn as SkillDesignTurn,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignTurnAttachment as SkillDesignTurnAttachment,
)
from app.shared_assets.skill_design_contracts import (
    SkillDesignValidation as SkillDesignValidation,
)
from app.shared_assets.skill_design_contracts import (
    SubmitSkillDesignTurn as SubmitSkillDesignTurn,
)
from app.shared_assets.skill_design_contracts import (
    ValidateSkillDesignSession as ValidateSkillDesignSession,
)
from app.shared_assets.skill_design_generation import (
    SkillDesignGenerationRequest as SkillDesignGenerationRequest,
)
from app.shared_assets.skill_design_lifecycle import (
    MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT as MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT,
)
from app.shared_assets.skill_design_lifecycle import (
    SkillDesignService as SkillDesignService,
)

__all__ = [
    "CancelSkillDesignSession",
    "CommitSkillDesignSession",
    "CreateSkillDesignRevisionSession",
    "CreateSkillDesignSession",
    "MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT",
    "SkillDesignBaseFile",
    "SkillDesignClarificationResponse",
    "SkillDesignClarificationTurn",
    "SkillDesignCommitResult",
    "SkillDesignDraftUpdateTurn",
    "SkillDesignFileView",
    "SkillDesignMessageTurn",
    "SkillDesignProgressItem",
    "SkillDesignSecretRequirement",
    "SkillDesignService",
    "SkillDesignSessionSummary",
    "SkillDesignSessionView",
    "SkillDesignStatus",
    "SkillDesignTurnAttachment",
    "SkillDesignValidation",
    "SubmitSkillDesignTurn",
    "ValidateSkillDesignSession",
]
