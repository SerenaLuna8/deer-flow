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
