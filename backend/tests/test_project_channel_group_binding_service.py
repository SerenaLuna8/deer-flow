from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app.channel_group_bindings.agent_validation import (
    ProjectGroupBindingAgentValidator,
)
from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingForbidden,
    GroupBindingNotFound,
    GroupBindingUnavailable,
)
from app.channel_group_bindings.identity import AuditChannelGroupIdentityHasher
from app.channel_group_bindings.models import (
    CreateGroupBindingChallenge,
    UpdateGroupBinding,
)
from app.channel_group_bindings.service import ProjectChannelGroupBindingService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid
from app.shared_assets.errors import (
    AssetNotFound,
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
AGENT_ID = uuid.UUID("30000000-0000-4000-8000-000000000001")
BINDING_ID = uuid.UUID("40000000-0000-4000-8000-000000000001")


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("50000000-0000-4000-8000-000000000001"),
        project_id=PROJECT_ID,
        membership_id=uuid.UUID("60000000-0000-4000-8000-000000000001"),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=7,
        request_id="req-group-binding",
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


class _IdentityHasher:
    def group_ref(self, provider: str, instance_id: uuid.UUID, external_chat_id: str) -> str:
        assert provider == "feishu"
        assert instance_id == INSTANCE_ID
        assert external_chat_id == "oc_raw_secret_chat"
        return "a" * 64

    def account_ref(self, provider: str, instance_id: uuid.UUID, external_account_id: str) -> str:
        assert provider == "feishu"
        assert instance_id == INSTANCE_ID
        assert external_account_id == "ou_raw_secret_sender"
        return "b" * 64


def _binding(**overrides):
    values = {
        "id": BINDING_ID,
        "project_id": PROJECT_ID,
        "channel_instance_id": INSTANCE_ID,
        "provider": "feishu",
        "external_group_ref": "a" * 64,
        "external_group_name": "研发群",
        "agent_asset_id": AGENT_ID,
        "agent_scope": "system",
        "status": "active",
        "revision": 1,
        "last_activity_at": None,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_external_group_and_sender_refs_are_domain_and_instance_separated() -> None:
    hasher = AuditChannelGroupIdentityHasher(
        AuditHmacKeyring(
            active_key_id="audit-v1",
            _keys={"audit-v1": b"g" * 32},
        )
    )
    other_instance = uuid.UUID("20000000-0000-4000-8000-000000000002")

    group_ref = hasher.group_ref("feishu", INSTANCE_ID, "same-external-id")
    sender_ref = hasher.account_ref("feishu", INSTANCE_ID, "same-external-id")
    other_instance_ref = hasher.group_ref(
        "feishu",
        other_instance,
        "same-external-id",
    )

    assert len(group_ref) == len(sender_ref) == len(other_instance_ref) == 64
    assert len({group_ref, sender_ref, other_instance_ref}) == 3
    assert "same-external-id" not in group_ref


@pytest.mark.asyncio
async def test_retained_hmac_refs_are_forwarded_for_rotation_safe_guest_lookup() -> None:
    old_keyring = AuditHmacKeyring(
        active_key_id="audit-old",
        _keys={"audit-old": b"o" * 32},
    )
    rotated_keyring = AuditHmacKeyring(
        active_key_id="audit-new",
        _keys={"audit-new": b"n" * 32, "audit-old": b"o" * 32},
    )
    old_hasher = AuditChannelGroupIdentityHasher(old_keyring)
    rotated_hasher = AuditChannelGroupIdentityHasher(rotated_keyring)
    old_group_ref = old_hasher.group_ref("feishu", INSTANCE_ID, "oc-rotation")
    old_account_ref = old_hasher.account_ref("feishu", INSTANCE_ID, "ou-rotation")
    guest_id = uuid.uuid4()
    repository = SimpleNamespace(
        resolve_or_create_guest=AsyncMock(
            return_value={
                "id": uuid.uuid4().hex,
                "account_id": str(guest_id),
                "project_id": str(PROJECT_ID),
                "owner_user_id": str(guest_id),
                "membership_version": 1,
                "provider": "feishu",
                "status": "connected",
                "channel_instance_id": str(INSTANCE_ID),
                "external_account_id": old_account_ref,
                "workspace_id": old_group_ref,
                "metadata": {
                    "group_binding_id": str(BINDING_ID),
                    "agent_asset_id": str(AGENT_ID),
                    "agent_scope": "system",
                },
            }
        )
    )
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        identity_hasher=rotated_hasher,
        clock=lambda: NOW,
    )

    authority = await service.resolve_or_create_guest(
        provider="feishu",
        channel_instance_id=INSTANCE_ID,
        chat_id="oc-rotation",
        sender_id="ou-rotation",
        topic_id="om-rotation",
    )

    call = repository.resolve_or_create_guest.await_args
    assert call.kwargs["external_group_refs"][0] != old_group_ref
    assert old_group_ref in call.kwargs["external_group_refs"]
    assert call.kwargs["external_account_refs"][0] != old_account_ref
    assert old_account_ref in call.kwargs["external_account_refs"]
    assert authority["resolved_conversation_id"] == old_group_ref
    assert authority["resolved_topic_id"] == old_hasher.topic_ref(
        "feishu",
        INSTANCE_ID,
        "om-rotation",
    )

    alias_refs = service.pseudonymize_topic_aliases(
        provider="feishu",
        channel_instance_id=INSTANCE_ID,
        chat_id="oc-rotation",
        resolved_conversation_id=old_group_ref,
        topic_ids=("om-parent", "om-card"),
    )
    assert alias_refs == (
        old_hasher.topic_ref("feishu", INSTANCE_ID, "om-parent"),
        old_hasher.topic_ref("feishu", INSTANCE_ID, "om-card"),
    )


@pytest.mark.asyncio
async def test_admin_creates_one_time_challenge_bound_to_instance_agent_and_membership() -> None:
    context = _context()
    instance = SimpleNamespace(
        id=INSTANCE_ID,
        project_id=PROJECT_ID,
        provider="feishu",
        desired_status="enabled",
        observed_status="running",
        deleted_at=None,
    )
    repository = SimpleNamespace(
        lock_project_context=AsyncMock(),
        get_runtime_instance=AsyncMock(return_value=instance),
        create_challenge=AsyncMock(),
    )
    agent_validator = SimpleNamespace(validate=AsyncMock())
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        agent_validator=agent_validator,
        identity_hasher=_IdentityHasher(),
        clock=lambda: NOW,
        code_factory=lambda: "bind-code-do-not-store-raw",
        challenge_ttl_seconds=600,
    )

    result = await service.create_challenge(
        context,
        CreateGroupBindingChallenge(
            provider="feishu",
            agent_asset_id=AGENT_ID,
            agent_scope="system",
        ),
    )

    repository.lock_project_context.assert_awaited_once_with(ANY, context, read=False)
    repository.get_runtime_instance.assert_awaited_once_with(
        ANY,
        project_id=PROJECT_ID,
        provider="feishu",
        for_update=True,
    )
    agent_validator.validate.assert_awaited_once()
    create_call = repository.create_challenge.await_args
    assert isinstance(create_call.args[0], _Session)
    assert create_call.kwargs["project_id"] == PROJECT_ID
    assert create_call.kwargs["channel_instance_id"] == INSTANCE_ID
    assert create_call.kwargs["provider"] == "feishu"
    assert create_call.kwargs["agent_asset_id"] == AGENT_ID
    assert create_call.kwargs["agent_scope"] == "system"
    assert create_call.kwargs["membership_id"] == context.membership_id
    assert create_call.kwargs["membership_version"] == 7
    assert create_call.kwargs["created_by_user_id"] == str(context.user_id)
    assert create_call.kwargs["expires_at"] == NOW + timedelta(minutes=10)
    assert len(create_call.kwargs["code_digest"]) == 64
    assert "bind-code-do-not-store-raw" not in repr(create_call)
    assert result.provider == "feishu"
    assert result.code == "bind-code-do-not-store-raw"
    assert result.command == "/bind-project bind-code-do-not-store-raw"
    assert result.expires_in == 600


