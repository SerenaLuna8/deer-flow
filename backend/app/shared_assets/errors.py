from __future__ import annotations

from typing import ClassVar


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


class AssetValidationFailed(SharedAssetError):
    code = "asset_validation_failed"
    status_code = 422
    public_message = "Asset validation failed"


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


class SkillDesignTargetUnsupported(SharedAssetError):
    """The published base version cannot seed a Builder revision session."""

    code = "SKILL_DESIGN_TARGET_UNSUPPORTED"
    status_code = 422
    public_message = "Skill Builder cannot revise this Skill's published version"


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


class SkillDesignBaseStale(SharedAssetError):
    """The live published version moved since the revision session pinned its base."""

    code = "SKILL_DESIGN_BASE_STALE"
    status_code = 409
    public_message = "The published version changed since this revision session started"


class SkillDesignNoChanges(SharedAssetError):
    """The candidate draft is byte-identical to the pinned base version."""

    code = "SKILL_DESIGN_NO_CHANGES"
    status_code = 409
    public_message = "The draft is identical to the base version"


class SkillPublishBaseStale(SharedAssetError):
    """Publishing would supersede a version that is no longer the live one."""

    code = "SKILL_PUBLISH_BASE_STALE"
    status_code = 409
    public_message = "The version being published was not based on the live published version"
