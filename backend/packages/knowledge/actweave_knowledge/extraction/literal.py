"""Serialize ordinary source text, never already-generated Markdown."""

import re

_INLINE = re.compile(r"([\\`*_\[\]<>#!~&])")
_BLOCK = re.compile(r"(?m)^([ \t]{0,3})([-+=]|[0-9]{0,9}[.)])")
_INDENT = re.compile(r"(?m)^(?= {4}| {0,3}\t)[ \t]")


def escape_literal_text(
    text: str,
    *,
    protect_indentation: bool = True,
    escape_pipes: bool = True,
) -> str:
    text = _INLINE.sub(r"\\\1", text)
    if escape_pipes:
        text = text.replace("|", r"\|")
    text = _BLOCK.sub(lambda match: match[1] + match[2][:-1] + "\\" + match[2][-1], text)
    if protect_indentation:
        text = _INDENT.sub(lambda match: "&#9;" if match[0] == "\t" else "&#32;", text)
    return text
