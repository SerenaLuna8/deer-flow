from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.audit.models import (
    AUDIT_ACTION_CONTRACTS,
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditScope,
    AuditTargetKind,
)

WORKFLOW_CONTROL_AUDIT_ACTIONS = {
    AuditAction.WORKFLOW_DEFINITION_CREATED,
    AuditAction.WORKFLOW_DEFINITION_UPDATED,
    AuditAction.WORKFLOW_DEFINITION_ARCHIVED,
    AuditAction.WORKFLOW_DRAFT_SAVED,
    AuditAction.WORKFLOW_VERSION_PUBLISHED,
    AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_UPDATED,
    AuditAction.WORKFLOW_DRAFT_GRANT_INTENT_DELETED,
    AuditAction.WORKFLOW_VERSION_GRANT_UPDATED,
    AuditAction.WORKFLOW_VERSION_GRANT_REVOKED,
}


def test_workflow_control_audit_actions_are_project_user_content_free() -> None:
    assert {action.value for action in WORKFLOW_CONTROL_AUDIT_ACTIONS} == {
        "workflow.definition_created",
        "workflow.definition_updated",
        "workflow.definition_archived",
        "workflow.draft_saved",
        "workflow.version_published",
        "workflow.draft_grant_intent_updated",
        "workflow.draft_grant_intent_deleted",
        "workflow.version_grant_updated",
        "workflow.version_grant_revoked",
    }

    for action in WORKFLOW_CONTROL_AUDIT_ACTIONS:
        contract = AUDIT_ACTION_CONTRACTS[action]
        assert contract.target_kind is AuditTargetKind.WORKFLOW
        assert contract.authority_matches_project is False
        assert len(contract.variants) == 1
        assert contract.variants[0].scope is AuditScope.PROJECT
        assert contract.variants[0].actor == "user"
        assert AUDIT_METADATA_MODELS[action].model_validate({}).model_dump() == {}
        with pytest.raises(ValidationError):
            AUDIT_METADATA_MODELS[action].model_validate(
                {
                    "spec": {"secret": "do-not-log"},
                    "credential_id": str(uuid.uuid4()),
                    "idempotency_key": "raw-key",
                }
            )
