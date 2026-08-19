from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.shared_assets.errors import (
    SkillCredentialBindingInvalid,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)
from app.shared_assets.skill_credential_policy import (
    SkillCredentialBindingInput,
    credential_is_eligible,
    normalize_binding_inputs,
    parse_secret_requirements,
    require_complete_bindings,
    validate_selected_credential,
)


def _record(
    env: object,
    *,
    credential_status: str = "active",
    version_status: str = "active",
    current: bool = True,
):
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    return SimpleNamespace(
        credential=SimpleNamespace(
            id=credential_id,
            scope="project",
            status=credential_status,
            is_delete=False,
            current_version_id=version_id if current else uuid.uuid4(),
        ),
        version=SimpleNamespace(
            id=version_id,
            credential_id=credential_id,
            status=version_status,
            payload_schema={"env": env},
        ),
    )


def test_policy_parses_strict_requirements_and_rejects_duplicate_names() -> None:
    assert parse_secret_requirements(
        [
            {"name": "API_KEY", "optional": False},
            {"name": "OPTIONAL_TOKEN", "optional": True},
        ],
        request_id="req-policy",
    ) == (("API_KEY", False), ("OPTIONAL_TOKEN", True))

    with pytest.raises(SkillCredentialBindingInvalid):
        parse_secret_requirements(
            [
                {"name": "API_KEY", "optional": False},
                {"name": "API_KEY", "optional": True},
            ],
            request_id="req-policy",
        )


def test_policy_normalizes_binding_inputs_without_accepting_duplicates() -> None:
    version_id = uuid.uuid4()
    assert normalize_binding_inputs(
        (SkillCredentialBindingInput("API_KEY", version_id),),
        request_id="req-policy",
    ) == (SkillCredentialBindingInput("API_KEY", version_id),)

    with pytest.raises(SkillCredentialBindingInvalid):
        normalize_binding_inputs(
            (
                SkillCredentialBindingInput("API_KEY", version_id),
                SkillCredentialBindingInput("API_KEY", uuid.uuid4()),
            ),
            request_id="req-policy",
        )

    # Canonical frontmatter parsing remains compatible with historical long
    # POSIX names, but the binding write boundary must fail before PostgreSQL's
    # varchar(255) column does.
    with pytest.raises(SkillCredentialBindingInvalid):
        normalize_binding_inputs(
            (SkillCredentialBindingInput("A" * 256, version_id),),
            request_id="req-policy",
        )


def test_policy_distinguishes_schema_mismatch_from_stale_credential() -> None:
    eligible = _record(["API_KEY"])
    assert credential_is_eligible(eligible, "API_KEY")
    assert not credential_is_eligible(
        _record(["A" * 256]),
        "A" * 256,
    )
    validate_selected_credential(
        eligible,
        "API_KEY",
        active_envelope=True,
        request_id="req-policy",
    )

    with pytest.raises(SkillCredentialBindingInvalid):
        validate_selected_credential(
            _record(["OTHER_KEY"]),
            "API_KEY",
            active_envelope=True,
            request_id="req-policy",
        )

    with pytest.raises(SkillCredentialSelectionStale):
        validate_selected_credential(
            _record(["API_KEY"], current=False),
            "API_KEY",
            active_envelope=True,
            request_id="req-policy",
        )

    with pytest.raises(SkillCredentialSelectionStale):
        validate_selected_credential(
            eligible,
            "API_KEY",
            active_envelope=False,
            request_id="req-policy",
        )


def test_policy_requires_all_non_optional_bindings_for_active_skill() -> None:
    requirements = (("API_KEY", False), ("OPTIONAL_TOKEN", True))
    require_complete_bindings(
        requirements,
        configured_names=frozenset({"API_KEY"}),
        request_id="req-policy",
    )

    with pytest.raises(SkillCredentialBindingsIncomplete):
        require_complete_bindings(
            requirements,
            configured_names=frozenset(),
            request_id="req-policy",
        )
