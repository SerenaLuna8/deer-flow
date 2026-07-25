def test_removed_global_upload_response_schemas_are_absent() -> None:
    from app import upload_contracts

    for name in (
        "UploadedFileInfo",
        "UploadResponse",
        "UploadListResponse",
        "UploadLimits",
        "LEGACY_UPLOAD_DEFAULTS",
        "get_uploads_config_value",
        "resolve_upload_limits",
    ):
        assert not hasattr(upload_contracts, name)


MIB = 1024 * 1024


def test_private_upload_defaults_are_the_only_application_contract() -> None:
    from app.upload_contracts import PRIVATE_UPLOAD_DEFAULTS

    assert PRIVATE_UPLOAD_DEFAULTS.max_files == 10
    assert PRIVATE_UPLOAD_DEFAULTS.max_file_size == 100 * MIB
    assert PRIVATE_UPLOAD_DEFAULTS.max_total_size == 100 * MIB


def test_private_upload_limits_response_exposes_only_remaining_project_storage() -> None:
    import pytest
    from pydantic import ValidationError

    from app.gateway.routers.private_work import PrivateUploadLimitsResponse

    payload = {
        "max_files": 10,
        "max_file_size": 100 * MIB,
        "max_total_size": 100 * MIB,
        "project_storage": {
            "policy": "project_quota",
            "remaining_bytes": 500,
        },
        "request_id": "upload-limits",
    }

    assert PrivateUploadLimitsResponse.model_validate(payload).model_dump() == payload

    with pytest.raises(ValidationError):
        PrivateUploadLimitsResponse.model_validate({**payload, "unexpected": True})

    leaking_storage = {
        **payload,
        "project_storage": {
            **payload["project_storage"],
            "used_bytes": 300,
        },
    }
    with pytest.raises(ValidationError):
        PrivateUploadLimitsResponse.model_validate(leaking_storage)
