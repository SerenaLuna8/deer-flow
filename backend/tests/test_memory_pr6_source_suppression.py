from __future__ import annotations

import uuid

from app.private_work.memory_source_admission import prepare_memory_source
from app.reliability.owner_refs import AuditHmacKeyring


def test_memory_source_preparation_keeps_retained_key_refs_for_suppression() -> None:
    old_keyring = AuditHmacKeyring(
        active_key_id="memory-k1",
        _keys={"memory-k1": b"1" * 32},
    )
    rotated_keyring = AuditHmacKeyring(
        active_key_id="memory-k2",
        _keys={"memory-k1": b"1" * 32, "memory-k2": b"2" * 32},
    )
    arguments = {
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "owner_user_id": "00000000-0000-0000-0000-000000000002",
        "namespace": "default",
        "run_id": "run-1",
        "source_attempt_id": uuid.UUID(
            "00000000-0000-0000-0000-000000000003",
        ),
        "raw_input": {
            "messages": [
                {
                    "role": "user",
                    "id": "message-1",
                    "content": "用户偏好简洁回答。",
                }
            ]
        },
    }

    before_rotation = prepare_memory_source(
        **arguments,
        source_hmac=old_keyring.memory_source_ref,
        source_hmac_refs=old_keyring.memory_source_refs,
    )
    after_rotation = prepare_memory_source(
        **arguments,
        source_hmac=rotated_keyring.memory_source_ref,
        source_hmac_refs=rotated_keyring.memory_source_refs,
    )

    assert before_rotation is not None
    assert after_rotation is not None
    assert after_rotation.hmac_key_version == "memory-k2"
    assert [key_id for key_id, _digest in after_rotation.items[0].suppression_refs] == [
        "memory-k2",
        "memory-k1",
    ]
    old_digest = before_rotation.items[0].suppression_refs[0][1]
    assert ("memory-k1", old_digest) in after_rotation.items[0].suppression_refs
