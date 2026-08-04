from __future__ import annotations

import json
import uuid
from base64 import b64encode
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.project_channels.credential_store import ProjectChannelCredentialStore
from app.project_channels.errors import ChannelInstanceConflict
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-channel-credential",
    )


def _keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "channel-key")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        json.dumps({"channel-key": b64encode(b"c" * 32).decode("ascii")}),
    )


@pytest.mark.asyncio
async def test_create_encrypts_channel_secret_and_returns_exact_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyring(monkeypatch)
    context = _context()
    repository = SimpleNamespace(
        create_project_credential=AsyncMock(),
        add_version=AsyncMock(),
        session=SimpleNamespace(flush=AsyncMock()),
    )

    async def assign_id(_context, row):
        row.id = uuid.uuid4()
        return row

    repository.create_project_credential.side_effect = assign_id
    store = ProjectChannelCredentialStore(repository)
    secret = "never-store-in-plaintext"
    result = await store.create(
        context,
        instance_id=uuid.uuid4(),
        provider="feishu",
        display_name="Feishu",
        payload={"env": {"FEISHU_APP_SECRET": secret}},
    )

    credential = repository.create_project_credential.await_args.args[1]
    version, envelope = repository.add_version.await_args.args[1:3]
    assert credential.credential_type == "channel.feishu"
    assert credential.current_version_id == result.credential_version_id
    assert result.credential_id == credential.id
    assert version.payload_schema == {"env": ["FEISHU_APP_SECRET"]}
    assert secret.encode() not in envelope.ciphertext
    assert secret not in repr(result)
    assert secret not in repr(envelope)


@pytest.mark.asyncio
async def test_rotate_retires_previous_exact_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyring(monkeypatch)
    context = _context()
    credential_id = uuid.uuid4()
    previous_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        credential_type="channel.feishu",
        status="active",
        current_version_id=previous_id,
        version=3,
    )
    previous = SimpleNamespace(id=previous_id, status="active", retired_at=None)
    repository = SimpleNamespace(
        get_project_credential=AsyncMock(return_value=credential),
        lock_current_version=AsyncMock(return_value=previous),
        next_version_number=AsyncMock(return_value=2),
        add_version=AsyncMock(),
        session=SimpleNamespace(flush=AsyncMock()),
    )
    result = await ProjectChannelCredentialStore(repository).rotate(
        context,
        credential_id=credential_id,
        provider="feishu",
        payload={"env": {"FEISHU_APP_SECRET": "replacement-secret"}},
    )

    assert previous.status == "retired"
    assert previous.retired_at is not None
    assert credential.current_version_id == result.credential_version_id
    assert credential.version == 4
    version = repository.add_version.await_args.args[1]
    assert version.supersedes_version_id == previous_id


@pytest.mark.asyncio
async def test_rotate_rejects_wrong_channel_credential_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyring(monkeypatch)
    context = _context()
    repository = SimpleNamespace(
        get_project_credential=AsyncMock(
            return_value=SimpleNamespace(
                id=uuid.uuid4(),
                credential_type="token",
                status="active",
            )
        )
    )
    with pytest.raises(ChannelInstanceConflict):
        await ProjectChannelCredentialStore(repository).rotate(
            context,
            credential_id=uuid.uuid4(),
            provider="feishu",
            payload={"env": {"FEISHU_APP_SECRET": "secret"}},
        )


@pytest.mark.asyncio
async def test_revoke_advances_credential_revision_once_and_revokes_all_versions() -> None:
    context = _context()
    credential_id = uuid.uuid4()
    credential = SimpleNamespace(
        id=credential_id,
        credential_type="channel.feishu",
        status="active",
        is_delete=False,
        version=5,
        revoked_at=None,
        revoked_by_user_id=None,
    )
    versions = (
        SimpleNamespace(status="retired", revoked_at=None, revoked_by_user_id=None),
        SimpleNamespace(status="active", revoked_at=None, revoked_by_user_id=None),
    )

    async def mark_deleted(row, *, request_id):
        assert request_id == context.request_id
        row.is_delete = True
        row.version += 1
        return row

    repository = SimpleNamespace(
        get_project_credential=AsyncMock(return_value=credential),
        lock_all_versions=AsyncMock(return_value=versions),
        mark_deleted=AsyncMock(side_effect=mark_deleted),
        session=SimpleNamespace(flush=AsyncMock()),
    )

    await ProjectChannelCredentialStore(repository).revoke(
        context,
        credential_id=credential_id,
        provider="feishu",
    )

    assert credential.status == "revoked"
    assert credential.is_delete is True
    assert credential.version == 6
    assert all(version.status == "revoked" for version in versions)
    assert all(version.revoked_at is not None for version in versions)
    assert all(version.revoked_by_user_id == str(context.user_id) for version in versions)
