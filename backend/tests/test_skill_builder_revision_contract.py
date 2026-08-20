from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.gateway.routers.project_assets import raise_asset_domain
from app.gateway.routers.project_skill_builder import (
    CreateSkillDesignSessionRequest,
    _session_item,
)
from app.private_work.skill_builder_run_admission import SkillBuilderRunAdmissionService
from app.shared_assets.errors import (
    AssetValidationFailed,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
)
from app.shared_assets.skill_builder_agent_runtime import _SYSTEM_PROMPT
from app.shared_assets.skill_design_service import (
    SkillDesignGenerationRequest,
    SkillDesignSessionView,
    SkillDesignStatus,
)


def test_session_response_exposes_durable_exact_created_version() -> None:
    skill_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    version_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    now = datetime(2026, 8, 19, tzinfo=UTC)
    response = _session_item(
        SkillDesignSessionView(
            id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            owner_user_id="owner-1",
            thread_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
            slug="catalog-auditor",
            display_name="Catalog auditor",
            status=SkillDesignStatus.COMPLETED,
            revision=7,
            messages=(),
            active_clarification=None,
            progress=(),
            files=(),
            draft_checksum="a" * 64,
            validation=None,
            error_code=None,
            error_message=None,
            created_skill_id=skill_id,
            created_skill_version_id=version_id,
            created_at=now,
            updated_at=now,
        )
    )

    assert response.created_skill_id == skill_id
    assert response.created_skill_version_id == version_id


def test_create_session_request_defaults_to_create_and_rejects_mixed_fields() -> None:
    created = CreateSkillDesignSessionRequest.model_validate(
        {
            "slug": "catalog-auditor",
            "display_name": "catalog-auditor",
            "idempotency_key": "create-1",
        }
    )
    assert created.kind == "create"
    assert created.skill_id is None

    skill_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    revised = CreateSkillDesignSessionRequest.model_validate(
        {
            "kind": "revise",
            "skill_id": skill_id,
            "idempotency_key": "revise-1",
        }
    )
    assert revised.kind == "revise"
    assert revised.slug is None
    assert revised.skill_id == skill_id

    with pytest.raises(ValidationError):
        CreateSkillDesignSessionRequest.model_validate(
            {
                "kind": "revise",
                "slug": "catalog-auditor",
                "display_name": "catalog-auditor",
                "skill_id": skill_id,
                "idempotency_key": "revise-2",
            }
        )
    with pytest.raises(ValidationError):
        CreateSkillDesignSessionRequest.model_validate(
            {
                "kind": "create",
                "skill_id": skill_id,
                "idempotency_key": "create-2",
            }
        )


def test_revision_errors_map_to_contract_status_codes() -> None:
    expected = {
        SkillDesignTargetUnsupported: 422,
        SkillDesignTargetSessionExists: 409,
        SkillDesignTargetDeleted: 409,
        SkillDesignNoChanges: 409,
    }
    for error_type, status_code in expected.items():
        with pytest.raises(HTTPException) as exc_info:
            raise_asset_domain(error_type("req-revision"))
        assert exc_info.value.status_code == status_code
        assert exc_info.value.detail["code"] == error_type.code
        assert exc_info.value.detail["request_id"] == "req-revision"


def test_run_input_payload_isolates_revise_authoring_from_conversation() -> None:
    request = SkillDesignGenerationRequest(
        skill_slug="catalog-auditor",
        skill_name="Catalog auditor",
        brief="Tighten the published instructions.",
    )
    payload = SkillBuilderRunAdmissionService._run_input_payload(
        request,
        turn_message="Tighten the published instructions.",
        first_turn=True,
        draft_checksum="a" * 64,
        authoring_kind="revise",
        base_version_number=3,
        request_id="req-1",
    )
    assert payload["authoring"] == {
        "kind": "revise",
        "target_slug": "catalog-auditor",
        "base_version_number": 3,
    }
    assert payload["conversation"]["mode"] == "initial"

    create_payload = SkillBuilderRunAdmissionService._run_input_payload(
        request,
        turn_message="Tighten the published instructions.",
        first_turn=False,
        draft_checksum=None,
        request_id="req-1",
    )
    assert create_payload["authoring"] == {"kind": "create"}

    with pytest.raises(AssetValidationFailed):
        SkillBuilderRunAdmissionService._run_input_payload(
            request,
            turn_message="Tighten the published instructions.",
            first_turn=True,
            draft_checksum="a" * 64,
            authoring_kind="revise",
            base_version_number=None,
            request_id="req-1",
        )


def test_builder_system_prompt_covers_revision_authoring() -> None:
    assert "`authoring` block" in _SYSTEM_PROMPT
    assert "never change the\n  frontmatter `name`" in _SYSTEM_PROMPT


def test_skill_builder_session_summary_exposes_session_kind() -> None:
    from datetime import UTC, datetime

    from app.gateway.routers.project_skill_builder import (
        SkillDesignSessionSummaryResponse,
    )
    from app.shared_assets.skill_design_service import (
        SkillDesignSessionSummary,
        SkillDesignStatus,
    )

    response = SkillDesignSessionSummaryResponse.model_validate(
        SkillDesignSessionSummary(
            id=uuid.UUID("00000000-0000-4000-8000-000000000010"),
            slug="catalog-auditor",
            display_name="catalog-auditor",
            status=SkillDesignStatus.DRAFT_READY,
            revision=1,
            updated_at=datetime(2026, 8, 14, tzinfo=UTC),
            session_kind="revise",
        ),
        from_attributes=True,
    )
    assert response.session_kind == "revise"
