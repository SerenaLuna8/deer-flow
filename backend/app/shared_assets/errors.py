from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

SKILL_ARCHIVE_SECURITY_RISK_ACCEPTANCE = "accept-blocked-skill-archive"
MAX_SKILL_ARCHIVE_SECURITY_DIAGNOSTICS = 20


class SharedAssetError(Exception):
    code: ClassVar[str]
    status_code: ClassVar[int]
    public_message: ClassVar[str]

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.public_message)


class AssetNotFound(SharedAssetError):
    code = "asset_not_found"
    status_code = 404
    public_message = "Asset not found"


class AssetForbidden(SharedAssetError):
    code = "asset_forbidden"
    status_code = 403
    public_message = "Asset capability required"


class AssetConflict(SharedAssetError):
    code = "asset_conflict"
    status_code = 409
    public_message = "Asset state conflict"


class AssetInUse(AssetConflict):
    """The asset has immutable references that forbid physical deletion."""

    code = "ASSET_IN_USE"
    public_message = "Asset is still referenced"


class AssetValidationFailed(SharedAssetError):
    code = "asset_validation_failed"
    status_code = 422
    public_message = "Asset validation failed"


class SkillArchiveLimitExceeded(AssetValidationFailed):
    """An uploaded Skill archive exceeded a bounded parsing limit."""

    code = "SKILL_ARCHIVE_LIMIT_EXCEEDED"
    status_code = 413
    public_message = "Skill archive exceeds the allowed size or member limit"


@dataclass(frozen=True)
class SkillArchiveSecurityDiagnostic:
    rule_id: str
    file: str | None
    line: int | None


@dataclass(frozen=True)
class SkillArchiveSecurityRiskAcceptance:
    payload_checksum: str
    findings_checksum: str


class SkillArchiveSecurityBlocked(AssetValidationFailed):
    """An uploaded Skill archive has blocking static-scan findings."""

    code = "SKILL_ARCHIVE_SECURITY_BLOCKED"
    public_message = "Skill archive failed security scan"

    def __init__(
        self,
        request_id: str,
        diagnostics: tuple[SkillArchiveSecurityDiagnostic, ...],
        *,
        risk_confirmation: SkillArchiveSecurityRiskAcceptance | None = None,
    ) -> None:
        self.diagnostics = diagnostics[:MAX_SKILL_ARCHIVE_SECURITY_DIAGNOSTICS]
        self.risk_confirmation = risk_confirmation
        super().__init__(request_id)


class SkillRuntimeNameConflict(AssetConflict):
    """A runtime-visible Skill already owns the requested normalized name."""

    code = "SKILL_RUNTIME_NAME_CONFLICT"
    public_message = "A runtime-visible Skill already uses this name"


class SkillSecretDeclarationInvalid(AssetValidationFailed):
    """A Skill secret declaration cannot be parsed or safely patched.

    ``diagnostics`` contains only the bounded, public-safe structured
    diagnostics produced by the canonical frontmatter parser.  The exception
    deliberately never stores the submitted SKILL.md source.
    """

    code = "SKILL_SECRET_DECLARATION_INVALID"
    public_message = "Skill secret declaration is invalid"

    def __init__(
        self,
        request_id: str,
        diagnostics: tuple[object, ...] = (),
    ) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(request_id)


class SkillFrontmatterSourceStale(AssetConflict):
    """The submitted form patch no longer targets the caller's exact buffer."""

    code = "SKILL_FRONTMATTER_SOURCE_STALE"
    public_message = "Skill frontmatter source changed"


class AgentDesignSlugConflict(AssetConflict):
    """A project Agent already owns the requested Builder slug."""

    code = "AGENT_DESIGN_SLUG_CONFLICT"
    public_message = "A project Agent already uses this slug"


class AgentDesignConflictUnresolved(AssetConflict):
    """A generated candidate still has an unresolved error conflict."""

    code = "AGENT_DESIGN_CONFLICT_UNRESOLVED"
    public_message = "Agent design has unresolved error conflicts"


class AgentDesignGenerationProfileStale(AssetConflict):
    """The selected model can no longer honor the requested Builder mode."""

    code = "AGENT_DESIGN_GENERATION_PROFILE_STALE"
    public_message = "Agent design generation profile is no longer available"


class AgentDesignSessionLimitExceeded(SharedAssetError):
    """An owner already has the maximum incomplete Builder sessions."""

    code = "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED"
    status_code = 429
    public_message = "Agent Builder incomplete session limit exceeded"


class AssetStorageUnavailable(SharedAssetError):
    code = "asset_storage_unavailable"
    status_code = 503
    public_message = "Asset storage unavailable"


class AssetStorageQuotaExceeded(SharedAssetError):
    code = "asset_storage_quota_exceeded"
    status_code = 429
    public_message = "Project Skill storage quota exceeded"


class AssetRunQuotaExceeded(SharedAssetError):
    code = "asset_run_quota_exceeded"
    status_code = 429
    public_message = "Project concurrent Run quota exceeded"


class AssetResolutionUnavailable(SharedAssetError):
    code = "asset_resolution_unavailable"
    status_code = 503
    public_message = "Asset resolution unavailable"


class AgentArchived(AssetResolutionUnavailable):
    """A project Agent exists but cannot admit any new Run."""

    code = "AGENT_ARCHIVED"
    status_code = 409
    public_message = "Agent is archived"


class SkillDesignTargetUnsupported(SharedAssetError):
    """The selected base cannot seed a forward Builder revision session."""

    code = "SKILL_DESIGN_TARGET_UNSUPPORTED"
    status_code = 422
    public_message = "Skill Builder can revise only this Skill's current version"


class SkillDesignTargetSessionExists(SharedAssetError):
    """An incomplete revision session already targets the same Skill."""

    code = "SKILL_DESIGN_TARGET_SESSION_EXISTS"
    status_code = 409
    public_message = "An incomplete revision session already exists for this Skill"


class SkillDesignTargetDeleted(SharedAssetError):
    """The revision target Skill was deleted after the session started."""

    code = "SKILL_DESIGN_TARGET_DELETED"
    status_code = 409
    public_message = "The revision target Skill was deleted"


class SkillDesignNoChanges(SharedAssetError):
    """The candidate is byte-identical to the pinned base version."""

    code = "SKILL_DESIGN_NO_CHANGES"
    status_code = 409
    public_message = "The candidate is identical to the base version"


class SkillSecretsIncomplete(AssetValidationFailed):
    """A Skill Candidate cannot activate without every required secret."""

    code = "SKILL_SECRETS_INCOMPLETE"
    public_message = "Required Skill secrets are incomplete"


class SkillSecretConfigurationInvalid(AssetValidationFailed):
    """A Skill declaration or write-only secret input is invalid."""

    code = "SKILL_SECRET_CONFIGURATION_INVALID"
    public_message = "Skill secret configuration is invalid"


class SkillSecretRevisionStale(AssetConflict):
    """The activation-plan secret revision is no longer current."""

    code = "SKILL_SECRET_REVISION_STALE"
    public_message = "Skill secret revision is stale"