@pytest.mark.asyncio
async def test_non_admin_cannot_create_update_or_delete_group_binding() -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("forbidden request must not open storage")

    service = ProjectChannelGroupBindingService(
        _ExplodingFactory(),
        identity_hasher=_IdentityHasher(),
    )
    context = _context(ProjectRole.EDITOR)

    with pytest.raises(GroupBindingForbidden):
        await service.list(context)
    with pytest.raises(GroupBindingForbidden):
        await service.create_challenge(
            context,
            CreateGroupBindingChallenge("feishu", AGENT_ID, "system"),
        )
    with pytest.raises(GroupBindingForbidden):
        await service.update(
            context,
            BINDING_ID,
            UpdateGroupBinding(expected_revision=1, enabled=False),
        )
    with pytest.raises(GroupBindingForbidden):
        await service.delete(context, BINDING_ID, expected_revision=1)


@pytest.mark.asyncio
async def test_challenge_code_must_have_at_least_16_random_characters() -> None:
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=SimpleNamespace(),
        identity_hasher=_IdentityHasher(),
        code_factory=lambda: "short-code",
    )

    with pytest.raises(GroupBindingUnavailable):
        await service.create_challenge(
            _context(),
            CreateGroupBindingChallenge("feishu", AGENT_ID, "system"),
        )


