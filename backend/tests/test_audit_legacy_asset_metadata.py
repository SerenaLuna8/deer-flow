from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditMetadataRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
)
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow


def _row(
    metadata: dict[str, object],
    *,
    action: AuditAction = AuditAction.ASSET_PUBLISHED,
) -> AuditLogRow:
    project_id = uuid.uuid4()
    return AuditLogRow(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        actor_user_id=str(uuid.uuid4()),
        actor_process=None,
        actor_platform_role=None,
        project_id=project_id,
        action=action.value,
        target_kind=AuditTargetKind.ASSET.value,
        target_ref_key_id="audit-test",
        target_ref_hmac="a" * 64,
        outcome=AuditOutcome.SUCCESS.value,
        public_error_code=None,
        request_id=None,
        job_id=None,
        attempt_id=None,
        metadata_json=metadata,
    )


@pytest.mark.parametrize("asset_kind", ("agent", "skill", "mcp"))
def test_read_projection_preserves_legacy_asset_metadata_without_inference(
    asset_kind: str,
) -> None:
    record = AuditService._record(_row({"asset_kind": asset_kind}))

    assert record.action is AuditAction.ASSET_PUBLISHED
    assert record.metadata == {"asset_kind": asset_kind}
    assert "operation" not in record.metadata


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"asset_kind": "credential"},
        {"asset_kind": "agent", "operation": None},
        {"asset_kind": "agent", "version_number": 7},
    ),
)
def test_read_projection_rejects_shapes_that_were_never_valid_legacy_metadata(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(AuditMetadataRejected):
        AuditService._record(_row(metadata))


def test_read_projection_does_not_apply_asset_fallback_to_other_actions() -> None:
    with pytest.raises(AuditMetadataRejected):
        AuditService._record(
            _row(
                {"asset_kind": "agent"},
                action=AuditAction.PROJECT_CREATED,
            )
        )


@pytest.mark.asyncio
async def test_append_does_not_accept_the_legacy_asset_metadata_shape() -> None:
    project_id = uuid.uuid4()
    service = AuditService(
        None,
        AuditHmacKeyring(
            active_key_id="audit-test",
            _keys={"audit-test": b"a" * 32},
        ),
    )

    with pytest.raises(AuditMetadataRejected):
        await service.append(
            object(),  # type: ignore[arg-type]
            AuditActor.user(uuid.uuid4()),
            AuditAction.ASSET_PUBLISHED,
            AuditTarget(
                kind=AuditTargetKind.ASSET,
                authority_id=uuid.uuid4(),
                project_id=project_id,
            ),
            AuditOutcome.SUCCESS,
            {"asset_kind": "agent"},
        )
