from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from actweave_knowledge.extraction.contracts import ExtractionError
from actweave_knowledge.ingestion.tokenizer import (
    count_knowledge_tokens,
)


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
