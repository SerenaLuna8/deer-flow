"""The post-summarization keep policy is token-count-only.

Platform settings, the harness config model, and the manual-compaction API all
retain recent history by token count. Message-count and context-fraction keep
measurements were removed; only internal force paths (manual compaction, Seal,
Dream) still use the ``("messages", 0)`` archive-all sentinel below the config
layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.summarization_config import ContextSize, SummarizationConfig


def test_summarization_keep_defaults_to_64000_tokens() -> None:
    config = SummarizationConfig()

    assert config.keep.to_tuple() == ("tokens", 64_000)


def test_summarization_keep_accepts_an_explicit_token_count() -> None:
    config = SummarizationConfig(keep={"type": "tokens", "value": 8_000})

    assert config.keep == ContextSize(type="tokens", value=8_000)
    assert config.keep.to_tuple() == ("tokens", 8_000)


@pytest.mark.parametrize(
    "keep",
    [
        {"type": "messages", "value": 10},
        {"type": "messages", "value": 0},
        {"type": "fraction", "value": 0.8},
    ],
)
def test_summarization_keep_rejects_retired_measurements(
    keep: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SummarizationConfig(keep=keep)


@pytest.mark.parametrize("value", [0, -1, 0.5])
def test_summarization_keep_requires_a_positive_token_count(
    value: int | float,
) -> None:
    with pytest.raises(ValidationError):
        SummarizationConfig(keep={"type": "tokens", "value": value})


def test_enabled_summarization_requires_keep_strictly_below_trigger() -> None:
    with pytest.raises(ValidationError):
        SummarizationConfig(
            enabled=True,
            trigger_tokens=64_000,
            keep={"type": "tokens", "value": 64_000},
        )
