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

The query side derives a *subset* of the index stream so stored rows stay
valid without a version bump: Han singles are dropped unless the run is one
character long (the index keeps every single, so ``锌`` still matches ``锌``),
and stop tokens — a Han bigram made only of function characters, or a common
English stopword — are removed. Identifiers, numbers, and IPs are never
filtered. Dropping singles is what lets a 120-character Chinese question fit
the 128-token cap and keeps ``的/是/在`` from dominating ``ts_rank_cd``.
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
    "lexical_query_tokens",
    "lexical_v1_tokens",
]

# Hard cap on one segment's derivation input (UTF-8 bytes of the content).
# The documented 16000-character content bound (48 KiB of UTF-8) stays far
# below this; hitting it is a contract violation that must fail the write,
# not drop the text.
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

# Query-side stop tokens. A Han bigram whose two characters are both in this
# function-character set carries no matching signal; a bigram with one content
# character (``的配``) is kept because it still anchors the adjacent word.
_HAN_STOP_CHARS = frozenset("的了是在和与或及等这那也就都而且但如果因为所以于以为把被让给对从到着过吗呢啊吧呀之其")
_ASCII_STOP_WORDS = frozenset("a an the of to in on at by for with and or is are was were be been being this that these those it its as from into than then there here what which who whom whose how why when where do does did not no".split())


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
    """The plain (pre-encoding) index token stream, repeats and positions intact."""

    return _scan(text, han_singles=True)


def _scan(text: str, *, han_singles: bool) -> list[str]:
    """Shared scan; ``han_singles`` selects the index (True) or query (False)
    treatment of Han runs longer than one character."""

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
            if han_singles or len(run) == 1:
                for offset in range(len(run)):
                    tokens.append(run[offset])
                    if offset + 1 < len(run):
                        tokens.append(run[offset : offset + 2])
            else:
                tokens.extend(run[offset : offset + 2] for offset in range(len(run) - 1))
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


def _is_han_token(token: str) -> bool:
    return all(_is_han(char) for char in token)


def _is_stop_token(token: str) -> bool:
    if _is_han_token(token):
        return len(token) == 2 and all(char in _HAN_STOP_CHARS for char in token)
    return token in _ASCII_STOP_WORDS


def lexical_query_tokens(query: str) -> list[str]:
    """The plain query token stream: a subset of the index rule.

    Han runs contribute their bigrams only, except a one-character run, which
    keeps its single so it can still match the index-side single. Han bigrams
    made entirely of function characters and English stopwords are dropped;
    identifiers (which contain a separator), IPs, and digits pass untouched.
    Repeats are preserved here; :func:`lexical_query_input` dedupes.
    """

    return [token for token in _scan(query, han_singles=False) if not _is_stop_token(token)]


def lexical_query_input(query: str) -> list[str]:
    """Encoded query tokens (a subset of the index rule), deduplicated in order."""

    seen: set[str] = set()
    encoded: list[str] = []
    for token in lexical_query_tokens(query):
        code = encode_lexical_token(token)
        if code not in seen:
            seen.add(code)
            encoded.append(code)
    return encoded
