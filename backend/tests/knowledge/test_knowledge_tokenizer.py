from __future__ import annotations

import os
import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest
from actweave_knowledge.extraction.contracts import ExtractionError
from actweave_knowledge.ingestion import tokenizer
from actweave_knowledge.ingestion.tokenizer import (
    PrefixTokenCounter,
    count_knowledge_tokens,
)

# Whitespace runs, contractions, digits, CJK, combining marks and emoji around
# the "non-blank, space, ASCII letter" split point the prefix cache relies on.
_FUZZ_ALPHABET = (
    list(string.ascii_letters) * 6
    + list(string.digits) * 2
    + list(".,!?;:'\"-()[]{}*_`#|<>/\\&%$@~^=+") * 2
    + [" "] * 10
    + ["\t", "\n", "\n", "\r", "\r\n", "\u00a0", "\u3000", "\u200b"]
    + list("中文设备状态检查请注意事项第节") * 3
    + ["é", "ü", "ß", "e\u0301", "😀", "🌐", "𝒳", "Ω", "ж", "ا", "अ"]
    + ["'s", "'t", "'re", "'ll", "'ve", "'d", "'S", "'LL", " a", " b", " the", "1 ", " 2", "\n\n", " \n ", "  "]
)


class _RecordingEncoder:
    """Stand-in for ``_encoder()`` that records how many characters get encoded."""

    def __init__(self) -> None:
        self.real = tokenizer._encoder()
        self.encoded_lengths: list[int] = []

    def encode_ordinary(self, text: str) -> list[int]:
        self.encoded_lengths.append(len(text))
        return self.real.encode_ordinary(text)


def _random_text(rng: random.Random, low: int, high: int) -> str:
    return "".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(low, high)))


def test_prefix_token_counter_matches_full_counts_for_interleaved_streams() -> None:
    """Splitting at a non-boundary would silently change every chunk budget decision."""

    rng = random.Random(20260904)
    counter = PrefixTokenCounter()
    for _ in range(150):
        streams = [_random_text(rng, 0, 12), _random_text(rng, 0, 12)]
        for _ in range(rng.randint(1, 40)):
            index = rng.randrange(len(streams))
            streams[index] += _random_text(rng, 1, 6)
            assert counter.count(streams[index]) == count_knowledge_tokens(streams[index])
            if rng.random() < 0.2:
                probe = _random_text(rng, 0, 10)
                assert counter.count(probe) == count_knowledge_tokens(probe)
            if rng.random() < 0.1:
                shorter = streams[index][: rng.randint(0, len(streams[index]))]
                assert counter.count(shorter) == count_knowledge_tokens(shorter)


def test_prefix_token_counter_invalidates_a_changed_boundary() -> None:
    """Matching cached text is insufficient when its following boundary changed."""

    counter = PrefixTokenCounter()
    counter.count("aa\u0301 a")

    assert counter.count("aa\u0301's c") == 5


@pytest.mark.parametrize(
    "steps",
    [
        ("x", " \n", " ", " a", "b", " c"),
        ("it", "'s", " a", " test", "'ll", " do"),
        ("1234", " abc", " 567", " d"),
        ("设备", " status", " 正常", "！", " ok"),
        ("a", " ", " b", "  ", " c"),
        ("a\u00a0b", " c", "\u3000", " d"),
        ("end.", " Next", "\r\n", " line", "\n\n", " para"),
        ("tab\t", "a", " b", "\t", " c"),
        ("e\u0301", " a", "😀", " b", "🌐 c"),
        ("'s", " a", "'S", " B"),
        ("trailing ", " x", " ", " y "),
    ],
)
def test_prefix_token_counter_matches_full_counts_around_whitespace_edges(steps: tuple[str, ...]) -> None:
    """Whitespace before a split point is pre-tokenized differently at end of input."""

    counter = PrefixTokenCounter()
    text = ""
    for step in steps:
        text += step
        assert counter.count(text) == count_knowledge_tokens(text), repr(text)


