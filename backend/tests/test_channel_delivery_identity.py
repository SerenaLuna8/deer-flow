"""Provider delivery identity extraction contracts."""

from __future__ import annotations

import pytest

from app.channels.message_bus import InboundMessage, extract_provider_delivery_id


def _message(metadata: dict) -> InboundMessage:
    return InboundMessage(
        channel_name="slack",
        chat_id="conversation-a",
        user_id="external-a",
        text="hello",
        metadata=metadata,
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"event_id": "event-a"}, "event-a"),
        ({"message_id": 123}, "123"),
        ({"msg_id": "message-a"}, "message-a"),
        ({"raw_message": {"message_id": "raw-a"}}, "raw-a"),
        ({"github": {"delivery_id": "github-a"}}, "github-a"),
    ],
)
def test_extract_provider_delivery_id_uses_stable_provider_fields(
    metadata: dict,
    expected: str,
) -> None:
    assert extract_provider_delivery_id(_message(metadata)) == expected


def test_extract_provider_delivery_id_ignores_empty_values() -> None:
    assert (
        extract_provider_delivery_id(
            _message(
                {
                    "event_id": " ",
                    "raw_message": {"message_id": None},
                    "github": {"delivery_id": ""},
                }
            )
        )
        is None
    )


def test_explicit_provider_delivery_id_has_priority_over_legacy_metadata() -> None:
    message = _message({"message_id": "legacy-a"})
    message.provider_delivery_id = "canonical-a"

    assert extract_provider_delivery_id(message) == "canonical-a"
