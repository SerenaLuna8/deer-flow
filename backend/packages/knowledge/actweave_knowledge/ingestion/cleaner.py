"""Optional pre-processing rules applied between extraction and splitting.

Both rules default to off and are frozen on the document row at upload time,
so retries and the chunk preview always clean exactly like the first
ingestion. Text is newline-normalized before the rules run (the newline rule
assumes ``\\n``); URL/email removal runs first because it can leave doubled
spaces behind, which the whitespace rule then compresses when both are on.
"""

from __future__ import annotations

import re

from .extractor import ExtractedBlock
from .splitter import normalize_text

_EXCESS_NEWLINES = re.compile(r"\n{3,}")
# Horizontal whitespace only: newlines are paragraph structure and belong to
# the newline rule above. The class covers ASCII plus common Unicode spaces.
_HORIZONTAL_WHITESPACE_RUNS = re.compile(r"[\t\f\v\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]{2,}")
_EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
# Printable ASCII only (not \S): in Chinese text a URL is usually followed by
# CJK characters without whitespace, and \S+ would swallow them too.
_URL = re.compile(r"https?://[!-~]+")


def clean_text(text: str, *, remove_extra_spaces: bool, remove_urls_emails: bool) -> str:
    """Apply the enabled rules to one piece of extracted text."""

    if remove_urls_emails:
        text = _EMAIL.sub("", text)
        text = _URL.sub("", text)
    if remove_extra_spaces:
        text = _EXCESS_NEWLINES.sub("\n\n", text)
        text = _HORIZONTAL_WHITESPACE_RUNS.sub(" ", text)
    return text


def clean_blocks(
    blocks: list[ExtractedBlock],
    *,
    remove_extra_spaces: bool,
    remove_urls_emails: bool,
) -> list[ExtractedBlock]:
    """Clean every block; blocks that clean to nothing are dropped later by splitting."""

    if not remove_extra_spaces and not remove_urls_emails:
        return blocks
    return [
        ExtractedBlock(
            text=clean_text(
                normalize_text(block.text),
                remove_extra_spaces=remove_extra_spaces,
                remove_urls_emails=remove_urls_emails,
            ),
            source_position=block.source_position,
        )
        for block in blocks
    ]
