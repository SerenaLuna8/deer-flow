from __future__ import annotations

import dataclasses
import json
import uuid
from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app.audit.models import resolve_system_audit_context
from app.shared_assets.crypto import encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope
from app.system_settings.credential_adapter import (
    SystemModelCredentialAdapter,
    SystemModelMaterializationUnavailable,
)
from app.system_settings.errors import SystemModelInvalid
from app.system_settings.models import (
    CreateSystemModel,
    LockedSystemModelMaterial,
    PublicSystemModelView,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.service import SystemModelCatalogService
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    provider_class_path,
    validate_create_system_model,
    validate_model_settings,
)
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.base import Base
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)

USER_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
MODEL_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
MODEL_VERSION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
CREDENTIAL_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
CREDENTIAL_VERSION_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _CaptureSession:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


class _NoopTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ServiceSession:
    def __init__(self) -> None:
        self.role_checks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _NoopTransaction()

    async def execute(self, statement):
        del statement
        self.role_checks += 1
        return _ScalarResult("system_admin")


def _command(**overrides: object) -> CreateSystemModel:
    values: dict[str, object] = {
        "logical_name": "primary-openai",
        "display_name": "Primary OpenAI",
        "description": "Primary model",
        "status": "active",
        "provider_adapter": "openai",
        "provider_model": "gpt-5",
        "settings": {
            "base_url": "https://api.openai.com/v1",
            "temperature": 0.2,
            "extra_body": {"reasoning": {"effort": "medium"}},
        },
        "supports_thinking": True,
        "supports_reasoning_effort": True,
        "supports_vision": True,
        "credential_id": CREDENTIAL_ID,
        "credential_version_id": CREDENTIAL_VERSION_ID,
        "credential_env_key": "OPENAI_API_KEY",
    }
    values.update(overrides)
    return CreateSystemModel(**values)


def _keyring() -> CredentialKeyring:
    return CredentialKeyring(
        active_key_id="test-key",
        _keys={"test-key": b"k" * 32},
    )


def test_system_model_tables_are_registered_in_final_metadata() -> None:
    assert {
        "system_model_catalog_state",
        "system_model_configs",
        "system_model_config_versions",
        "run_model_config_snapshots",
    }.issubset(Base.metadata.tables)

    assert SystemModelCatalogStateRow.__table__ is Base.metadata.tables["system_model_catalog_state"]
    assert SystemModelConfigRow.__table__ is Base.metadata.tables["system_model_configs"]
    assert SystemModelConfigVersionRow.__table__ is Base.metadata.tables["system_model_config_versions"]
    assert RunModelConfigSnapshotRow.__table__ is Base.metadata.tables["run_model_config_snapshots"]


def test_system_model_orm_encodes_revision_identity_and_secret_free_snapshot_contract() -> None:
    config_table = SystemModelConfigRow.__table__
    version_table = SystemModelConfigVersionRow.__table__
    snapshot_table = RunModelConfigSnapshotRow.__table__

    indexes = {item.name: item for item in config_table.indexes if isinstance(item, Index)}
    assert indexes["uq_system_model_configs_logical_name"].unique is True
    assert "lower(system_model_configs.logical_name)" in str(indexes["uq_system_model_configs_logical_name"].expressions[0])

    config_constraints = {item.name for item in config_table.constraints if isinstance(item, (CheckConstraint, UniqueConstraint))}
    assert {
        "ck_system_model_configs_revision",
        "ck_system_model_configs_status",
        "uq_system_model_configs_id_current_version",
    }.issubset(config_constraints)

    version_columns = set(version_table.columns.keys())
    assert {
        "provider_adapter",
        "provider_model",
        "settings",
        "credential_id",
        "credential_version_id",
        "credential_env_key",
        "payload_checksum",
    }.issubset(version_columns)
    assert not ({"api_key", "token", "secret", "password"} & version_columns)

    snapshot_columns = set(snapshot_table.columns.keys())
    assert {
        "model_config_id",
        "model_config_version_id",
        "payload_checksum",
        "credential_id",
        "credential_version_id",
        "credential_env_key",
    }.issubset(snapshot_columns)


