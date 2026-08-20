from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.audit.models import (
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditAuthorityRejected,
)
from app.audit.service import AuditService
from app.shared_assets.audit import (
    _ACTIONS,
    DurableSharedAssetGovernanceEventSink,
)

_AGENT_ACTIONS = (
    ("agent.create", AuditAction.ASSET_CREATED, None),
    ("agent.version.create", AuditAction.ASSET_UPDATED, 7),
    ("agent.instructions.update", AuditAction.ASSET_UPDATED, 7),
    ("agent.capability_bindings.update", AuditAction.ASSET_UPDATED, 7),
    ("agent.version.activate", AuditAction.ASSET_UPDATED, 7),
    ("agent.delete", AuditAction.ASSET_DELETED, None),
    ("agent.enable", AuditAction.ASSET_UPDATED, None),
    ("agent.suspend", AuditAction.ASSET_DEPRECATED, None),
    ("agent.default.set", AuditAction.ASSET_BOUND, None),
    ("agent.default.clear", AuditAction.ASSET_UNBOUND, None),
)


class _Session:
    def __init__(
        self,
        *,
        membership_id: uuid.UUID | None = None,
        version_number: int | None = 7,
    ) -> None:
        self.membership_id = membership_id or uuid.uuid4()
        self.version_number = version_number
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        rendered = str(statement)
        if "users" in rendered:
            return "system_admin"
        if "project_memberships" in rendered:
            return self.membership_id
        if "agent_versions" in rendered or "skill_versions" in rendered:
            return self.version_number
        raise AssertionError(f"unexpected audit authority query: {rendered}")


def _sink() -> tuple[DurableSharedAssetGovernanceEventSink, AsyncMock]:
    service = object.__new__(AuditService)
    append = AsyncMock()
    service.append = append  # type: ignore[method-assign]
    return DurableSharedAssetGovernanceEventSink(service), append


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "audit_action", "version_number"), _AGENT_ACTIONS)
async def test_agent_governance_keeps_exact_operation_and_safe_version_number(
    operation: str,
    audit_action: AuditAction,
    version_number: int | None,
) -> None:
    sink, append = _sink()
    session = _Session()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4() if version_number is not None else None

    await sink.append_project(
        session,  # type: ignore[arg-type]
        actor=actor_id,
        project_id=project_id,
        asset_id=asset_id,
        version_id=version_id,
        action=operation,
        request_id="req-agent-audit",
        asset_kind="agent",
    )

    assert append.await_args.args[2] is audit_action
    metadata = append.await_args.args[5]
    expected = {
        "asset_kind": "agent",
        "operation": operation,
    }
    if version_number is not None:
        expected["version_number"] = version_number
    assert metadata == expected
    assert "version_id" not in metadata
    assert version_id is None or str(version_id) not in repr(metadata)

    rendered = tuple(str(statement) for statement in session.statements)
    version_queries = tuple(statement for statement in rendered if "agent_versions" in statement)
    if version_id is None:
        assert version_queries == ()
    else:
        assert len(version_queries) == 1
        assert "agent_versions.agent_id" in version_queries[0]
        assert "agent_versions.id" in version_queries[0]


