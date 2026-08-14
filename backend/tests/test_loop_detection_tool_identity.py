"""Regression tests for content-sensitive loop detection tool identities."""

from deerflow.agents.middlewares.loop_detection_middleware import _hash_tool_calls


def _candidate_upsert(*, content: str, checksum: str | None) -> list[dict]:
    return [
        {
            "name": "upsert_candidate_file",
            "args": {
                "path": "SKILL.md",
                "media_type": "text/markdown",
                "content": content,
                "mode": "replace" if checksum is None else "append",
                "expected_draft_checksum": checksum,
                "expected_file_size_bytes": 0 if checksum is None else 6,
                "expected_file_sha256": checksum,
            },
        }
    ]


def test_candidate_file_chunks_have_distinct_loop_detection_identities() -> None:
    first = _candidate_upsert(content="first\n", checksum=None)
    second = _candidate_upsert(content="second\n", checksum="a" * 64)

    assert _hash_tool_calls(first) != _hash_tool_calls(second)


def test_identical_candidate_file_upserts_keep_stable_identity() -> None:
    call = _candidate_upsert(content="same\n", checksum=None)

    assert _hash_tool_calls(call) == _hash_tool_calls(call)
