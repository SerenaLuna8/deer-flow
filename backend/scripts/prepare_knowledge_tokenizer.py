"""Explicit build-time exporter for the fixed Knowledge Tokenizer resource."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

TOKENIZER_PROFILE_ID = "knowledge-cl100k-v1"
_RESOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "packages" / "knowledge" / "actweave_knowledge" / "ingestion" / "tokenizer_data"
_VOCAB_NAME = "cl100k_base.tiktoken"
_MANIFEST_NAME = "manifest.json"


def _canonical_manifest(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _packaged_resources() -> tuple[bytes, bytes]:
    vocab = (_RESOURCE_DIRECTORY / _VOCAB_NAME).read_bytes()
    manifest_bytes = (_RESOURCE_DIRECTORY / _MANIFEST_NAME).read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"pat_str", "profile_id", "sha256", "special_tokens"}:
        raise ValueError("invalid packaged tokenizer manifest")
    if manifest.get("profile_id") != TOKENIZER_PROFILE_ID:
        raise ValueError("invalid packaged tokenizer profile")
    if not isinstance(manifest.get("pat_str"), str) or not isinstance(manifest.get("special_tokens"), dict):
        raise ValueError("invalid packaged tokenizer configuration")
    expected_hash = manifest.get("sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("invalid packaged tokenizer digest")
    try:
        int(expected_hash, 16)
    except ValueError:
        raise ValueError("invalid packaged tokenizer digest") from None
    if hashlib.sha256(vocab).hexdigest() != expected_hash:
        raise ValueError("packaged tokenizer digest mismatch")
    if _canonical_manifest(manifest) != manifest_bytes:
        raise ValueError("packaged tokenizer manifest is not canonical")
    return vocab, manifest_bytes


def export_tokenizer(output: Path) -> None:
    """Copy the validated packaged vocabulary and manifest to an explicit path."""

    vocab, manifest = _packaged_resources()
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve() == _RESOURCE_DIRECTORY.resolve():
        return
    (output / _VOCAB_NAME).write_bytes(vocab)
    (output / _MANIFEST_NAME).write_bytes(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the fixed local Knowledge Tokenizer")
    parser.add_argument("--output", required=True, type=Path, help="local resource directory to write")
    export_tokenizer(parser.parse_args().output)


if __name__ == "__main__":
    main()
