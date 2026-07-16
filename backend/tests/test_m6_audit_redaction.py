from __future__ import annotations

import uuid

import pytest

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditMetadataRejected,
    AuditOutcome,
    AuditPlatformRole,
    AuditProcess,
    AuditTarget,
    AuditTargetKind,
)
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring


def _keyring(active: str = "audit-v2") -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id=active,
        _keys={"audit-v1": b"1" * 32, "audit-v2": b"2" * 32},
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "forbidden",
    [
        "prompt",
        "message",
        "memory",
        "path",
        "filename",
        "token",
        "exception",
        "credential",
        "payload",
    ],
)
async def test_metadata_rejects_private_or_free_form_fields(forbidden: str) -> None:
    secret = f"private-{uuid.uuid4()}"
    service = AuditService(None, _keyring())

    with pytest.raises(AuditMetadataRejected) as exc_info:
        await service.append(
            object(),
            AuditActor.user(uuid.uuid4()),
            AuditAction.RUN_ADMITTED,
            AuditTarget(
                kind=AuditTargetKind.RUN,
                authority_id=uuid.uuid4(),
                project_id=uuid.uuid4(),
            ),
            AuditOutcome.SUCCESS,
            {
                "job_type": "private_run",
                "non_interactive": False,
                forbidden: secret,
            },
        )

    assert str(exc_info.value) == "Audit metadata was rejected"
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


@pytest.mark.anyio
async def test_action_requires_enum_and_action_specific_metadata() -> None:
    service = AuditService(None, _keyring())
    actor = AuditActor.user(uuid.uuid4())
    target = AuditTarget(
        kind=AuditTargetKind.RUN,
        authority_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )

    with pytest.raises(AuditMetadataRejected):
        await service.append(
            object(),
            actor,
            "run.admitted",
            target,
            AuditOutcome.SUCCESS,
            {"job_type": "private_run", "non_interactive": False},
        )
    with pytest.raises(AuditMetadataRejected):
        await service.append(
            object(),
            actor,
            AuditAction.RUN_ADMITTED,
            target,
            AuditOutcome.SUCCESS,
            {"job_type": "retention_purge", "non_interactive": False},
        )


def test_actor_and_target_authority_are_strict() -> None:
    with pytest.raises(AuditAuthorityRejected):
        AuditActor(user_id=uuid.uuid4(), process="worker")
    with pytest.raises(AuditAuthorityRejected):
        AuditActor(user_id=None, process=None)
    with pytest.raises(AuditAuthorityRejected):
        AuditTarget(
            kind=AuditTargetKind.RUN,
            authority_id="raw-private-run-id",
            project_id=uuid.uuid4(),
        )


@pytest.mark.anyio
async def test_service_revalidates_frozen_contract_snapshots() -> None:
    service = AuditService(None, _keyring())
    actor = AuditActor.user(uuid.uuid4())
    target = AuditTarget(
        kind=AuditTargetKind.RUN,
        authority_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )
    object.__setattr__(actor, "process", "worker")
    object.__setattr__(target, "authority_id", "private-run")

    with pytest.raises(AuditAuthorityRejected):
        await service.append(
            object(),
            actor,
            AuditAction.RUN_ADMITTED,
            target,
            AuditOutcome.SUCCESS,
            {"job_type": "private_run", "non_interactive": False},
        )