def test_asset_audit_metadata_accepts_only_safe_agent_version_coordinates() -> None:
    metadata_model = AUDIT_METADATA_MODELS[AuditAction.ASSET_UPDATED]

    parsed = metadata_model.model_validate(
        {
            "asset_kind": "agent",
            "operation": "agent.version.activate",
            "version_number": 7,
        }
    )

    assert parsed.model_dump(mode="json", exclude_none=True) == {
        "asset_kind": "agent",
        "operation": "agent.version.activate",
        "version_number": 7,
    }
    with pytest.raises(ValueError):
        metadata_model.model_validate(
            {
                "asset_kind": "agent",
                "operation": "agent.version.activate",
                "version_number": 7,
                "version_id": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValueError):
        metadata_model.model_validate({"asset_kind": "agent"})
    with pytest.raises(ValueError):
        metadata_model.model_validate(
            {
                "asset_kind": "agent",
                "operation": "agent.unknown",
            }
        )
    with pytest.raises(ValueError):
        metadata_model.model_validate(
            {
                "asset_kind": "agent",
                "operation": "agent.version.activate",
            }
        )
    with pytest.raises(ValueError):
        metadata_model.model_validate(
            {
                "asset_kind": "agent",
                "operation": "agent.create",
                "version_number": 7,
            }
        )


@pytest.mark.parametrize("operation", tuple(_ACTIONS))
def test_every_governance_operation_has_a_valid_closed_audit_encoding(
    operation: str,
) -> None:
    operation_domain = operation.partition(".")[0]
    asset_kind = operation_domain if operation_domain in {"agent", "skill", "mcp"} else "mcp" if operation_domain == "credential" else "agent"
    metadata: dict[str, object] = {
        "asset_kind": asset_kind,
        "operation": operation,
    }
    if operation in {
        "agent.version.create",
        "agent.instructions.update",
        "agent.capability_bindings.update",
        "agent.version.activate",
        "skill.version.create",
        "skill.version.activate",
        "skill.export",
        "skill.version.revoke",
    }:
        metadata["version_number"] = 7

    parsed = AUDIT_METADATA_MODELS[_ACTIONS[operation]].model_validate(metadata)

    assert parsed.model_dump(mode="json", exclude_none=True) == metadata


@pytest.mark.asyncio
async def test_system_override_audit_uses_the_same_safe_agent_coordinates() -> None:
    sink, append = _sink()
    session = _Session()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    await sink.append_override(
        session,  # type: ignore[arg-type]
        actor=actor_id,
        project_id=project_id,
        asset_id=asset_id,
        version_id=version_id,
        action="agent.version.activate",
        request_id="req-agent-override-audit",
        asset_kind="agent",
    )

    assert append.await_args.args[2] is AuditAction.ASSET_UPDATED
    assert append.await_args.args[5] == {
        "asset_kind": "agent",
        "operation": "agent.version.activate",
        "version_number": 7,
    }
    assert str(version_id) not in repr(append.await_args.args[5])


@pytest.mark.asyncio
async def test_system_skill_revocation_audit_keeps_exact_safe_version_number() -> None:
    sink, append = _sink()
    session = _Session(version_number=3)
    actor_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    await sink.append_override(
        session,  # type: ignore[arg-type]
        actor=actor_id,
        project_id=None,
        asset_id=asset_id,
        version_id=version_id,
        action="skill.version.revoke",
        request_id="req-skill-revocation-audit",
        asset_kind="skill",
    )

    assert append.await_args.args[2] is AuditAction.ASSET_DEPRECATED
    assert append.await_args.args[5] == {
        "asset_kind": "skill",
        "operation": "skill.version.revoke",
        "version_number": 3,
    }
    rendered = tuple(str(statement) for statement in session.statements)
    version_queries = tuple(statement for statement in rendered if "skill_versions" in statement)
    assert len(version_queries) == 1
    assert "skill_versions.skill_id" in version_queries[0]
    assert "skill_versions.id" in version_queries[0]
    assert str(version_id) not in repr(append.await_args.args[5])


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "system"])
async def test_skill_export_audit_uses_a_distinct_read_action_and_safe_version_number(
    scope: str,
) -> None:
    sink, append = _sink()
    session = _Session(version_number=9)
    actor_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    if scope == "project":
        await sink.append_project(
            session,  # type: ignore[arg-type]
            actor=actor_id,
            project_id=uuid.uuid4(),
            asset_id=asset_id,
            version_id=version_id,
            action="skill.export",
            request_id="req-skill-export-audit",
            asset_kind="skill",
        )
    else:
        await sink.append_override(
            session,  # type: ignore[arg-type]
            actor=actor_id,
            project_id=None,
            asset_id=asset_id,
            version_id=version_id,
            action="skill.export",
            request_id="req-skill-export-audit",
            asset_kind="skill",
        )

    assert append.await_args.args[2] is AuditAction.ASSET_EXPORTED
    assert append.await_args.args[5] == {
        "asset_kind": "skill",
        "operation": "skill.export",
        "version_number": 9,
    }
    assert str(version_id) not in repr(append.await_args.args[5])


@pytest.mark.asyncio
async def test_agent_version_audit_rejects_unknown_or_cross_asset_version() -> None:
    sink, append = _sink()
    session = _Session(version_number=None)

    with pytest.raises(AuditAuthorityRejected):
        await sink.append_project(
            session,  # type: ignore[arg-type]
            actor=uuid.uuid4(),
            project_id=uuid.uuid4(),
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            action="agent.version.activate",
            request_id="req-agent-audit-missing-version",
            asset_kind="agent",
        )

    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_operation_cannot_be_relabelled_as_another_asset_kind() -> None:
    sink, append = _sink()

    with pytest.raises(TypeError, match="shared asset audit event is invalid"):
        await sink.append_project(
            _Session(),  # type: ignore[arg-type]
            actor=uuid.uuid4(),
            project_id=uuid.uuid4(),
            asset_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            action="agent.version.activate",
            request_id="req-agent-audit-kind-mismatch",
            asset_kind="skill",
        )

    append.assert_not_awaited()
