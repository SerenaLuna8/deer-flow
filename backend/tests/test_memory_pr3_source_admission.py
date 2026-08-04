from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from app.private_work.memory_source_admission import prepare_memory_source
from app.reliability.jobs import memory_extract_idempotency_key


@dataclass(frozen=True, slots=True)
class _HmacRef:
    key_id: str
    hmac_hex: str


def _source_hmac(payload: bytes) -> _HmacRef:
    return _HmacRef(
        key_id="memory-test-v1",
        hmac_hex=hmac.new(b"m" * 32, payload, hashlib.sha256).hexdigest(),
    )


def _prepare(raw_input: object):
    return prepare_memory_source(
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        owner_user_id="00000000-0000-0000-0000-000000000002",
        namespace="default",
        run_id="run-1",
        source_attempt_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        raw_input=raw_input,
        source_hmac=_source_hmac,
    )


def test_prepare_memory_source_keeps_only_ordered_visible_user_text() -> None:
    prepared = _prepare(
        {
            "messages": [
                {"role": "system", "content": "framework"},
                {
                    "type": "human",
                    "id": "user-message-1",
                    "content": "<uploaded_files>private upload metadata</uploaded_files>\n记住我喜欢中文。",
                },
                {"role": "assistant", "content": "assistant claim"},
                {"role": "tool", "content": "tool output"},
                {
                    "role": "user",
                    "content": "hidden reminder",
                    "additional_kwargs": {"hide_from_ui": True},
                },
                {"role": "user", "name": "summary", "content": "internal summary"},
                {
                    "role": "user",
                    "content": "clarification transport",
                    "additional_kwargs": {"human_input_response": {"value": "yes"}},
                },
                {
                    "role": "user",
                    "type": "human",
                    "content": [
                        {"type": "text", "text": "我的编辑器是 "},
                        {"type": "text", "text": "VS Code。"},
                        {
                            "type": "image_url",
                            "text": "图片中伪装的文本",
                            "image_url": "https://example.invalid/private.png",
                        },
                    ],
                },
                {"role": "user", "type": "tool", "content": "conflicting role"},
            ]
        }
    )

    assert prepared is not None
    assert prepared.hmac_key_version == "memory-test-v1"
    assert [item.ordinal for item in prepared.items] == [0, 1]
    assert [item.content for item in prepared.items] == [
        "记住我喜欢中文。",
        "我的编辑器是 VS Code。",
    ]
    assert prepared.items[0].source_message_id == "user-message-1"
    assert prepared.items[1].source_message_id.startswith("generated:")
    assert all(len(item.content_hmac) == 64 for item in prepared.items)
    assert len(prepared.source_identity_digest) == 64


def test_prepare_memory_source_rejects_obvious_secrets_and_upload_only_messages() -> None:
    prepared = _prepare(
        [
            {"role": "user", "content": "<uploaded_files>only metadata</uploaded_files>"},
            {"role": "user", "content": "password = hunter22"},
            {"role": "user", "content": "Authorization: Bearer abcdefghijklmnop"},
            {"role": "user", "content": "Authorization: Basic dXNlcjpwYXNzd29yZA=="},
            {"role": "user", "content": "api_key: sk-proj-abcdefghijklmnop"},
            {"role": "user", "content": "密钥：abcdefghijklmnop"},
            {"role": "user", "content": "-----BEGIN PRIVATE KEY-----"},
            {"role": "user", "content": "请记住：我偏好简洁回答。"},
        ]
    )

    assert prepared is not None
    assert [item.content for item in prepared.items] == ["请记住：我偏好简洁回答。"]


def test_prepare_memory_source_does_not_reject_normal_security_discussion() -> None:
    prepared = _prepare(
        {
            "messages": [
                {"role": "user", "content": "请解释 token 是什么。"},
                {"role": "user", "content": "我使用 password manager。"},
                {
                    "role": "user",
                    "content": "我正在讨论文字 <uploaded_files>，这不是上传包装。",
                },
            ]
        }
    )

    assert prepared is not None
    assert [item.content for item in prepared.items] == [
        "请解释 token 是什么。",
        "我使用 password manager。",
        "我正在讨论文字 <uploaded_files>，这不是上传包装。",
    ]


def test_prepare_memory_source_is_stable_and_uses_attempt_in_batch_identity() -> None:
    raw_input = {
        "messages": [
            {"role": "user", "id": "same-id", "content": "第一条"},
            {"role": "user", "id": "same-id", "content": "第二条"},
        ]
    }
    first = _prepare(raw_input)
    replay = _prepare(raw_input)

    assert first == replay
    assert first is not None
    assert first.items[0].source_message_id == "same-id"
    assert first.items[1].source_message_id.startswith("generated:")
    assert len({item.source_message_id for item in first.items}) == 2

    other_attempt = prepare_memory_source(
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        owner_user_id="00000000-0000-0000-0000-000000000002",
        namespace="default",
        run_id="run-1",
        source_attempt_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        raw_input=raw_input,
        source_hmac=_source_hmac,
    )
    assert other_attempt is not None
    assert other_attempt.source_identity_digest != first.source_identity_digest


def test_prepare_memory_source_returns_none_without_eligible_content() -> None:
    assert _prepare(None) is None
    assert _prepare({"messages": [{"role": "assistant", "content": "not user-authored"}]}) is None
    assert _prepare({"messages": [{"role": "user", "content": "x" * 64_001}]}) is None


def test_memory_extract_job_identity_is_stable_and_contract_specific() -> None:
    batch_id = uuid.UUID("00000000-0000-0000-0000-000000000010")

    first = memory_extract_idempotency_key(batch_id, "a" * 64)

    assert first == memory_extract_idempotency_key(batch_id, "a" * 64)
    assert len(first) == 64
    assert first != memory_extract_idempotency_key(batch_id, "b" * 64)