def test_full_schema_contains_model_catalog_and_immutable_snapshot_triggers() -> None:
    schema = (Path(__file__).parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql").read_text(encoding="utf-8")

    for table in (
        "system_model_catalog_state",
        "system_model_configs",
        "system_model_config_versions",
        "run_model_config_snapshots",
    ):
        assert f"CREATE TABLE {table} (" in schema
    assert ("CREATE TRIGGER trg_system_model_config_versions_immutable BEFORE UPDATE OR DELETE ON system_model_config_versions") in schema
    assert "CREATE OR REPLACE FUNCTION enforce_run_model_snapshot_credential_closure()" in schema
    assert "CREATE OR REPLACE FUNCTION reject_direct_run_model_snapshot_mutation()" in schema
    assert ("CREATE TRIGGER trg_run_model_config_snapshots_credential_closure BEFORE INSERT ON run_model_config_snapshots") in schema
    assert ("CREATE TRIGGER trg_run_model_config_snapshots_immutable BEFORE UPDATE OR DELETE ON run_model_config_snapshots FOR EACH ROW EXECUTE FUNCTION reject_direct_run_model_snapshot_mutation();") in schema
    assert "api_key" not in schema[schema.index("CREATE TABLE system_model_config_versions (") :]
    assert "INSERT INTO system_model_catalog_state (id, revision) VALUES (1, 1);" in schema


def test_public_projection_has_no_provider_settings_or_version_identity() -> None:
    assert {item.name for item in dataclasses.fields(PublicSystemModelView)} == {
        "logical_name",
        "display_name",
        "description",
        "supports_thinking",
        "supports_reasoning_effort",
        "supports_vision",
        "is_default",
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"api_key": "secret"},
        {"headers": {"Authorization": "Bearer secret"}},
        {"extra_body": [{"client_secret": "secret"}]},
        {"nested": {"refreshToken": "secret"}},
        {"nested": {"openai_api_key": "secret"}},
        {"nested": {"anthropic_auth_token": "secret"}},
        {"nested": {"password": "secret"}},
        {"nested": {"cookie": "secret"}},
        {"default_headers": {"x-safe-looking": "secret"}},
        {"oauth_config": {"endpoint": "https://example.invalid"}},
        {"envelope_nonce": "secret"},
        {"storage_locator": "secret"},
    ],
)
def test_model_settings_recursively_reject_secret_bearing_keys(
    settings: object,
) -> None:
    with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$") as error:
        validate_model_settings(settings)

    assert error.value.__dict__ == {}
    assert "secret" not in str(error.value).lower()


def test_model_settings_are_bounded_canonical_json_without_mutating_input() -> None:
    settings = {
        "temperature": 0.2,
        "extra_body": {
            "reasoning": {"effort": "medium"},
            "thinking": {"type": "enabled"},
        },
    }
    original = json.loads(json.dumps(settings))

    assert validate_model_settings(settings) == settings
    assert settings == original


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": True},
        {"stop": ["DONE"]},
        {"extra_body": {"stop": ["DONE"]}},
        {"extra_body": {"reasoning": {"effort": "medium", "summary": "full"}}},
        {"max_retries": True},
        {"max_retries": 21},
        {"max_tokens": 0},
        {"request_timeout": "120"},
        {"temperature": float("nan")},
        {"reasoning_effort": "custom"},
        {"when_thinking_enabled": {"extra_body": {"thinking": {"type": "disabled"}}}},
        {"when_thinking_disabled": {"extra_body": {"thinking": {"type": "enabled"}}}},
        {"when_thinking_enabled": {"extra_body": {"thinking": {"type": "custom"}}}},
    ],
)
def test_model_settings_reject_unknown_shapes_and_wrong_types(
    settings: object,
) -> None:
    with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
        validate_model_settings(settings)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user@example.invalid/v1",
        "https://user:password@example.invalid/v1",
        "https://example.invalid/v1?api-version=1",
        "https://example.invalid/v1?",
        "https://example.invalid/v1#models",
        "https://example.invalid/v1#",
        "https://example.invalid/\tv1",
        "ftp://example.invalid/v1",
        "https://example.invalid:99999/v1",
    ],
)
def test_model_settings_reject_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
        validate_model_settings({"base_url": base_url})


