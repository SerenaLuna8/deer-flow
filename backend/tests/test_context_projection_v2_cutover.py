"""Direct V2 cutover must not retain the legacy Context Gauge authority path."""

from app.private_work import error_mapping, errors
from app.private_work.chat_controls import ProjectChatControlService


def test_context_projection_v2_removes_v1_usage_authority_and_error() -> None:
    assert not hasattr(ProjectChatControlService, "context_usage")
    assert not hasattr(ProjectChatControlService, "context_usage_authority_marker")
    assert not hasattr(errors, "PrivateWorkContextUsageUnsupported")
    assert "CONTEXT_USAGE_UNSUPPORTED" not in errors.PRIVATE_WORK_ERROR_STATUS
    assert all(error_type.__name__ != "PrivateWorkContextUsageUnsupported" for error_type in error_mapping._PRIVATE_WORK_ERROR_TYPES)
