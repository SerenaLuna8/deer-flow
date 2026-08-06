"""Stable, content-free failures for runtime-policy administration."""

from __future__ import annotations

from typing import ClassVar


class SystemRuntimePolicyError(Exception):
    code: ClassVar[str]
    status_code: ClassVar[int]
    public_message: ClassVar[str]

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.public_message)


class SystemRuntimePolicyNotFound(SystemRuntimePolicyError):
    code = "system_runtime_policy_not_found"
    status_code = 404
    public_message = "System runtime policy not found"


class SystemRuntimePolicyConflict(SystemRuntimePolicyError):
    code = "system_runtime_policy_conflict"
    status_code = 409
    public_message = "System runtime policy state conflict"


class SystemRuntimePolicyInvalid(SystemRuntimePolicyError):
    code = "system_runtime_policy_invalid"
    status_code = 422
    public_message = "System runtime policy invalid"


class SystemRuntimePolicyStorageUnavailable(SystemRuntimePolicyError):
    code = "system_runtime_policy_storage_unavailable"
    status_code = 503
    public_message = "System runtime policy storage unavailable"


class SystemRuntimePolicyAdministrationRequired(PermissionError):
    def __init__(self) -> None:
        super().__init__("System runtime policy administration required")


class SystemRuntimePolicyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("System runtime policy unavailable")


__all__ = [
    "SystemRuntimePolicyAdministrationRequired",
    "SystemRuntimePolicyConflict",
    "SystemRuntimePolicyError",
    "SystemRuntimePolicyInvalid",
    "SystemRuntimePolicyNotFound",
    "SystemRuntimePolicyStorageUnavailable",
    "SystemRuntimePolicyUnavailable",
]