def test_model_settings_are_scoped_to_the_provider_adapter() -> None:
    assert validate_model_settings(
        {"reasoning_effort": "high", "retry_max_attempts": 2},
        provider_adapter="codex_cli",
    ) == {"reasoning_effort": "high", "retry_max_attempts": 2}

    with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
        validate_model_settings(
            {"base_url": "https://example.invalid/v1"},
            provider_adapter="codex_cli",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("logical_name", "sk-proj-example-secret-value"),
        ("display_name", "Bearer abcdefghijklmnop"),
        ("description", "api_key=must-not-be-public"),
        ("description", "See https://example.invalid/docs?token=redacted"),
        ("provider_model", "sk-example-provider-secret"),
    ],
)
def test_model_public_text_rejects_secret_like_values(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
        validate_create_system_model(_command(**{field: value}))


def test_provider_adapter_is_allowlisted_and_never_accepts_a_class_path() -> None:
    assert provider_class_path("openai") == "langchain_openai:ChatOpenAI"
    assert provider_class_path("patched_mimo") == ("deerflow.models.patched_mimo:PatchedChatMiMo")

    for value in (
        "langchain_openai:ChatOpenAI",
        "builtins:eval",
        "unknown",
    ):
        with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
            provider_class_path(value)


def test_patched_deepseek_accepts_the_current_v4_runtime_settings() -> None:
    command = validate_create_system_model(
        _command(
            provider_adapter="patched_deepseek",
            provider_model="deepseek-v4",
            credential_env_key="DEEPSEEK_API_KEY",
            settings={
                "base_url": "https://api.deepseek.com/v1",
                "request_timeout": 120,
                "max_retries": 3,
                "max_tokens": 8192,
                "temperature": 0.2,
                "reasoning_effort": "medium",
                "when_thinking_enabled": {"extra_body": {"thinking": {"type": "enabled"}}},
                "when_thinking_disabled": {"extra_body": {"thinking": {"type": "disabled"}}},
            },
        )
    )

    assert provider_class_path(command.provider_adapter) == ("deerflow.models.patched_deepseek:PatchedChatDeepSeek")
    assert command.credential_env_key == "DEEPSEEK_API_KEY"
    assert command.settings["max_tokens"] == 8192


def test_model_payload_checksum_is_deterministic_and_binds_exact_credential() -> None:
    command = validate_create_system_model(_command())
    checksum = canonical_model_payload_checksum(MODEL_ID, command)

    assert len(checksum) == 64
    assert checksum == canonical_model_payload_checksum(
        MODEL_ID,
        dataclasses.replace(
            command,
            settings={
                "extra_body": {"reasoning": {"effort": "medium"}},
                "temperature": 0.2,
                "base_url": "https://api.openai.com/v1",
            },
        ),
    )
    assert checksum != canonical_model_payload_checksum(
        MODEL_ID,
        dataclasses.replace(
            command,
            credential_version_id=uuid.UUID("50000000-0000-0000-0000-000000000002"),
        ),
    )


def test_provider_requiring_api_key_requires_complete_exact_credential_reference() -> None:
    for command in (
        _command(credential_id=None),
        _command(credential_version_id=None),
        _command(credential_env_key=None),
    ):
        with pytest.raises(ModelSettingsInvalid, match="^Model settings invalid$"):
            validate_create_system_model(command)

    cli_command = validate_create_system_model(
        _command(
            provider_adapter="codex_cli",
            settings={"reasoning_effort": "medium"},
            credential_id=None,
            credential_version_id=None,
            credential_env_key=None,
        )
    )
    assert cli_command.credential_id is None


@pytest.mark.asyncio
async def test_repository_selects_only_active_system_model_api_key_credentials() -> None:
    session = _CaptureSession([_ScalarResult(None)])

    with pytest.raises(SystemModelRepositoryInvariant):
        await SystemModelRepository(session).lock_system_credential_reference(  # type: ignore[arg-type]
            CREDENTIAL_ID,
            CREDENTIAL_VERSION_ID,
            "OPENAI_API_KEY",
            require_current=True,
            load_envelope=False,
        )

    statement = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "credentials.scope = 'system'" in statement
    assert "credentials.project_id IS NULL" in statement
    assert "credentials.credential_type = 'model_api_key'" in statement
    assert "credentials.status = 'active'" in statement
    assert "credentials.is_delete IS false" in statement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", "project"),
        ("project_id", uuid.UUID("60000000-0000-0000-0000-000000000001")),
        ("credential_type", "database"),
        ("status", "revoked"),
        ("is_delete", True),
    ],
)
async def test_repository_revalidates_returned_model_credential_rows(
    field: str,
    value: object,
) -> None:
    credential_values = {
        "id": CREDENTIAL_ID,
        "scope": "system",
        "project_id": None,
        "credential_type": "model_api_key",
        "status": "active",
        "is_delete": False,
        "current_version_id": CREDENTIAL_VERSION_ID,
    }
    credential_values[field] = value
    session = _CaptureSession(
        [_ScalarResult(SimpleNamespace(**credential_values))],
    )

    with pytest.raises(SystemModelRepositoryInvariant):
        await SystemModelRepository(session).lock_system_credential_reference(  # type: ignore[arg-type]
            CREDENTIAL_ID,
            CREDENTIAL_VERSION_ID,
            "OPENAI_API_KEY",
            require_current=True,
            load_envelope=False,
        )

    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_service_fails_closed_when_credential_repository_rejects_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ServiceSession()
    credential_calls: list[tuple[object, ...]] = []

    class _RejectingRepository:
        def __init__(self, passed_session: object) -> None:
            assert passed_session is session
            self.session = passed_session

        async def catalog_state(self, *, for_update: bool = False):
            assert for_update is True
            return SimpleNamespace(
                default_model_config_id=None,
                revision=1,
                updated_by_user_id=None,
            )

        async def lock_system_credential_reference(
            self,
            credential_id: object,
            credential_version_id: object,
            credential_env_key: object,
            *,
            require_current: bool,
            load_envelope: bool,
        ):
            credential_calls.append(
                (
                    credential_id,
                    credential_version_id,
                    credential_env_key,
                    require_current,
                    load_envelope,
                )
            )
            raise SystemModelRepositoryInvariant

    monkeypatch.setattr(
        "app.system_settings.service.SystemModelRepository",
        _RejectingRepository,
    )
    context = resolve_system_audit_context(
        SimpleNamespace(id=USER_ID, system_role="system_admin"),
        request_id="reject-non-model-credential",
    )

    with pytest.raises(SystemModelInvalid) as error:
        await SystemModelCatalogService(lambda: session).create_model(
            context,
            _command(),
        )

    assert error.value.request_id == "reject-non-model-credential"
    assert credential_calls == [
        (
            CREDENTIAL_ID,
            CREDENTIAL_VERSION_ID,
            "OPENAI_API_KEY",
            True,
            False,
        )
    ]
    assert session.role_checks == 1


