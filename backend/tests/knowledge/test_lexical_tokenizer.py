"""Frozen lexical tokens, query filtering, safe encoding, and size limits."""

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
    lexical_query_tokens,
    lexical_v1_tokens,
)


def _hex_token(token: str) -> str:
    return "x" + token.encode("utf-8").hex()


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


def test_punctuation_is_a_boundary_and_never_reaches_tsquery_syntax() -> None:
    assert lexical_v1_tokens("你好，世界！") == ["你", "你好", "好", "世", "世界", "界"]
    assert lexical_v1_tokens("a&b|c!(d)") == ["a", "b", "c", "d"]
    # Encoded output is a closed alphabet: no quoting or operator injection
    # can survive into the parameterized tsquery.
    encoded = lexical_index_input("重启 a&b|c '); DROP TABLE x; --")
    assert re.fullmatch(r"[xh0-9a-f ]*", encoded)


def test_document_positions_keep_repeats_while_the_query_dedupes() -> None:
    assert lexical_v1_tokens("重启 重启") == ["重", "重启", "启", "重", "重启", "启"]
    # The query side keeps only the bigram: index-side singles remain
    # matchable but never drive ranking through the OR query.
    assert lexical_query_input("重启 重启") == [_hex_token("重启")]


def test_query_drops_han_singles_except_a_lone_character_run() -> None:
    """Han singles are index-only noise for ranking; a one-character run is
    the only single the query keeps, so ``锌`` still finds ``锌``."""

    assert lexical_query_tokens("网络故障") == ["网络", "络故", "故障"]
    assert lexical_query_tokens("锌") == ["锌"]
    assert lexical_query_tokens("查询HTTP 404状态") == ["查询", "http", "404", "状态"]
    # The index side is unchanged: existing lexical_version=1 rows stay valid.
    assert lexical_v1_tokens("网络故障") == ["网", "网络", "络", "络故", "故", "故障", "障"]


def test_query_drops_function_word_bigrams_and_english_stopwords() -> None:
    """A Han bigram made only of function characters (的/了/是/在/和/…) and
    common English stopwords carry no matching signal; identifiers, numbers
    and IPs are never filtered."""

    assert lexical_query_tokens("这是的了") == []
    assert lexical_query_tokens("路由器的配置") == ["路由", "由器", "器的", "的配", "配置"]
    assert lexical_query_tokens("what is the error code of E-1042") == ["error", "code", "e-1042", "e", "1042"]
    assert lexical_query_tokens("the 10.0.0.1 and a") == ["10.0.0.1"]


def test_long_chinese_questions_now_fit_the_query_token_cap() -> None:
    """The measured failure: a 49-character question produced 95 tokens
    under singles+bigrams; bigrams only keep even 120 characters under 128."""

    question = "网络故障排查手册中关于路由器无法连接互联网时应该如何处理的详细步骤以及常见的错误代码说明和解决方案"
    assert len(question) == 49
    assert len(lexical_query_input(question)) <= len(question)
    longer = "".join(chr(0x4E00 + index) for index in range(120))
    assert len(lexical_query_input(longer)) == 119


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
