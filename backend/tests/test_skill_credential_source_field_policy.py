from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.shared_assets.skill_credential_policy import (
    SkillCredentialBindingInput,
    credential_is_eligible,
    normalize_binding_inputs,
    validate_selected_credential,
)
from deerflow.persistence.private_work.model import RunSkillCredentialSnapshotRow
from deerflow.persistence.shared_assets.skill_credential_model import (
    ProjectSkillCredentialBindingRow,
)


def _record(*, env_fields: list[str]):
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    return SimpleNamespace(
        credential=SimpleNamespace(
            id=credential_id,
            scope="project",
            status="active",
            is_delete=False,
            current_version_id=version_id,
        ),
        version=SimpleNamespace(
            id=version_id,
            credential_id=credential_id,
            status="active",
            payload_schema={"env": env_fields},
        ),
    )


def test_binding_input_preserves_distinct_target_and_source_env_names() -> None:
    credential_version_id = uuid.uuid4()

    binding = SkillCredentialBindingInput(
        name="TEXT_ROUTE_DB_NAME",
        credential_version_id=credential_version_id,
        source_env_field_name="db.name",
    )

    assert normalize_binding_inputs(
        (binding,),
        request_id="req-source-field",
    ) == (binding,)
    assert binding.name == "TEXT_ROUTE_DB_NAME"
    assert binding.source_env_field_name == "db.name"


def test_source_env_field_eligibility_is_independent_from_target_name() -> None:
    record = _record(env_fields=["db.name", "DB_PASSWORD"])

    assert credential_is_eligible(record, "db.name") is True
    assert credential_is_eligible(record, "TEXT_ROUTE_DB_NAME") is False
    validate_selected_credential(
        record,
        "db.name",
        active_envelope=True,
        request_id="req-source-field",
    )


def test_binding_and_run_snapshot_persist_source_env_field_name() -> None:
    assert "source_env_field_name" in ProjectSkillCredentialBindingRow.__table__.columns
    assert "source_env_field_name" in RunSkillCredentialSnapshotRow.__table__.columns
