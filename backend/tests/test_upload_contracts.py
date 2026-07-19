from types import SimpleNamespace


def test_removed_global_upload_response_schemas_are_absent() -> None:
    from app import upload_contracts

    for name in ("UploadedFileInfo", "UploadResponse", "UploadListResponse"):
        assert not hasattr(upload_contracts, name)


MIB = 1024 * 1024


def test_legacy_and_private_upload_defaults_are_independent() -> None:
    from app.upload_contracts import LEGACY_UPLOAD_DEFAULTS, PRIVATE_UPLOAD_DEFAULTS

    assert LEGACY_UPLOAD_DEFAULTS.max_files == PRIVATE_UPLOAD_DEFAULTS.max_files == 10
    assert LEGACY_UPLOAD_DEFAULTS.max_file_size == 50 * MIB
    assert PRIVATE_UPLOAD_DEFAULTS.max_file_size == 100 * MIB
    assert LEGACY_UPLOAD_DEFAULTS.max_total_size == PRIVATE_UPLOAD_DEFAULTS.max_total_size == 100 * MIB


def test_upload_limit_resolver_applies_overrides_to_supplied_defaults() -> None:
    from app.upload_contracts import PRIVATE_UPLOAD_DEFAULTS, UploadLimits, resolve_upload_limits

    config = SimpleNamespace(
        uploads={
            "max_files": "4",
            "max_file_size": 123,
            "max_total_size": "456",
        }
    )

    assert resolve_upload_limits(config, defaults=PRIVATE_UPLOAD_DEFAULTS) == UploadLimits(
        max_files=4,
        max_file_size=123,
        max_total_size=456,
    )


def test_upload_limit_resolver_uses_supplied_defaults_for_invalid_values() -> None:
    from app.upload_contracts import LEGACY_UPLOAD_DEFAULTS, PRIVATE_UPLOAD_DEFAULTS, resolve_upload_limits

    config = SimpleNamespace(
        uploads={
            "max_files": 0,
            "max_file_size": "not-an-int",
            "max_total_size": -1,
        }
    )

    assert resolve_upload_limits(config, defaults=LEGACY_UPLOAD_DEFAULTS).model_dump() == {
        "max_files": 10,
        "max_file_size": 50 * MIB,
        "max_total_size": 100 * MIB,
    }
    assert resolve_upload_limits(config, defaults=PRIVATE_UPLOAD_DEFAULTS).model_dump() == {
        "max_files": 10,
        "max_file_size": 100 * MIB,
        "max_total_size": 100 * MIB,
    }
