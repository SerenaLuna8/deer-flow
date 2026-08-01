from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.summarization_config import SummarizationConfig


@pytest.mark.parametrize(
    "summary_prompt",
    [
        "Summarize this conversation.",
        "Summarize {conversation}.",
        "Summarize {messages",
        "Summarize {messages!r}.",
        "Summarize {messages:>20}.",
    ],
)
def test_summary_prompt_rejects_missing_unknown_or_unsafe_fields(summary_prompt: str) -> None:
    with pytest.raises(ValidationError, match=r"summary_prompt.*\{messages\}"):
        SummarizationConfig(summary_prompt=summary_prompt)


def test_summary_prompt_accepts_messages_field_and_literal_braces() -> None:
    prompt = 'Keep literal JSON {{"kind": "summary"}} and summarize:\n{messages}'

    config = SummarizationConfig(summary_prompt=prompt)

    assert config.summary_prompt == prompt
