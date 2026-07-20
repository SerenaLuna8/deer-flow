from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from scripts.release_acceptance import evidence as evidence_module
from scripts.release_acceptance.evidence import (
    EvidenceWriter,
    ForbiddenEvidenceField,
    UnsafeEvidencePath,
    UnsafeEvidenceRoot,
    manifest_digest,
)

_RUN_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": {"prompt": "synthetic"}},
        {"summary": {"nested": [{"message": "synthetic"}]}},
        {"summary": {"run_id": "synthetic"}},
        {"summary": {"database_url": "synthetic"}},
        {"summary": {"cookie_count": 1}},
        {"summary": {"ciphertext_bytes": 1}},
        {"summary": {"raw_path": "relative.txt"}},
    ],
)
def test_writer_rejects_forbidden_fields_before_creating_any_path(tmp_path: Path, payload: object) -> None:
    writer = EvidenceWriter(tmp_path, acceptance_run_id=_RUN_ID)
    with pytest.raises(ForbiddenEvidenceField):
        writer.write_json("bad.json", payload)
    assert list(tmp_path.iterdir()) == []


def test_writer_rejects_absolute_values_and_unsafe_relative_names(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path, acceptance_run_id=_RUN_ID)
    with pytest.raises(ForbiddenEvidenceField):
        writer.write_json("bad.json", {"summary": {"locator": str(tmp_path)}})
    with pytest.raises(UnsafeEvidencePath):
        writer.write_json("../escape.json", {"status": "failed"})
    with pytest.raises(UnsafeEvidencePath):
        writer.write_json("nested/file.json", {"status": "failed"})
    assert list(tmp_path.iterdir()) == []


def test_writer_rejects_symlink_output_root_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "evidence-link"
    link.symlink_to(target, target_is_directory=True)
    writer = EvidenceWriter(link, acceptance_run_id=_RUN_ID)
    with pytest.raises(UnsafeEvidenceRoot):
        writer.write_json("manifest.json", {"status": "failed"})
    assert list(target.iterdir()) == []


def test_writer_atomically_publishes_canonical_json_and_returns_digest(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path, acceptance_run_id=_RUN_ID)
    artifact = writer.write_json("manifest.json", {"status": "candidate_ready", "counts": {"passed": 2, "failed": 0}})
    published = tmp_path / str(_RUN_ID) / "manifest.json"
    assert artifact.name == "manifest.json"
    assert artifact.sha256 == evidence_module.file_digest(published)
    assert artifact.size_bytes == published.stat().st_size
    assert json.loads(published.read_text(encoding="utf-8")) == {
        "counts": {"failed": 0, "passed": 2},
        "status": "candidate_ready",
    }
    assert [item.name for item in published.parent.iterdir()] == ["manifest.json"]


def test_replace_failure_removes_partial_file_and_empty_run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = EvidenceWriter(tmp_path, acceptance_run_id=_RUN_ID)

    def fail_replace(_source: os.PathLike[str] | str, _target: os.PathLike[str] | str) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(evidence_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        writer.write_json("manifest.json", {"status": "failed"})
    assert list(tmp_path.iterdir()) == []


def test_manifest_digest_excludes_only_its_self_hash() -> None:
    first = {"status": "candidate_ready", "manifest_sha256": "a" * 64, "nested": {"digest": "b" * 64}}
    second = {"status": "candidate_ready", "manifest_sha256": "c" * 64, "nested": {"digest": "b" * 64}}
    changed = {"status": "candidate_ready", "manifest_sha256": "a" * 64, "nested": {"digest": "d" * 64}}
    assert manifest_digest(first) == manifest_digest(second)
    assert manifest_digest(first) != manifest_digest(changed)
