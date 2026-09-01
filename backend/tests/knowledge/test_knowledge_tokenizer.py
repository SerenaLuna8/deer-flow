from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from actweave_knowledge.extraction.contracts import ExtractionError
from actweave_knowledge.ingestion.tokenizer import (
    count_knowledge_tokens,
    tokenizer_fingerprint,
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


def test_manifest_fingerprint_is_stable_sha256() -> None:
    """Returning a host-dependent or non-SHA digest breaks profile reproducibility."""

    digest = tokenizer_fingerprint()

    assert len(digest) == 64
    assert int(digest, 16) >= 0
    assert digest == tokenizer_fingerprint()


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


def test_prepare_script_writes_a_path_free_canonical_manifest(tmp_path: Path) -> None:
    """Preparation copies the packaged canonical resources without network access."""

    script = Path(__file__).resolve().parents[2] / "scripts" / "prepare_knowledge_tokenizer.py"
    output = tmp_path / "tokenizer-data"
    isolated_cache = tmp_path / "empty-tiktoken-cache"
    code = "\n".join(
        (
            "import runpy, socket, sys",
            "def blocked(*args, **kwargs): raise AssertionError('network access attempted')",
            "socket.socket.connect = blocked",
            f"sys.argv = [{str(script)!r}, '--output', {str(output)!r}]",
            f"runpy.run_path({str(script)!r}, run_name='__main__')",
        )
    )
    environment = dict(os.environ)
    environment["TIKTOKEN_CACHE_DIR"] = str(isolated_cache)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile_id"] == "knowledge-cl100k-v1"
    assert manifest["sha256"] == "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    assert str(tmp_path) not in json.dumps(manifest, sort_keys=True)
    assert not isolated_cache.exists()


def test_prepare_script_accepts_the_packaged_resource_directory_as_output(tmp_path: Path) -> None:
    """The Docker build's checked-in output path must not cause a same-file copy."""

    backend = Path(__file__).resolve().parents[2]
    copied_backend = tmp_path / "backend"
    copied_script = copied_backend / "scripts" / "prepare_knowledge_tokenizer.py"
    copied_script.parent.mkdir(parents=True)
    shutil.copyfile(backend / "scripts" / "prepare_knowledge_tokenizer.py", copied_script)
    copied_data = copied_backend / "packages" / "knowledge" / "actweave_knowledge" / "ingestion" / "tokenizer_data"
    shutil.copytree(
        backend / "packages" / "knowledge" / "actweave_knowledge" / "ingestion" / "tokenizer_data",
        copied_data,
    )
    before = {path.name: path.read_bytes() for path in copied_data.iterdir()}

    result = subprocess.run(
        [sys.executable, str(copied_script), "--output", str(copied_data)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name: path.read_bytes() for path in copied_data.iterdir()} == before


@pytest.mark.parametrize("damage", ["vocab_hash", "manifest_canonical", "profile", "fields"])
def test_prepare_script_rejects_invalid_packaged_resources_without_output(tmp_path: Path, damage: str) -> None:
    """Every packaged-resource validation fails before a successful output exists."""

    backend = Path(__file__).resolve().parents[2]
    copied_backend = tmp_path / "backend"
    copied_script = copied_backend / "scripts" / "prepare_knowledge_tokenizer.py"
    copied_script.parent.mkdir(parents=True)
    shutil.copyfile(backend / "scripts" / "prepare_knowledge_tokenizer.py", copied_script)
    copied_data = copied_backend / "packages" / "knowledge" / "actweave_knowledge" / "ingestion" / "tokenizer_data"
    shutil.copytree(
        backend / "packages" / "knowledge" / "actweave_knowledge" / "ingestion" / "tokenizer_data",
        copied_data,
    )
    vocab = copied_data / "cl100k_base.tiktoken"
    manifest_path = copied_data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "vocab_hash":
        vocab.write_bytes(vocab.read_bytes() + b"tampered")
    elif damage == "manifest_canonical":
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    elif damage == "profile":
        manifest["profile_id"] = "wrong-profile"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    else:
        manifest["unexpected"] = True
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    output = tmp_path / "prepared"
    result = subprocess.run(
        [sys.executable, str(copied_script), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()
