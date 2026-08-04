from __future__ import annotations


class ProjectChannelGroupBindingError(Exception):
    code = "GROUP_BINDING_ERROR"
    message = "Group connection failed."

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.message)


class GroupBindingNotFound(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_NOT_FOUND"
    message = "Group connection not found."


class GroupBindingForbidden(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_FORBIDDEN"
    message = "Project Admin permission is required to manage group connections."


class GroupBindingConflict(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_CONFLICT"
    message = "Group connection changed. Refresh and try again."


class GroupBindingUnavailable(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_UNAVAILABLE"
    message = "Group connection storage is unavailable."


class GroupBindingAgentUnavailable(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_AGENT_UNAVAILABLE"
    message = "The selected Agent is unavailable."


class GroupBindingInvalid(ProjectChannelGroupBindingError):
    code = "GROUP_BINDING_INVALID"

    def __init__(
        self,
        request_id: str,
        message: str = "Group connection input is invalid.",
        *,
        fields: tuple[str, ...] = (),
    ) -> None:
        self.request_id = request_id
        self.message = message
        self.fields = fields
        Exception.__init__(self, message)


__all__ = [
    "GroupBindingAgentUnavailable",
    "GroupBindingConflict",
    "GroupBindingForbidden",
    "GroupBindingInvalid",
    "GroupBindingNotFound",
    "GroupBindingUnavailable",
    "ProjectChannelGroupBindingError",
]