def test_materializer_returns_only_runtime_model_config_with_masked_api_key() -> None:
    keyring = _keyring()
    envelope = encrypt_credential_payload(
        {"env": {"OPENAI_API_KEY": "runtime-super-secret"}},
        AssetScope.SYSTEM,
        None,
        CREDENTIAL_VERSION_ID,
        keyring,
    )
    material = LockedSystemModelMaterial(
        model=SystemModelConfigRow(
            id=MODEL_ID,
            logical_name="primary-openai",
            display_name="Primary OpenAI",
            description="Primary model",
            status="active",
            current_version_id=MODEL_VERSION_ID,
            revision=1,
            sort_order=0,
            created_by_user_id=str(USER_ID),
            updated_by_user_id=str(USER_ID),
        ),
        version=SystemModelConfigVersionRow(
            id=MODEL_VERSION_ID,
            model_config_id=MODEL_ID,
            version_number=1,
            provider_adapter="openai",
            provider_model="gpt-5",
            settings={"temperature": 0.2},
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=True,
            credential_id=CREDENTIAL_ID,
            credential_version_id=CREDENTIAL_VERSION_ID,
            credential_env_key="OPENAI_API_KEY",
            payload_checksum="a" * 64,
            created_by_user_id=str(USER_ID),
        ),
        credential=CredentialRow(
            id=CREDENTIAL_ID,
            scope="system",
            project_id=None,
            name="openai",
            display_name="OpenAI",
            credential_type="model_api_key",
            status="active",
            is_delete=False,
            current_version_id=CREDENTIAL_VERSION_ID,
            version=1,
            created_by_user_id=str(USER_ID),
        ),
        credential_version=CredentialVersionRow(
            id=CREDENTIAL_VERSION_ID,
            credential_id=CREDENTIAL_ID,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": ["OPENAI_API_KEY"]},
            created_by_user_id=str(USER_ID),
        ),
        envelope=CredentialEnvelopeRow(
            credential_version_id=CREDENTIAL_VERSION_ID,
            envelope_generation=1,
            key_id=envelope.key_id,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            is_active=True,
            created_by_user_id=str(USER_ID),
        ),
    )

    runtime = SystemModelCredentialAdapter(keyring=keyring).materialize(material)

    assert type(runtime) is ModelConfig
    assert runtime.name == "primary-openai"
    assert runtime.use == "langchain_openai:ChatOpenAI"
    assert runtime.model == "gpt-5"
    assert isinstance(runtime.api_key, SecretStr)
    assert runtime.api_key.get_secret_value() == "runtime-super-secret"
    assert "runtime-super-secret" not in repr(runtime)
    assert "runtime-super-secret" not in runtime.model_dump_json()
    assert "runtime-super-secret" not in repr(material)


