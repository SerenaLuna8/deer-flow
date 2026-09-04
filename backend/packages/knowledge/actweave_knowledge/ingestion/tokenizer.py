"""Fixed local Tokenizer for reproducible Knowledge chunking.

The encoding is constructed only from the packaged vocabulary.  In particular,
this module must never call ``tiktoken.get_encoding`` because that resolver can
download encoding data through a user cache on first use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files

import tiktoken

from actweave_knowledge.extraction.contracts import ExtractionError

TOKENIZER_PROFILE_ID = "knowledge-cl100k-v1"
_RESOURCE_PACKAGE = "actweave_knowledge.ingestion"
_RESOURCE_DIRECTORY = "tokenizer_data"
_PREFIX_CACHE_PATTERN = r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
_PREFIX_BOUNDARY = re.compile(r"(?<=\S) (?=[A-Za-z])")


def _unavailable() -> ExtractionError:
    """Return a stable, path-free error for every local-resource failure."""

    return ExtractionError("TOKENIZER_UNAVAILABLE", "知识库 Tokenizer 不可用")


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_ranks(payload: bytes) -> dict[bytes, int]:
    """Parse the bundled BPE text without tiktoken's cache-aware file loader."""

    ranks: dict[bytes, int] = {}
    for line in payload.splitlines():
        encoded, raw_rank = line.split()
        ranks[base64.b64decode(encoded)] = int(raw_rank)
    return ranks


@lru_cache(maxsize=1)
def _load_manifest() -> tuple[dict[str, object], bytes]:
    try:
        root = files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_DIRECTORY)
        manifest_bytes = root.joinpath("manifest.json").read_bytes()
        value = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"pat_str", "profile_id", "sha256", "special_tokens"}:
            raise ValueError
        if value.get("profile_id") != TOKENIZER_PROFILE_ID:
            raise ValueError
        if not isinstance(value.get("pat_str"), str) or not isinstance(value.get("special_tokens"), dict):
            raise ValueError
        expected_hash = value.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError
        int(expected_hash, 16)
        if _canonical_manifest_bytes(value) != manifest_bytes:
            raise ValueError
        return value, manifest_bytes
    except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise _unavailable() from None


@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding:
    try:
        manifest, _ = _load_manifest()
        root = files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_DIRECTORY)
        data = root.joinpath("cl100k_base.tiktoken")
        payload = data.read_bytes()
        expected_hash = manifest["sha256"]
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError
        ranks = _parse_ranks(payload)
        return tiktoken.Encoding(
            name=TOKENIZER_PROFILE_ID,
            pat_str=manifest["pat_str"],
            mergeable_ranks=ranks,
            special_tokens=manifest["special_tokens"],
        )
    except ExtractionError:
        raise
    except (OSError, ValueError, TypeError):
        raise _unavailable() from None


def count_knowledge_tokens(text: str, *, profile_id: str = TOKENIZER_PROFILE_ID) -> int:
    """Count text with the sole supported fixed Knowledge Tokenizer profile."""

    if profile_id != TOKENIZER_PROFILE_ID or not isinstance(text, str):
        raise _unavailable()
    return len(_encoder().encode_ordinary(text))


class PrefixTokenCounter:
    """Reuse one stable BPE prefix while a split candidate grows.

    For the frozen pattern, a space after non-whitespace and before an ASCII
    letter starts a new letter pre-token. The prefix ends in non-whitespace,
    so encoding it alone cannot trigger the pattern's end-of-input whitespace
    rules. BPE never merges across these pre-token boundaries. Keep the last
    word in the tail because appending letters can still change its tokens.
    """

    def __init__(self) -> None:
        manifest, _ = _load_manifest()
        self._enabled = manifest["pat_str"] == _PREFIX_CACHE_PATTERN
        self._prefix = ""
        self._prefix_tokens = 0

    def count(self, text: str) -> int:
        if not self._enabled or not isinstance(text, str):
            return count_knowledge_tokens(text)
        if self._prefix and (not text.startswith(self._prefix) or not _PREFIX_BOUNDARY.match(text, len(self._prefix))):
            self._prefix = ""
            self._prefix_tokens = 0

        tail = text[len(self._prefix) :]
        boundary = 0
        for match in _PREFIX_BOUNDARY.finditer(tail):
            boundary = match.start()
        if boundary:
            self._prefix_tokens += count_knowledge_tokens(tail[:boundary])
            self._prefix = text[: len(self._prefix) + boundary]
        # ponytail: tails without proven boundaries are recounted; extend only
        # if profiling warrants another pattern-specific boundary proof.
        return self._prefix_tokens + count_knowledge_tokens(tail[boundary:])


def tokenizer_fingerprint() -> str:
    """Return the digest of the canonical packaged manifest, not parse identity."""

    _, manifest_bytes = _load_manifest()
    return hashlib.sha256(manifest_bytes).hexdigest()
