from __future__ import annotations

import pytest

from deerflow.subagents.status_contract import (
    SUBAGENT_USAGE_COMPLETENESS_KEY,
    SUBAGENT_USAGE_RECEIPT_ID_KEY,
    SUBAGENT_USAGE_RECEIPT_STATE_KEY,
    make_subagent_additional_kwargs,
    read_subagent_usage_receipt,
    read_subagent_usage_receipt_state,
)


def test_subagent_usage_receipt_id_is_persisted_independently_from_tool_call_id() -> None:
    payload = make_subagent_additional_kwargs(
        "completed",
        result="done",
        token_usage={
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
        },
        usage_receipt_id="execution-receipt-1",
    )

    assert payload[SUBAGENT_USAGE_RECEIPT_ID_KEY] == "execution-receipt-1"


def test_subagent_usage_completeness_distinguishes_final_from_cutoff_usage() -> None:
    payload = make_subagent_additional_kwargs(
        "polling_timed_out",
        token_usage={
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
        },
        usage_completeness="latest_observed",
    )

    assert payload[SUBAGENT_USAGE_COMPLETENESS_KEY] == "latest_observed"


def test_subagent_usage_completeness_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="usage_completeness"):
        make_subagent_additional_kwargs(
            "completed",
            usage_completeness="unknown",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("receipt_id", ["", "   "])
def test_subagent_usage_receipt_id_rejects_blank_values(receipt_id: str) -> None:
    with pytest.raises(ValueError, match="usage_receipt_id"):
        make_subagent_additional_kwargs(
            "completed",
            result="done",
            usage_receipt_id=receipt_id,
        )


def test_subagent_usage_receipt_id_rejects_non_strings_as_value_errors() -> None:
    with pytest.raises(ValueError, match="usage_receipt_id must be a string"):
        make_subagent_additional_kwargs(
            "completed",
            result="done",
            usage_receipt_id=object(),  # type: ignore[arg-type]
        )


def test_subagent_usage_without_a_receipt_id_is_not_inferred() -> None:
    assert (
        read_subagent_usage_receipt(
            {
                "subagent_token_usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                }
            }
        )
        is None
    )


def test_subagent_usage_receipt_state_reads_conflict_tombstones() -> None:
    state = read_subagent_usage_receipt_state(
        {
            SUBAGENT_USAGE_RECEIPT_STATE_KEY: {
                "version": 1,
                "baseline": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
                "contributions": [],
                "conflicts": ["receipt-b", "receipt-a"],
            }
        }
    )

    assert state == (
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
        },
        {},
        frozenset({"receipt-a", "receipt-b"}),
    )


@pytest.mark.parametrize(
    "conflicts",
    [
        ["receipt-a", "receipt-a"],
        ["receipt-a", ""],
        "receipt-a",
    ],
)
def test_subagent_usage_receipt_state_rejects_malformed_conflicts(
    conflicts: object,
) -> None:
    assert (
        read_subagent_usage_receipt_state(
            {
                SUBAGENT_USAGE_RECEIPT_STATE_KEY: {
                    "version": 1,
                    "baseline": {
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "total_tokens": 5,
                    },
                    "contributions": [
                        {
                            "receipt_id": "accepted-receipt",
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ],
                    "conflicts": conflicts,
                }
            }
        )
        is None
    )


def test_subagent_usage_receipt_state_rejects_contribution_conflict_overlap() -> None:
    assert (
        read_subagent_usage_receipt_state(
            {
                SUBAGENT_USAGE_RECEIPT_STATE_KEY: {
                    "version": 1,
                    "baseline": {
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "total_tokens": 5,
                    },
                    "contributions": [
                        {
                            "receipt_id": "receipt-a",
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ],
                    "conflicts": ["receipt-a"],
                }
            }
        )
        is None
    )