@pytest.mark.anyio
async def test_action_contract_binds_target_scope_and_elevated_actor() -> None:
    service = AuditService(None, _keyring())
    forged_system_actor = AuditActor(
        user_id=uuid.uuid4(),
        platform_role=AuditPlatformRole.SYSTEM_ADMIN,
    )
    forged_process_actor = AuditActor(process=AuditProcess.WORKER)

    for actor, target in (
        (
            AuditActor.user(uuid.uuid4()),
            AuditTarget(
                kind=AuditTargetKind.BACKUP,
                authority_id=uuid.uuid4(),
                project_id=None,
            ),
        ),
        (
            forged_system_actor,
            AuditTarget(
                kind=AuditTargetKind.RUN,
                authority_id=uuid.uuid4(),
                project_id=uuid.uuid4(),
            ),
        ),
        (
            forged_process_actor,
            AuditTarget(
                kind=AuditTargetKind.RUN,
                authority_id=uuid.uuid4(),
                project_id=uuid.uuid4(),
            ),
        ),
    ):
        with pytest.raises(AuditAuthorityRejected):
            await service.append(
                object(),
                actor,
                AuditAction.RUN_ADMITTED,
                target,
                AuditOutcome.SUCCESS,
                {"job_type": "private_run", "non_interactive": False},
            )


@pytest.mark.anyio
async def test_action_contract_uses_joint_scope_actor_and_metadata_variants() -> None:
    service = AuditService(None, _keyring())
    project_id = uuid.uuid4()
    scheduler = AuditActor.trusted_process(AuditProcess.SCHEDULER)
    cases = (
        (
            AuditActor.user(uuid.uuid4()),
            AuditAction.ASSET_CREATED,
            AuditTarget(AuditTargetKind.ASSET, uuid.uuid4(), None),
            {"asset_kind": "skill"},
        ),
        (
            AuditActor.user(uuid.uuid4()),
            AuditAction.PROJECT_CREATED,
            AuditTarget(AuditTargetKind.PROJECT, uuid.uuid4(), project_id),
            {},
        ),
        (
            scheduler,
            AuditAction.AUTOMATION_TRIGGERED,
            AuditTarget(AuditTargetKind.AUTOMATION, uuid.uuid4(), project_id),
            {"trigger_kind": "manual"},
        ),
        (
            scheduler,
            AuditAction.RUN_ADMITTED,
            AuditTarget(AuditTargetKind.RUN, uuid.uuid4(), project_id),
            {"job_type": "private_run", "non_interactive": False},
        ),
    )

    for actor, action, target, metadata in cases:
        with pytest.raises(AuditAuthorityRejected):
            await service.append(
                object(),
                actor,
                action,
                target,
                AuditOutcome.SUCCESS,
                metadata,
            )


def test_platform_account_purge_has_an_explicit_recovery_variant() -> None:
    actor = AuditActor.trusted_process(AuditProcess.RECOVERY)
    target = AuditTarget(AuditTargetKind.PURGE, uuid.uuid4(), None)

    assert AuditService._action_authorized(
        actor,
        AuditAction.PURGE_COMPLETED,
        target,
        {"resource_kind": "account", "purged_count": 1},
    )
    assert not AuditService._action_authorized(
        actor,
        AuditAction.PURGE_COMPLETED,
        target,
        {"resource_kind": "file", "purged_count": 1},
    )


def test_request_ref_accepts_trace_contract_without_preserving_input() -> None:
    keyring = _keyring()
    request_id = "S" * 512

    ref = keyring.audit_request_ref(request_id)

    assert len(ref.hmac_hex) == 64
    assert request_id not in repr(ref)
    assert (
        ref.hmac_hex
        != keyring.audit_target_ref(
            AuditTargetKind.RUN.value,
            uuid.uuid4(),
        ).hmac_hex
    )


def test_target_hmac_is_domain_separated_and_rotation_aware() -> None:
    keyring = _keyring()
    target_id = uuid.uuid4()

    refs = keyring.audit_target_refs(AuditTargetKind.RUN.value, target_id)

    assert tuple(ref.key_id for ref in refs) == ("audit-v2", "audit-v1")
    assert all(len(ref.hmac_hex) == 64 for ref in refs)
    assert str(target_id) not in repr(refs)
    assert refs[0].hmac_hex != keyring.job_owner_ref(str(target_id)).hmac_hex
    assert refs[0].hmac_hex != keyring.quota_source_ref(target_id.bytes).hmac_hex
