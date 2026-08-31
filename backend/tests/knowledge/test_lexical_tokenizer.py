"""T8 — lexical_v1: the in-package deterministic tokenizer (design §8.1).

Pure-function tests: normalization (NFKC + English lowercasing), the fixed
scan order (complete valid IPs, then the longest ASCII identifier with
internal ``._:/-`` separators, then contiguous Han runs as singles plus
overlapping bigrams, then other letter/digit runs, punctuation as boundaries),
in-fragment part deduplication, cross-position repetition, the 128-byte
``x``-hex / SHA-256 ``h``-hex safe encoding, the 256 KiB derived-input hard
limit, and the exact index-input snapshots the version contract freezes.
The token sequence itself is part of ``lexical_version=1``: changing any
expectation here requires bumping the version and re-running the quality
evaluation.
"""

from __future__ import annotations

import hashlib
import re
from ipaddress import ip_address

import pytest
from actweave_knowledge import KNOWLEDGE_INVALID_REQUEST, KnowledgeError
from actweave_knowledge.retrieval import (
    LEXICAL_INDEX_INPUT_MAX_BYTES,
    LEXICAL_TOKEN_ENCODED_MAX_BYTES,
    encode_lexical_token,
    lexical_index_input,
    lexical_query_input,
    lexical_v1_tokens,
)


def _hex_token(token: str) -> str:
    return "x" + token.encode("utf-8").hex()


# ---------------------------------------------------------------------------
# Normalization and the frozen sample tokens
# ---------------------------------------------------------------------------


def test_nfkc_and_lowercase_produce_the_frozen_identifier_sample() -> None:
    """The design's frozen sample: ＡB-12 → [ab-12, ab, 12]."""

    assert lexical_v1_tokens("ＡB-12") == ["ab-12", "ab", "12"]


def test_chinese_runs_emit_singles_and_overlapping_bigrams() -> None:
    """The design's frozen sample: 网络 → [网, 网络, 络]."""

    assert lexical_v1_tokens("网络") == ["网", "网络", "络"]
    assert lexical_v1_tokens("网络层") == ["网", "网络", "络", "络层", "层"]
    assert lexical_v1_tokens("网") == ["网"]


def test_mixed_chinese_english_and_digits_keep_scan_order() -> None:
    assert lexical_v1_tokens("查询HTTP 404状态") == [
        "查",
        "查询",
        "询",
        "http",
        "404",
        "状",
        "状态",
        "态",
    ]


# ---------------------------------------------------------------------------
# IPs and identifiers
# ---------------------------------------------------------------------------


def test_valid_ips_stay_complete_and_canonical() -> None:
    assert lexical_v1_tokens("10.0.0.1") == ["10.0.0.1"]
    assert lexical_v1_tokens("网关fe80::1下线") == ["网", "网关", "关", "fe80::1", "下", "下线", "线"]
    # The canonical form comes from the standard library, and the query path
    # applies the same rule, so both sides agree on the stored token.
    assert lexical_v1_tokens("::ffff:192.0.2.1") == [str(ip_address("::ffff:192.0.2.1"))]


def test_invalid_ip_shapes_fall_back_to_identifier_rules() -> None:
    # Five dotted runs are not a complete IPv4; the leading four are.
    assert lexical_v1_tokens("1.2.3.4.5") == ["1.2.3.4", "5"]
    # Python's ip_address rejects leading zeros: identifier with parts.
    assert lexical_v1_tokens("192.168.001.001") == ["192.168.001.001", "192", "168", "001"]
    # A time-like fragment is no IP either.
    assert lexical_v1_tokens("12:30") == ["12:30", "12", "30"]


def test_identifiers_emit_the_complete_item_then_deduplicated_parts() -> None:
    assert lexical_v1_tokens("err_code-42.err") == ["err_code-42.err", "err", "code", "42"]
    assert lexical_v1_tokens("api/v2/run") == ["api/v2/run", "api", "v2", "run"]
    # Endpoint: the IP wins first, the port stays a plain word.
    assert lexical_v1_tokens("10.0.0.1:8080") == ["10.0.0.1", "8080"]


def test_urls_split_on_unbridged_separators_without_rejecting_the_text() -> None:
    assert lexical_v1_tokens("https://a.b/c") == ["https", "a.b/c", "a", "b", "c"]


# ---------------------------------------------------------------------------
# Boundaries, repetition, dedupe
# ---------------------------------------------------------------------------


def test_punctuation_is_a_boundary_and_never_reaches_tsquery_syntax() -> None:
    assert lexical_v1_tokens("你好，世界！") == ["你", "你好", "好", "世", "世界", "界"]
    assert lexical_v1_tokens("a&b|c!(d)") == ["a", "b", "c", "d"]
    # Encoded output is a closed alphabet: no quoting or operator injection
    # can survive into the parameterized tsquery.
    encoded = lexical_index_input("重启 a&b|c '); DROP TABLE x; --")
    assert re.fullmatch(r"[xh0-9a-f ]*", encoded)


def test_document_positions_keep_repeats_while_the_query_dedupes() -> None:
    assert lexical_v1_tokens("重启 重启") == ["重", "重启", "启", "重", "重启", "启"]
    assert lexical_query_input("重启 重启") == [
        _hex_token("重"),
        _hex_token("重启"),
        _hex_token("启"),
    ]


def test_zero_token_inputs_produce_an_empty_stream() -> None:
    assert lexical_v1_tokens("！？。…") == []
    assert lexical_index_input("！？。…") == ""
    assert lexical_query_input("  ") == []


# ---------------------------------------------------------------------------
# Safe encoding and limits
# ---------------------------------------------------------------------------


def test_tokens_encode_as_hex_below_and_hash_above_128_bytes() -> None:
    short = "错误码e404"
    for token in lexical_v1_tokens(short):
        assert encode_lexical_token(token) == "x" + token.encode("utf-8").hex()

    long_token = "a" * (LEXICAL_TOKEN_ENCODED_MAX_BYTES + 1)
    assert encode_lexical_token(long_token) == "h" + hashlib.sha256(long_token.encode("utf-8")).hexdigest()
    # A long unbroken run keeps the document indexable instead of failing it.
    stream = lexical_index_input(long_token)
    assert stream.startswith("h")
    assert " " not in stream


def test_exact_index_input_snapshot_is_a_version_contract() -> None:
    assert lexical_index_input("ＡB-12 网络") == "x61622d3132 x6162 x3132 xe7bd91 xe7bd91e7bb9c xe7bb9c"


def test_derived_input_over_256kib_fails_loudly_instead_of_dropping_text() -> None:
    at_limit = "a" * LEXICAL_INDEX_INPUT_MAX_BYTES
    assert lexical_index_input(at_limit).startswith("h")

    with pytest.raises(KnowledgeError) as error:
        lexical_index_input("汉" * (LEXICAL_INDEX_INPUT_MAX_BYTES // 3 + 1))
    assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    assert "256" in error.value.message


def test_the_documented_4000_char_content_bound_fits_the_limit() -> None:
    # The densest legal segment (4000 Han chars, 12 KB UTF-8) stays far below
    # the 256 KiB derived-input bound and produces singles plus bigrams.
    content = "installation安装指南" * 250
    assert len(content) == 4000
    tokens = lexical_v1_tokens(content)
    # 250 repeats of one word, four Han singles, and three bigrams.
    assert len(tokens) == 250 * 8
    assert lexical_index_input(content) != ""
