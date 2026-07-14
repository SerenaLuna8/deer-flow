from __future__ import annotations

from typing import ClassVar

PRIVATE_WORK_ERROR_STATUS = {
    "PRIVATE_WORK_NOT_FOUND": 404,
    "PRIVATE_WORK_FORBIDDEN": 403,
    "PRIVATE_WORK_CONFLICT": 409,
    "PRIVATE_WORK_ASSET_STALE": 409,
    "PRIVATE_WORK_CUTOVER": 409,
    "PRIVATE_WORK_TOO_LARGE": 413,
    "PRIVATE_WORK_INVALID": 422,
    "PRIVATE_WORK_UNAVAILABLE": 503,
}


class PrivateWorkError(Exception):
    code: ClassVar[str]
    public_message: ClassVar[str]

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.public_message)


class PrivateWorkNotFound(PrivateWorkError):
    code = "PRIVATE_WORK_NOT_FOUND"
    public_message = "Private work was not found."


class PrivateWorkForbidden(PrivateWorkError):
    code = "PRIVATE_WORK_FORBIDDEN"
    public_message = "Private work action is forbidden."


class PrivateWorkConflict(PrivateWorkError):
    code = "PRIVATE_WORK_CONFLICT"
    public_message = "Private work conflict."


class PrivateWorkAssetStale(PrivateWorkError):
    code = "PRIVATE_WORK_ASSET_STALE"
    public_message = "Private work asset is stale."


class PrivateWorkCutover(PrivateWorkError):
    code = "PRIVATE_WORK_CUTOVER"
    public_message = "Private work cutover is not complete."


class PrivateWorkTooLarge(PrivateWorkError):
    code = "PRIVATE_WORK_TOO_LARGE"
    public_message = "Private work payload is too large."


class PrivateWorkInvalid(PrivateWorkError):
    code = "PRIVATE_WORK_INVALID"
    public_message = "Private work request is invalid."


class PrivateWorkUnavailable(PrivateWorkError):
    code = "PRIVATE_WORK_UNAVAILABLE"
    public_message = "Private work is unavailable."
