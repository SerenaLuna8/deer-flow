from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.system_settings.execution_adapter import (
    SystemModelExecutionAdapter,
    SystemModelMaterializationUnavailable,
)
from app.system_settings.models import (
    CreateSystemModel,
    FrozenSystemModelExecution,
    LockedSystemModelMaterial,
)
from app.system_settings.secrets import (
    model_secret_envelope_digest,
    model_secret_recipient,
)
from app.system_settings.validation import (
    canonical_model_payload,
    canonical_model_payload_checksum,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)
from deerflow.secrets import SecretEnvelope, SecretKey


def _material() -> tuple[
    SecretKey,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    dict[str, object],
]:
    model_id = uuid.UUID("10000000-0000-4000-8000-000000000001")
    generation_id = uuid.UUID("20000000-0000-4000-8000-000000000001")
    command = CreateSystemModel(
        display_name="DeepSeek Flash",
        status="active",
        provider_adapter="patched_deepseek",
        provider_model="deepseek-v4-flash",
        settings={"base_url": "https://api.deepseek.com"},
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
        api_key=None,
    )
    payload = canonical_model_payload(model_id, command)
    key = SecretKey(b"k" * 32)
    recipient = model_secret_recipient(
        model_id,
        command.provider_adapter,
        command.settings,
    )
    envelope = SecretEnvelope.protect(
        b"runtime-only-api-key",
        recipient=recipient,
        key=key,
    )
    generation = SystemModelSecretGenerationRow(
        id=generation_id,
        model_config_id=model_id,
        revision=1,
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
        envelope_digest=model_secret_envelope_digest(recipient, envelope),
        created_by_user_id="00000000-0000-4000-8000-000000000001",
        created_at=datetime.now(UTC),
    )
    model = SystemModelConfigRow(
        id=model_id,
        display_name=command.display_name,
        status="active",
        provider_adapter=command.provider_adapter,
        provider_model=command.provider_model,
        settings=dict(command.settings),
        supports_thinking=command.supports_thinking,
        supports_reasoning_effort=command.supports_reasoning_effort,
        supports_vision=command.supports_vision,
        payload_checksum=canonical_model_payload_checksum(model_id, command),
        current_secret_generation_id=generation_id,
        secret_revision=1,
        revision=1,
        created_by_user_id="00000000-0000-4000-8000-000000000001",
        updated_by_user_id="00000000-0000-4000-8000-000000000001",
    )
    return key, model, generation, payload


def test_model_execution_materializes_only_the_exact_owned_generation() -> None:
    key, model, generation, _payload = _material()

    runtime = SystemModelExecutionAdapter(secret_key=key).materialize(
        LockedSystemModelMaterial(
            model=model,
            secret_generation=generation,
        )
    )

    assert runtime.api_key.get_secret_value() == "runtime-only-api-key"
    assert runtime._system_model_config_id == model.id
    assert runtime._system_model_payload_checksum == model.payload_checksum
    assert runtime._system_model_secret_generation_id == generation.id
    assert "runtime-only-api-key" not in repr(runtime)


def test_run_snapshot_keeps_payload_but_destroyed_generation_fails_closed() -> None:
    key, model, generation, payload = _material()
    snapshot = RunModelConfigSnapshotRow(
        project_id=uuid.UUID("30000000-0000-4000-8000-000000000001"),
        owner_user_id="00000000-0000-4000-8000-000000000001",
        thread_id="thread-1",
        run_id="run-1",
        purpose="lead",
        model_config_id=model.id,
        provider_payload=payload,
        payload_checksum=model.payload_checksum,
        secret_generation_id=generation.id,
        secret_envelope_digest=generation.envelope_digest,
    )
    execution = FrozenSystemModelExecution(
        model_config_id=model.id,
        provider_payload=dict(snapshot.provider_payload),
        payload_checksum=snapshot.payload_checksum,
        secret_generation_id=snapshot.secret_generation_id,
        secret_envelope_digest=snapshot.secret_envelope_digest,
    )
    model.provider_model = "later-model-edit"

    runtime = SystemModelExecutionAdapter(secret_key=key).materialize(
        LockedSystemModelMaterial(
            model=model,
            secret_generation=generation,
            execution=execution,
        )
    )
    assert runtime.model == "deepseek-v4-flash"

    with pytest.raises(
        SystemModelMaterializationUnavailable,
        match="^System model materialization unavailable$",
    ):
        SystemModelExecutionAdapter(secret_key=key).materialize(
            LockedSystemModelMaterial(
                model=model,
                secret_generation=None,
                execution=execution,
            )
        )