def test_prefix_token_counter_encodes_only_the_unstable_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-encoding the whole candidate per appended word makes chunk packing quadratic."""

    recording = _RecordingEncoder()
    monkeypatch.setattr(tokenizer, "_encoder", lambda: recording)
    counter = PrefixTokenCounter()
    text = "# 标题\n\nword"
    counter.count(text)
    recording.encoded_lengths.clear()

    for index in range(2000):
        text += f" w{index}"
        assert counter.count(text) == len(recording.real.encode_ordinary(text))

    assert sum(recording.encoded_lengths) < 100_000
    assert max(recording.encoded_lengths) < 64


def test_prefix_token_counter_falls_back_to_full_counts_for_another_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """The split-point proof holds only for the packaged pre-tokenizer pattern."""

    manifest, raw = tokenizer._load_manifest()
    monkeypatch.setattr(tokenizer, "_load_manifest", lambda: ({**manifest, "pat_str": r"\S+|\s+"}, raw))
    recording = _RecordingEncoder()
    monkeypatch.setattr(tokenizer, "_encoder", lambda: recording)
    counter = PrefixTokenCounter()

    counter.count("alpha beta")
    counter.count("alpha beta gamma")

    assert recording.encoded_lengths == [len("alpha beta"), len("alpha beta gamma")]


def test_cl100k_count_uses_tokens_not_characters() -> None:
    """Changing token encoding to character counting breaks this assertion."""

    assert count_knowledge_tokens("hello world") == 2
    assert count_knowledge_tokens("hello world") != len("hello world")
    assert count_knowledge_tokens("网络 🌐 List<int> values;") == 9


def test_unknown_tokenizer_profile_fails_with_safe_reason_code() -> None:
    """Accepting arbitrary profile IDs breaks frozen chunk-profile identity."""

    with pytest.raises(ExtractionError) as error:
        count_knowledge_tokens("hello", profile_id="another-profile")

    assert error.value.reason_code == "TOKENIZER_UNAVAILABLE"
    assert "/" not in error.value.message


def test_subprocess_counts_offline_without_a_user_cache(tmp_path: Path) -> None:
    """A runtime get_encoding/download path attempts a blocked socket connection."""

    package_root = Path(__file__).resolve().parents[2] / "packages" / "knowledge"
    copied_package_root = tmp_path / "package"
    shutil.copytree(package_root / "actweave_knowledge", copied_package_root / "actweave_knowledge")
    isolated_cache = tmp_path / "empty-tiktoken-cache"
    code = "\n".join(
        (
            "import socket",
            "def blocked(*args, **kwargs): raise AssertionError('network access attempted')",
            "socket.socket.connect = blocked",
            "from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens",
            "assert count_knowledge_tokens('hello world') == 2",
        )
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(copied_package_root)
    environment["TIKTOKEN_CACHE_DIR"] = str(isolated_cache)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not isolated_cache.exists()


@pytest.mark.parametrize("resource_state", ["missing", "tampered"])
def test_invalid_packaged_vocab_fails_without_exposing_a_path(tmp_path: Path, resource_state: str) -> None:
    """Skipping local resource validation accepts missing or altered vocabulary data."""

    package_root = Path(__file__).resolve().parents[2] / "packages" / "knowledge"
    copied_package_root = tmp_path / "package"
    shutil.copytree(package_root / "actweave_knowledge", copied_package_root / "actweave_knowledge")
    vocab = copied_package_root / "actweave_knowledge" / "ingestion" / "tokenizer_data" / "cl100k_base.tiktoken"
    if resource_state == "missing":
        vocab.unlink()
    else:
        vocab.write_bytes(vocab.read_bytes() + b"tampered")
    code = "\n".join(
        (
            "from actweave_knowledge.ingestion.tokenizer import count_knowledge_tokens",
            "from actweave_knowledge.extraction.contracts import ExtractionError",
            "try:",
            "    count_knowledge_tokens('hello')",
            "except ExtractionError as error:",
            "    assert error.reason_code == 'TOKENIZER_UNAVAILABLE'",
            "    assert '/' not in error.message",
            "else:",
            "    raise AssertionError('invalid vocabulary was accepted')",
        )
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(copied_package_root)

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
