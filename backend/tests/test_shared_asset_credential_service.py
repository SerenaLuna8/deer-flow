from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import uuid
from base64 import b64encode

import pytest
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.exc import TimeoutError as SATimeoutError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetStorageUnavailable, AssetValidationFailed


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-credential-unit",
    )


def _keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "unit-key")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        json.dumps({"unit-key": b64encode(b"k" * 32).decode("ascii")}),
    )


def test_credential_service_exposes_only_frozen_api_safe_views() -> None:
    package = importlib.import_module("app.shared_assets")
    service_module = importlib.import_module("app.shared_assets.credential_service")
    repository_module = importlib.import_module("app.shared_assets.credential_repository")

    assert package.CredentialService is service_module.CredentialService
    assert package.CreateCredential is service_module.CreateCredential
    for view_type in (
        service_module.CredentialView,
        service_module.CredentialVersionView,
        service_module.CredentialGrantView,
        service_module.CredentialGrantMigrationView,
    ):
        assert dataclasses.is_dataclass(view_type)
        assert view_type.__dataclass_params__.frozen is True
        fields = {field.name for field in dataclasses.fields(view_type)}
        assert fields.isdisjoint({"ciphertext", "nonce", "key_id", "secret_hash", "plaintext", "payload"})
        assert all("hash" not in field_name.lower() for field_name in fields)

    from app.audit.models import AuditAction
    from app.shared_assets.audit import _ACTIONS

    assert _ACTIONS["credential.create"] is AuditAction.ASSET_CREDENTIAL_CREATED
    assert _ACTIONS["credential.replace"] is AuditAction.ASSET_CREDENTIAL_REPLACED
    assert _ACTIONS["credential.revoke"] is AuditAction.ASSET_CREDENTIAL_REVOKED
    assert _ACTIONS["credential.grants.migrate"] is AuditAction.ASSET_CREDENTIAL_GRANTS_MIGRATED

    public_methods = inspect.getmembers(repository_module.CredentialRepository, predicate=inspect.isfunction)
    for name, method in public_methods:
        if not name.startswith("_"):
            assert "project_id" not in inspect.signature(method).parameters, name


@pytest.mark.asyncio
async def test_invalid_credential_input_is_rejected_before_crypto_or_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.credential_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open storage")

    monkeypatch.delenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", raising=False)
    monkeypatch.delenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", raising=False)
    service = service_module.CredentialService(ExplodingFactory())
    command = service_module.CreateCredential(
        name="Not Valid",
        display_name="ERP token",
        credential_type="token",
    )
    with pytest.raises(AssetValidationFailed) as exc_info:
        await service.create(_context(), command, {"env": {"ERP_TOKEN": "never-log-me"}})
    assert exc_info.value.request_id == "req-credential-unit"
    assert "never-log-me" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_editor_cannot_create_project_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    service_module = importlib.import_module("app.shared_assets.credential_service")

    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must happen before storage")

    _keyring(monkeypatch)
    service = service_module.CredentialService(ExplodingFactory())
    command = service_module.CreateCredential("erp", "ERP", "token")
    with pytest.raises(AssetForbidden):
        await service.create(
            _context(ProjectRole.EDITOR),
            command,
            {"env": {"ERP_TOKEN": "never-log-me"}},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint_name", "error_type"),
    [
        ("uq_credentials_project_name", AssetConflict),
        ("uq_credential_versions_asset_number", AssetConflict),
        ("ck_credential_envelopes_nonce_size", AssetStorageUnavailable),
        (None, AssetStorageUnavailable),
    ],
)
async def test_credential_integrity_errors_are_mapped_without_sql_or_secret(
    constraint_name: str | None,
    error_type: type[Exception],
) -> None:
    service_module = importlib.import_module("app.shared_assets.credential_service")

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return self

    class ConstraintViolation(Exception):
        def __init__(self, name: str | None):
            self.constraint_name = name

    async def fail(_repository):
        raise IntegrityError(
            "sensitive SQL",
            {"plaintext": "never-log-me"},
            ConstraintViolation(constraint_name),
        )

    service = service_module.CredentialService(EmptySession)
    with pytest.raises(error_type) as exc_info:
        await service._execute(_context(), fail)
    assert "sensitive SQL" not in str(exc_info.value)
    assert "never-log-me" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_credential_pool_timeout_is_mapped_to_safe_storage_unavailable() -> None:
    service_module = importlib.import_module("app.shared_assets.credential_service")

    class TimeoutFactory:
        def __call__(self):
            raise SATimeoutError("postgresql://admin:never-log-me@db.example.test/app")

    with pytest.raises(AssetStorageUnavailable) as exc_info:
        await service_module.CredentialService(TimeoutFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value.__cause__ is None
    assert "never-log-me" not in str(exc_info.value)
    assert "never-log-me" not in repr(exc_info.value)
    assert "postgresql" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_credential_programming_session_error_is_not_mapped_to_503() -> None:
    service_module = importlib.import_module("app.shared_assets.credential_service")
    programming_error = InvalidRequestError("programming failure")

    class InvalidFactory:
        def __call__(self):
            raise programming_error

    with pytest.raises(InvalidRequestError) as exc_info:
        await service_module.CredentialService(InvalidFactory()).get(_context(), uuid.uuid4())
    assert exc_info.value is programming_error
