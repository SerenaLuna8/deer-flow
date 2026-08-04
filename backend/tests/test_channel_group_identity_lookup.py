from __future__ import annotations

import uuid

from app.channel_group_bindings.identity import AuditChannelGroupIdentityHasher
from app.channels.service import _make_group_identity_candidates
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.channel_connections.sql import (
    ChannelConnectionRepository,
)


def test_connection_lookup_uses_raw_and_pseudonymous_identity_candidates() -> None:
    instance_id = uuid.uuid4()
    calls = []

    def candidate_factory(provider, channel_instance_id, account_id, workspace_id):
        calls.append(
            (provider, channel_instance_id, account_id, workspace_id),
        )
        return (("b" * 64, "a" * 64),)

    repository = ChannelConnectionRepository(
        lambda: None,  # type: ignore[arg-type]
        external_identity_candidates=candidate_factory,
    )

    assert repository._lookup_identity_candidates(
        "feishu",
        instance_id,
        "ou-raw-member",
        "oc-raw-group",
    ) == (
        ("ou-raw-member", "oc-raw-group"),
        ("b" * 64, "a" * 64),
    )
    assert calls == [
        ("feishu", instance_id, "ou-raw-member", "oc-raw-group"),
    ]


def test_legacy_connection_lookup_never_calls_group_identity_hasher() -> None:
    def exploding_factory(*_args):
        raise AssertionError("legacy lookup must not derive a group identity")

    repository = ChannelConnectionRepository(
        lambda: None,  # type: ignore[arg-type]
        external_identity_candidates=exploding_factory,
    )

    assert repository._lookup_identity_candidates(
        "feishu",
        None,
        "ou-personal",
        "oc-personal",
    ) == (("ou-personal", "oc-personal"),)


def test_channel_service_derives_separate_account_and_group_references() -> None:
    instance_id = uuid.uuid4()

    class _Hasher:
        def account_refs(self, provider, actual_instance_id, external_id):
            assert (provider, actual_instance_id, external_id) == (
                "feishu",
                instance_id,
                "ou-member",
            )
            return ("b" * 64,)

        def group_refs(self, provider, actual_instance_id, external_id):
            assert (provider, actual_instance_id, external_id) == (
                "feishu",
                instance_id,
                "oc-group",
            )
            return ("a" * 64,)

    candidates = _make_group_identity_candidates(_Hasher())

    assert candidates(
        "feishu",
        instance_id,
        "ou-member",
        "oc-group",
    ) == (("b" * 64, "a" * 64),)


def test_channel_identity_lookup_keeps_retained_rotation_keys_readable() -> None:
    instance_id = uuid.uuid4()
    keyring = AuditHmacKeyring(
        active_key_id="new",
        _keys={"old": b"o" * 32, "new": b"n" * 32},
    )
    hasher = AuditChannelGroupIdentityHasher(keyring)

    account_refs = hasher.account_refs("feishu", instance_id, "ou-member")
    group_refs = hasher.group_refs("feishu", instance_id, "oc-group")

    assert len(account_refs) == len(group_refs) == 2
    assert account_refs[0] == hasher.account_ref(
        "feishu",
        instance_id,
        "ou-member",
    )
    assert group_refs[0] == hasher.group_ref(
        "feishu",
        instance_id,
        "oc-group",
    )
    candidates = _make_group_identity_candidates(hasher)(
        "feishu",
        instance_id,
        "ou-member",
        "oc-group",
    )
    assert candidates == tuple((account_ref, group_ref) for account_ref in account_refs for group_ref in group_refs)
    assert (account_refs[0], group_refs[1]) in candidates
    assert (account_refs[1], group_refs[0]) in candidates
