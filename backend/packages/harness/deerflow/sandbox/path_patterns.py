"""Shared construction of host-to-virtual output masking regexes."""

from __future__ import annotations

import re

_SEGMENT_BOUNDARY = r"(?=/|$|[^\w./-])"
_PATH_TAIL = r"(?:[/\\][^\s\"';&|<>()]*)?"


def build_output_mask_pattern(
    base: str,
    *,
    separator_agnostic: bool = False,
) -> re.Pattern[str]:
    """Compile a host-root matcher that stops at a real path boundary."""
    escaped = re.escape(base)
    if separator_agnostic:
        escaped = escaped.replace(r"\\", r"[/\\]")
    return re.compile(escaped + _SEGMENT_BOUNDARY + _PATH_TAIL)