def test_materializer_fails_closed_without_echoing_secret() -> None:
    keyring = _keyring()
    envelope = encrypt_credential_payload(
        {"env": {"WRONG_KEY": "runtime-super-secret"}},
        AssetScope.SYSTEM,
        None,
        CREDENTIAL_VERSION_ID,
        keyring,
    )
    material = LockedSystemModelMaterial(
        model=SystemModelConfigRow(
            id=MODEL_ID,
            logical_name="primary-openai",
            display_name="Primary OpenAI",
            description="Primary model",
            status="active",
            current_version_id=MODEL_VERSION_ID,
            revision=1,
            sort_order=0,
            created_by_user_id=str(USER_ID),
            updated_by_user_id=str(USER_ID),
        ),
        version=SystemModelConfigVersionRow(
            id=MODEL_VERSION_ID,
            model_config_id=MODEL_ID,
            version_number=1,
            provider_adapter="openai",
            provider_model="gpt-5",
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            credential_id=CREDENTIAL_ID,
            credential_version_id=CREDENTIAL_VERSION_ID,
            credential_env_key="OPENAI_API_KEY",
            payload_checksum="a" * 64,
            created_by_user_id=str(USER_ID),
        ),
        credential=CredentialRow(
            id=CREDENTIAL_ID,
            scope="system",
            project_id=None,
            name="openai",
            display_name="OpenAI",
            credential_type="model_api_key",
            status="active",
            is_delete=False,
            current_version_id=CREDENTIAL_VERSION_ID,
            version=1,
            created_by_user_id=str(USER_ID),
        ),
        credential_version=CredentialVersionRow(
            id=CREDENTIAL_VERSION_ID,
            credential_id=CREDENTIAL_ID,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": ["WRONG_KEY"]},
            created_by_user_id=str(USER_ID),
        ),
        envelope=CredentialEnvelopeRow(
            credential_version_id=CREDENTIAL_VERSION_ID,
            envelope_generation=1,
            key_id=envelope.key_id,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            is_active=True,
            created_by_user_id=str(USER_ID),
        ),
    )

    with pytest.raises(
        SystemModelMaterializationUnavailable,
        match="^System model materialization unavailable$",
    ) as error:
        SystemModelCredentialAdapter(keyring=keyring).materialize(material)

    assert error.value.__dict__ == {}
    assert "runtime-super-secret" not in str(error.value)


