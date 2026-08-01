"""Stable, secret-free system model catalog failures."""

from __future__ import annotations

from typing import ClassVar


class SystemModelError(Exception):
    code: ClassVar[str]
    status_code: ClassVar[int]
    public_message: ClassVar[str]

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.public_message)


class SystemModelNotFound(SystemModelError):
    code = "system_model_not_found"
    status_code = 404
    public_message = "System model not found"


class SystemModelConflict(SystemModelError):
    code = "system_model_conflict"
    status_code = 409
    public_message = "System model state conflict"


class SystemModelInvalid(SystemModelError):
    code = "system_model_invalid"
    status_code = 422
    public_message = "System model invalid"


class SystemModelStorageUnavailable(SystemModelError):
    code = "system_model_storage_unavailable"
    status_code = 503
    public_message = "System model storage unavailable"


class SystemModelAdministrationRequired(PermissionError):
    def __init__(self) -> None:
        super().__init__("System model administration required")


__all__ = [
    "SystemModelAdministrationRequired",
    "SystemModelConflict",
    "SystemModelError",
    "SystemModelInvalid",
    "SystemModelNotFound",
    "SystemModelStorageUnavailable",
]
