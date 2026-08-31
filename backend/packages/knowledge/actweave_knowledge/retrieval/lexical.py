"""lexical_v1 — the deterministic in-package tokenizer (design §8.1).

The lexical route never depends on database-side dictionaries: Python derives
a token stream here and PostgreSQL only stores it (``to_tsvector('simple')``
over the encoded stream) and matches it (parameterized OR ``tsquery``). The
token sequence, the encoding, and the limits below are all part of
``lexical_version=1``; any behavioral change must bump the version so stale
rows fail loudly instead of silently mixing token dialects.

Scan order over the NFKC-normalized, lowercased text, left to right:

1. A complete IP address, validated by the standard library and stored in
   canonical form (both indexing and querying apply the same rule, so the
   canonical token always agrees across the two sides).
2. The longest ASCII identifier with internal ``._:/-`` separators
   (``err_code-42.err``): the complete item first, then its deduplicated
   parts at the same scan position.
3. A contiguous Han run: every single character plus overlapping bigrams
   (``网络`` → ``网, 网络, 络``).
4. Any other letter/digit run, kept whole.
5. Everything else (punctuation, symbols, whitespace) is a boundary and is
   never re-parsed.

Repeated tokens keep their document positions; only the query side dedupes.
Encoded tokens are ``x`` + UTF-8 hex for tokens up to 128 UTF-8 bytes and
``h`` + SHA-256 hex above that, a closed ``[xh0-9a-f]`` alphabet that cannot
collide with tsquery syntax.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from ipaddress import ip_address

from actweave_knowledge.contracts import KNOWLEDGE_INVALID_REQUEST, KnowledgeError

__all__ = [
    "LEXICAL_INDEX_INPUT_MAX_BYTES",
    "LEXICAL_TOKEN_ENCODED_MAX_BYTES",
    "encode_lexical_token",
    "lexical_index_input",
    "lexical_query_input",
    "lexical_v1_tokens",
]

# Hard cap on one segment's derivation input (UTF-8 bytes of the content).
# The documented 4000-character content bound stays far below this; hitting
# it is a contract violation that must fail the write, not drop the text.
LEXICAL_INDEX_INPUT_MAX_BYTES = 256 * 1024

# Tokens at most this many UTF-8 bytes encode as reversible hex; longer ones
# hash so no single token can reject an otherwise legal segment.
LEXICAL_TOKEN_ENCODED_MAX_BYTES = 128

# CJK Unified Ideographs: Extension A, the base block, and the compatibility
# block. Fixed ranges keep the version contract independent of the Unicode
# tables shipped with the running interpreter.
_HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))

_ASCII_WORD = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_IPV6_GATE = frozenset("0123456789abcdef:")

# At least two runs bridged by single separators; greedy, so the match is the
# longest identifier at the scan position.
_IDENTIFIER_RE = re.compile(r"[a-z0-9]+(?:[._:/\-][a-z0-9]+)+")
_IDENTIFIER_PART_RE = re.compile(r"[._:/\-]")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
# Candidate shapes only; ``ip_address`` decides validity (it rejects leading
# zeros, out-of-range octets, and malformed groups).
_IPV4_CANDIDATE_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_IPV6_CANDIDATE_RE = re.compile(r"[0-9a-f.:]+")


def _is_han(char: str) -> bool:
    code_point = ord(char)
    return any(low <= code_point <= high for low, high in _HAN_RANGES)


def _match_ip(text: str, start: int) -> tuple[str, int] | None:
    """A complete valid IP at ``start``, canonicalized, or nothing."""

    candidate = _IPV6_CANDIDATE_RE.match(text, start)
    if candidate is not None and ":" in candidate.group():
        try:
            return str(ip_address(candidate.group())), candidate.end()
        except ValueError:
            pass
    candidate = _IPV4_CANDIDATE_RE.match(text, start)
    if candidate is not None:
        try:
            return str(ip_address(candidate.group())), candidate.end()
        except ValueError:
            pass
    return None


def lexical_v1_tokens(text: str) -> list[str]:
    """The plain (pre-encoding) token stream, repeats and positions intact."""

    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    index = 0
    length = len(normalized)
    while index < length:
        char = normalized[index]
        if char in _IPV6_GATE:
            ip_match = _match_ip(normalized, index)
            if ip_match is not None:
                tokens.append(ip_match[0])
                index = ip_match[1]
                continue
        if char in _ASCII_WORD:
            identifier = _IDENTIFIER_RE.match(normalized, index)
            if identifier is not None:
                full = identifier.group()
                tokens.append(full)
                seen = {full}
                for part in _IDENTIFIER_PART_RE.split(full):
                    if part not in seen:
                        seen.add(part)
                        tokens.append(part)
                index = identifier.end()
                continue
            word = _ASCII_WORD_RE.match(normalized, index)
            assert word is not None  # char is in _ASCII_WORD
            tokens.append(word.group())
            index = word.end()
            continue
        if _is_han(char):
            end = index
            while end < length and _is_han(normalized[end]):
                end += 1
            run = normalized[index:end]
            for offset in range(len(run)):
                tokens.append(run[offset])
                if offset + 1 < len(run):
                    tokens.append(run[offset : offset + 2])
            index = end
            continue
        if char.isalnum() and not char.isascii():
            end = index
            while end < length:
                current = normalized[end]
                if not current.isalnum() or current.isascii() or _is_han(current):
                    break
                end += 1
            tokens.append(normalized[index:end])
            index = end
            continue
        index += 1
    return tokens


def encode_lexical_token(token: str) -> str:
    """Closed-alphabet encoding: ``x`` + hex, or ``h`` + SHA-256 when long."""

    raw = token.encode("utf-8")
    if len(raw) <= LEXICAL_TOKEN_ENCODED_MAX_BYTES:
        return "x" + raw.hex()
    return "h" + hashlib.sha256(raw).hexdigest()


def lexical_index_input(content: str) -> str:
    """The encoded stream ``to_tsvector('simple', …)`` indexes for one row.

    Raises on inputs above the 256 KiB derivation bound: dropping text
    silently would desynchronize the lexical route from the stored content.
    """

    if len(content.encode("utf-8")) > LEXICAL_INDEX_INPUT_MAX_BYTES:
        raise KnowledgeError(
            KNOWLEDGE_INVALID_REQUEST,
            "内容超过词法派生输入上限（256KiB UTF-8），无法建立词法索引",
        )
    return " ".join(encode_lexical_token(token) for token in lexical_v1_tokens(content))


def lexical_query_input(query: str) -> list[str]:
    """Encoded query tokens, same rule as indexing, deduplicated in order."""

    seen: set[str] = set()
    encoded: list[str] = []
    for token in lexical_v1_tokens(query):
        code = encode_lexical_token(token)
        if code not in seen:
            seen.add(code)
            encoded.append(code)
    return encoded
