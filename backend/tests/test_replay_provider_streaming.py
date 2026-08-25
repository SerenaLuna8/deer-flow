from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
from replay_provider import ReplayChatModel, hash_replay_input


def test_replay_turn_can_derive_deterministic_chunks_without_changing_recorded_bytes(tmp_path) -> None:
    input_messages = [HumanMessage(content="hello")]
    recorded = AIMessage(
        content="hi from replay.",
        id="recorded-message",
        usage_metadata={
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
        },
    )
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "scenario": "derived-stream",
                "turns": [
                    {
                        "caller": "lead_agent",
                        "input_hash": hash_replay_input(input_messages, caller="lead_agent"),
                        "stream": {
                            "provenance": "derived_from_recorded_output",
                            "text_chunk_chars": 1,
                        },
                        "output": message_to_dict(recorded),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    chunks = list(ReplayChatModel(fixture=str(fixture))._stream(input_messages))

    assert [chunk.message.content for chunk in chunks] == list(recorded.content)
    assert "".join(str(chunk.message.content) for chunk in chunks) == recorded.content
    assert {chunk.message.id for chunk in chunks} == {recorded.id}
    assert all(chunk.message.usage_metadata is None for chunk in chunks[:-1])
    assert chunks[-1].message.usage_metadata == recorded.usage_metadata