def test_materializer_rejects_non_model_credential_type() -> None:
    keyring = _keyring()
    envelope = encrypt_credential_payload(
        {"env": {"OPENAI_API_KEY": "runtime-super-secret"}},
        AssetScope.SYSTEM,
        None,
        CREDENTIAL_VERSION_ID,
        keyring,
    )
    material = LockedSystemModelMaterial(
        model=SystemModelConfigRow(
            id=MODEL_ID,
            logical_name="primary-openai",
            display_name="Primary OpenAI",
            description="Primary model",
            status="active",
            current_version_id=MODEL_VERSION_ID,
            revision=1,
            sort_order=0,
            created_by_user_id=str(USER_ID),
            updated_by_user_id=str(USER_ID),
        ),
        version=SystemModelConfigVersionRow(
            id=MODEL_VERSION_ID,
            model_config_id=MODEL_ID,
            version_number=1,
            provider_adapter="openai",
            provider_model="gpt-5",
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            credential_id=CREDENTIAL_ID,
            credential_version_id=CREDENTIAL_VERSION_ID,
            credential_env_key="OPENAI_API_KEY",
            payload_checksum="a" * 64,
            created_by_user_id=str(USER_ID),
        ),
        credential=CredentialRow(
            id=CREDENTIAL_ID,
            scope="system",
            project_id=None,
            name="database",
            display_name="Database",
            credential_type="database",
            status="active",
            is_delete=False,
            current_version_id=CREDENTIAL_VERSION_ID,
            version=1,
            created_by_user_id=str(USER_ID),
        ),
        credential_version=CredentialVersionRow(
            id=CREDENTIAL_VERSION_ID,
            credential_id=CREDENTIAL_ID,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": ["OPENAI_API_KEY"]},
            created_by_user_id=str(USER_ID),
        ),
        envelope=CredentialEnvelopeRow(
            credential_version_id=CREDENTIAL_VERSION_ID,
            envelope_generation=1,
            key_id=envelope.key_id,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            is_active=True,
            created_by_user_id=str(USER_ID),
        ),
    )

    with pytest.raises(
        SystemModelMaterializationUnavailable,
        match="^System model materialization unavailable$",
    ):
        SystemModelCredentialAdapter(keyring=keyring).materialize(material)


def test_materializer_rejects_a_required_provider_without_credential_material() -> None:
    material = LockedSystemModelMaterial(
        model=SystemModelConfigRow(
            id=MODEL_ID,
            logical_name="primary-openai",
            display_name="Primary OpenAI",
            description="Primary model",
            status="active",
            current_version_id=MODEL_VERSION_ID,
            revision=1,
            sort_order=0,
            created_by_user_id=str(USER_ID),
            updated_by_user_id=str(USER_ID),
        ),
        version=SystemModelConfigVersionRow(
            id=MODEL_VERSION_ID,
            model_config_id=MODEL_ID,
            version_number=1,
            provider_adapter="openai",
            provider_model="gpt-5",
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            credential_id=None,
            credential_version_id=None,
            credential_env_key=None,
            payload_checksum="a" * 64,
            created_by_user_id=str(USER_ID),
        ),
    )

    with pytest.raises(
        SystemModelMaterializationUnavailable,
        match="^System model materialization unavailable$",
    ):
        SystemModelCredentialAdapter(keyring=_keyring()).materialize(material)


@pytest.mark.asyncio
async def test_service_requires_an_issued_system_admin_context_before_storage() -> None:
    opened = False

    def session_factory():
        nonlocal opened
        opened = True
        raise AssertionError("storage must not open for an unissued context")

    service = SystemModelCatalogService(session_factory)

    with pytest.raises(PermissionError, match="^System model administration required$"):
        await service.list_models(object())

    assert opened is False
    assert not hasattr(service, "delete")

    issued = resolve_system_audit_context(
        type("Admin", (), {"id": USER_ID, "system_role": "system_admin"})(),
        request_id="request-1",
    )
    assert issued.user_id == USER_ID


def test_test_keyring_fixture_is_canonical() -> None:
    encoded = b64encode(b"k" * 32).decode("ascii")
    assert json.loads(json.dumps({"test-key": encoded})) == {"test-key": encoded}
