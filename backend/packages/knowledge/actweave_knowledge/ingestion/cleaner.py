"""Optional pre-processing rules applied between extraction and splitting.

Both rules default to off and are frozen on the document row at upload time,
so retries and the chunk preview always clean exactly like the first
ingestion. Text is newline-normalized before the rules run (the newline rule
assumes ``\\n``); URL/email removal runs first because it can leave doubled
spaces behind, which the whitespace rule then compresses when both are on.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

from ..extraction.contracts import Document
from .extractor import ExtractedBlock
from .source_mapping import edit_document
from .splitter import normalize_text
from .structure import inline_atoms

_EXCESS_NEWLINES = re.compile(r"\n{3,}")
# Horizontal whitespace only: newlines are paragraph structure and belong to
# the newline rule above. The class covers ASCII plus common Unicode spaces.
_HORIZONTAL_WHITESPACE_RUNS = re.compile(r"[\t\f\v\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]{2,}")
_EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
_MARKDOWN_EMAIL = re.compile(r"(?:[A-Za-z0-9]|\\?[_.+-])+@(?:[A-Za-z0-9]|\\?-)+\\?\.(?:[A-Za-z0-9]|\\?[.-])+")
_MARKDOWN_WHITESPACE_RUNS = re.compile(r"(?:[\t\f\v\x20\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]|(?<!\\)&#(?:32|9);){2,}")
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


def clean_documents(documents: tuple[Document, ...], *, remove_extra_spaces: bool, remove_urls_emails: bool) -> tuple[Document, ...]:
    """Clean editable text only, carrying source and occurrence offsets along."""
    if not remove_extra_spaces and not remove_urls_emails:
        return documents
    result = []
    for document in documents:
        text = document.page_content
        offsets = [0]
        for line in text.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        code_ranges = []
        protected = inline_atoms(text)
        for token in MarkdownIt("commonmark", {"html": False}).parse(text):
            if token.type in {"fence", "code_block", "html_block"} and token.map:
                interval = (offsets[token.map[0]], offsets[token.map[1]])
                code_ranges.append(interval)
                protected.append(interval)
        protected.extend((item.source.start, item.source.end) for item in document.attachments)
        if remove_urls_emails:
            edits = []
            for start, end in inline_atoms(text):
                atom = text[start:end]
                if atom.startswith("[") and not any(a <= start < b for a, b in code_ranges):
                    label_end = atom.find("](")
                    if re.match(r"(?:https?://|mailto:)", atom[label_end + 2 :]):
                        edits.extend([(start, start + 1, ""), (start + label_end, end, "")])
            if edits:
                document = edit_document(document, edits)
                protected = [
                    (start + sum(len(value) - (b - a) for a, b, value in edits if b <= start), end + sum(len(value) - (b - a) for a, b, value in edits if b <= end))
                    for start, end in protected
                    if not any(start <= a < end for a, _, _ in edits)
                ]
        patterns = []
        if remove_urls_emails:
            patterns.extend([(_MARKDOWN_EMAIL, ""), (_URL, "")])
        if remove_extra_spaces:
            patterns.extend([(_EXCESS_NEWLINES, "\n\n"), (_MARKDOWN_WHITESPACE_RUNS, " ")])
        current = document
        for pattern, replacement in patterns:
            edits = [(m.start(), m.end(), replacement) for m in pattern.finditer(current.page_content) if not any(start < m.end() and end > m.start() for start, end in protected)]
            current = edit_document(current, edits)
            protected = [(start + sum(len(value) - (b - a) for a, b, value in edits if b <= start), end + sum(len(value) - (b - a) for a, b, value in edits if b <= end)) for start, end in protected]
        result.append(current)
    return tuple(result)


def clean_character_document(document: Document, *, remove_extra_spaces: bool, remove_urls_emails: bool) -> Document:
    """Map the historical normalization/rules without reinterpreting its units."""

    def apply(pattern, replacement):
        nonlocal document
        document = edit_document(document, [(m.start(), m.end(), replacement) for m in pattern.finditer(document.page_content)])

    def normalize():
        nonlocal document
        apply(re.compile(r"\r\n?"), "\n")
        apply(re.compile(r"[^\S\n]+(?=\n|$)"), "")
        apply(_EXCESS_NEWLINES, "\n\n")
        apply(re.compile(r"^\s+|\s+$"), "")

    normalize()
    if remove_urls_emails:
        apply(_EMAIL, "")
        apply(_URL, "")
    if remove_extra_spaces:
        apply(_EXCESS_NEWLINES, "\n\n")
        apply(_HORIZONTAL_WHITESPACE_RUNS, " ")
    normalize()
    return document