@pytest.mark.asyncio
async def test_complete_challenge_persists_only_hmac_group_ref_and_never_raw_ids() -> None:
    repository = SimpleNamespace(
        complete_challenge=AsyncMock(return_value=_binding()),
    )
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        identity_hasher=_IdentityHasher(),
        clock=lambda: NOW,
    )

    result = await service.complete_challenge(
        provider="feishu",
        channel_instance_id=INSTANCE_ID,
        code="one-time-code-1234",
        chat_id="oc_raw_secret_chat",
        sender_id="ou_raw_secret_sender",
        display_name="研发群",
    )

    call = repository.complete_challenge.await_args
    assert isinstance(call.args[0], _Session)
    assert call.kwargs["code_digest"] != "one-time-code-1234"
    assert call.kwargs["external_group_ref"] == "a" * 64
    assert call.kwargs["external_group_refs"] == ("a" * 64,)
    assert call.kwargs["display_name"] == "研发群"
    assert "oc_raw_secret_chat" not in repr(call)
    assert "ou_raw_secret_sender" not in repr(call)
    assert result.id == BINDING_ID
    assert not hasattr(result, "channel_instance_id")
    assert not hasattr(result, "external_group_ref")


@pytest.mark.asyncio
async def test_expired_consumed_or_wrong_instance_challenge_is_public_not_found() -> None:
    repository = SimpleNamespace(complete_challenge=AsyncMock(return_value=None))
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        identity_hasher=_IdentityHasher(),
        clock=lambda: NOW,
    )

    with pytest.raises(GroupBindingNotFound):
        await service.complete_challenge(
            provider="feishu",
            channel_instance_id=INSTANCE_ID,
            code="expired-or-consumed",
            chat_id="oc_raw_secret_chat",
            sender_id="ou_raw_secret_sender",
        )


@pytest.mark.asyncio
async def test_resolve_or_create_guest_uses_separate_group_and_sender_hmac_refs() -> None:
    guest_user_id = uuid.uuid4()
    authority = {
        "id": uuid.uuid4().hex,
        "account_id": str(guest_user_id),
        "project_id": str(PROJECT_ID),
        "owner_user_id": str(guest_user_id),
        "membership_version": 1,
        "provider": "feishu",
        "status": "connected",
        "channel_instance_id": str(INSTANCE_ID),
        "external_account_id": "b" * 64,
        "workspace_id": "a" * 64,
        "metadata": {
            "group_binding_id": str(BINDING_ID),
            "agent_asset_id": str(AGENT_ID),
            "agent_scope": "system",
        },
    }
    repository = SimpleNamespace(resolve_or_create_guest=AsyncMock(return_value=authority))
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        identity_hasher=_IdentityHasher(),
        clock=lambda: NOW,
    )

    resolved = await service.resolve_or_create_guest(
        provider="feishu",
        channel_instance_id=INSTANCE_ID,
        chat_id="oc_raw_secret_chat",
        sender_id="ou_raw_secret_sender",
    )

    call = repository.resolve_or_create_guest.await_args
    assert call.kwargs["external_group_refs"] == ("a" * 64,)
    assert call.kwargs["external_account_refs"] == ("b" * 64,)
    assert "oc_raw_secret_chat" not in repr(call)
    assert "ou_raw_secret_sender" not in repr(call)
    assert {key: resolved[key] for key in authority} == authority
    assert resolved["resolved_conversation_id"] == "a" * 64
    assert resolved["resolved_topic_id"] is None
    assert resolved["status"] == "connected"
    assert resolved["channel_instance_id"] == str(INSTANCE_ID)
    assert resolved["metadata"] == {
        "group_binding_id": str(BINDING_ID),
        "agent_asset_id": str(AGENT_ID),
        "agent_scope": "system",
    }
    assert "oc_raw_secret_chat" not in repr(resolved)
    assert "ou_raw_secret_sender" not in repr(resolved)


