from __future__ import annotations


class ProjectChannelError(Exception):
    code = "CHANNEL_INSTANCE_ERROR"
    message = "Channel connection failed."

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(self.message)


class ChannelInstanceNotFound(ProjectChannelError):
    code = "CHANNEL_INSTANCE_NOT_FOUND"
    message = "Channel connection not found."


class ChannelInstanceForbidden(ProjectChannelError):
    code = "CHANNEL_INSTANCE_FORBIDDEN"
    message = "Project Admin permission is required to manage channel connections."


class ChannelInstanceConflict(ProjectChannelError):
    code = "CHANNEL_INSTANCE_CONFLICT"
    message = "Channel connection changed. Refresh and try again."


class ChannelInstanceIdentityConflict(ProjectChannelError):
    code = "CHANNEL_INSTANCE_IDENTITY_CONFLICT"
    message = "This provider application is already connected to another project."


class ChannelInstanceStorageUnavailable(ProjectChannelError):
    code = "CHANNEL_INSTANCE_UNAVAILABLE"
    message = "Channel connection storage is unavailable."


class ChannelInstanceValidationFailed(ProjectChannelError):
    code = "CHANNEL_INSTANCE_INVALID"

    def __init__(
        self,
        request_id: str,
        message: str,
        *,
        fields: tuple[str, ...] = (),
    ) -> None:
        self.request_id = request_id
        self.message = message
        self.fields = fields
        Exception.__init__(self, message)
