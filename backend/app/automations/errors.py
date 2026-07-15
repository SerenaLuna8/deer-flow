from __future__ import annotations

from typing import ClassVar

AUTOMATION_ERROR_STATUS = {
    "AUTOMATION_NOT_FOUND": 404,
    "AUTOMATION_FORBIDDEN": 403,
    "AUTOMATION_INVALID": 422,
    "AUTOMATION_VERSION_CONFLICT": 409,
    "AUTOMATION_ACTIVE_RUN": 409,
    "AUTOMATION_CUTOVER": 409,
    "AUTOMATION_CONCURRENCY_LIMIT": 429,
    "AUTOMATION_UNAVAILABLE": 503,
}


class AutomationError(Exception):
    code: ClassVar[str] = "AUTOMATION_UNAVAILABLE"
    public_message: ClassVar[str] = "Automation is temporarily unavailable."

    def __init__(self, request_id: str) -> None:
        super().__init__(self.public_message)
        self.request_id = request_id


class AutomationNotFound(AutomationError):
    code = "AUTOMATION_NOT_FOUND"
    public_message = "Automation was not found."


class AutomationForbidden(AutomationError):
    code = "AUTOMATION_FORBIDDEN"
    public_message = "Automation action is forbidden."


class AutomationInvalid(AutomationError):
    code = "AUTOMATION_INVALID"
    public_message = "Automation request is invalid."


class AutomationVersionConflict(AutomationError):
    code = "AUTOMATION_VERSION_CONFLICT"
    public_message = "Automation version conflict."


class AutomationActiveRun(AutomationError):
    code = "AUTOMATION_ACTIVE_RUN"
    public_message = "Automation has an active run."


class AutomationCutover(AutomationError):
    code = "AUTOMATION_CUTOVER"
    public_message = "Automation cutover is not complete."


class AutomationConcurrencyLimit(AutomationError):
    code = "AUTOMATION_CONCURRENCY_LIMIT"
    public_message = "Automation concurrency limit was reached."


class AutomationUnavailable(AutomationError):
    code = "AUTOMATION_UNAVAILABLE"
    public_message = "Automation is temporarily unavailable."


__all__ = [
    "AUTOMATION_ERROR_STATUS",
    "AutomationActiveRun",
    "AutomationConcurrencyLimit",
    "AutomationCutover",
    "AutomationError",
    "AutomationForbidden",
    "AutomationInvalid",
    "AutomationNotFound",
    "AutomationUnavailable",
    "AutomationVersionConflict",
]