@pytest.mark.asyncio
async def test_resolve_reports_selected_agent_that_is_no_longer_available() -> None:
    from app.channel_group_bindings.repository import (
        GroupBindingRepositoryAgentUnavailable,
    )

    repository = SimpleNamespace(resolve_or_create_guest=AsyncMock(side_effect=GroupBindingRepositoryAgentUnavailable))
    service = ProjectChannelGroupBindingService(
        _Session,
        repository=repository,
        identity_hasher=_IdentityHasher(),
        clock=lambda: NOW,
    )

    with pytest.raises(GroupBindingAgentUnavailable) as caught:
        await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=INSTANCE_ID,
            chat_id="oc_raw_secret_chat",
            sender_id="ou_raw_secret_sender",
        )

    assert caught.value.code == "GROUP_BINDING_AGENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_agent_validator_distinguishes_unavailable_missing_and_storage_failure() -> None:
    validator = ProjectGroupBindingAgentValidator(_Session)
    resolver = AsyncMock()
    validator._resolver.resolve_project_asset_snapshot_in_session = resolver

    resolver.side_effect = AssetResolutionUnavailable("asset-request")
    with pytest.raises(GroupBindingAgentUnavailable):
        await validator.validate(_Session(), _context(), AGENT_ID, "system")

    resolver.side_effect = AssetNotFound("asset-request")
    with pytest.raises(GroupBindingNotFound):
        await validator.validate(_Session(), _context(), AGENT_ID, "system")

    resolver.side_effect = AssetStorageUnavailable("asset-request")
    with pytest.raises(GroupBindingUnavailable):
        await validator.validate(_Session(), _context(), AGENT_ID, "system")


@pytest.mark.asyncio
async def test_resolve_maps_identity_keyring_failure_to_unavailable() -> None:
    class _UnavailableIdentityHasher:
        def group_refs(self, *args):
            raise AuditHmacKeyringInvalid

    service = ProjectChannelGroupBindingService(
        _Session,
        repository=SimpleNamespace(resolve_or_create_guest=AsyncMock()),
        identity_hasher=_UnavailableIdentityHasher(),
        clock=lambda: NOW,
    )

    with pytest.raises(GroupBindingUnavailable):
        await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=INSTANCE_ID,
            chat_id="oc_raw_secret_chat",
            sender_id="ou_raw_secret_sender",
        )


@pytest.mark.asyncio
async def test_repository_revalidates_agent_before_creating_guest_identity() -> None:
    from app.channel_group_bindings.repository import (
        GroupBindingRepositoryAgentUnavailable,
        PostgresProjectChannelGroupBindingRepository,
    )

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    binding = _binding(first_activity_at=None)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=(
                _Result(PROJECT_ID),
                _Result(PROJECT_ID),
                _Result(INSTANCE_ID),
                _Result(binding),
                _Result(None),
            )
        )
    )

    with pytest.raises(GroupBindingRepositoryAgentUnavailable):
        await PostgresProjectChannelGroupBindingRepository().resolve_or_create_guest(
            session,
            provider="feishu",
            channel_instance_id=INSTANCE_ID,
            external_group_refs=("a" * 64,),
            external_account_refs=("b" * 64,),
            now=NOW,
        )

    assert session.execute.await_count == 5
