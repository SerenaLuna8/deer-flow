"""Hash and availability checks before optional parser imports/download fallback."""

from __future__ import annotations

import importlib
import importlib.util
import json

import pytest


@pytest.fixture
def resources():
    name = "actweave_knowledge.extraction.runtime_resources"
    assert importlib.util.find_spec(name) is not None, "verified runtime resources not implemented"
    return importlib.import_module(name)


def test_runtime_manifest_is_stable_safe_and_not_chunk_dependent(resources):
    manifest = resources.runtime_manifest()
    assert resources.runtime_digest() == resources.runtime_digest()
    assert manifest["format_version"] == 1
    names = {x["name"] for x in manifest["packages"]}
    assert {"unstructured", "pypdfium2", "spacy", "en-core-web-sm", "pypandoc-binary", "python-magic"} <= names
    assert "tiktoken" not in names
    assert manifest["resources"]
    assert all(not x["logical_name"].startswith("/") and len(x["sha256"]) == 64 for x in manifest["resources"])
    serialized = json.dumps(manifest)
    assert all(x not in serialized for x in ["/Users/", "timestamp", "knowledge-cl100k", "ChunkProfile"])
    assert any(x["logical_name"].startswith("system/libmagic") for x in manifest["resources"])
    assert any("pandoc" in x["logical_name"] for x in manifest["resources"])


def test_all_registered_parsers_have_verified_resources(resources):
    from actweave_knowledge.extraction.registry import default_registry

    for entry in default_registry().registrations:
        assert resources.probe_parser_resources(entry.extractor_id) is None, entry.extractor_id
        assert entry.dependency_probe() is None, entry.extractor_id
        assert resources.runtime_digest() in entry.extractor_version


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_nlp_resource_missing_or_tampered_closes_capability_before_fallback(resources, tmp_path, monkeypatch, damage):
    path = tmp_path / "model"
    path.write_bytes(b"tampered")
    if damage == "missing":
        path.unlink()
    original = resources._resource_path
    monkeypatch.setattr(resources, "_resource_path", lambda logical: path if logical.startswith("en-core-web-sm/") else original(logical))
    assert resources.probe_parser_resources("unstructured.pptx") == "PARSER_DEPENDENCY_UNAVAILABLE"
    from actweave_knowledge.extraction.registry import default_registry

    entry = default_registry().resolve(datasource_type="file", etl_type="builtin", extension=".pptx")
    assert entry.dependency_probe() == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_pandoc_and_magic_tampering_close_required_parsers(resources, tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    broken.write_bytes(b"wrong binary")
    original = resources._resource_path
    monkeypatch.setattr(resources, "_resource_path", lambda logical: broken if logical.startswith(("pypandoc-binary/", "system/libmagic")) else original(logical))
    assert resources.probe_parser_resources("unstructured.epub") == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert resources.probe_parser_resources("unstructured.eml") == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_resource_changes_change_runtime_digest(resources, tmp_path, monkeypatch):
    before = resources.runtime_digest()
    replacement = tmp_path / "changed"
    replacement.write_bytes(b"changed model")
    original = resources._resource_path
    monkeypatch.setattr(resources, "_resource_path", lambda logical: replacement if logical.startswith("en-core-web-sm/") else original(logical))
    assert resources.runtime_digest() != before


def test_offline_loader_never_calls_installer(resources, monkeypatch):
    resources.prepare_local_parser("unstructured.pptx")
    import spacy
    import unstructured.nlp.tokenize as tokenize
    from actweave_knowledge.extraction.contracts import ExtractionError

    monkeypatch.setattr(tokenize, "_install_spacy_model", lambda: pytest.fail("runtime installer reached"))

    def unavailable(*args, **kwargs):
        raise OSError("model absent")

    monkeypatch.setattr(spacy, "load", unavailable)
    with pytest.raises(ExtractionError) as error:
        tokenize._load_spacy_model()
    assert error.value.reason_code == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_unregistered_platform_is_unavailable(resources, monkeypatch):
    monkeypatch.setattr(resources, "_platform_key", lambda: "unverified-platform")
    assert resources.probe_parser_resources("unstructured.pptx") == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_missing_model_aborts_before_partition_import(resources, tmp_path, monkeypatch):
    import builtins

    from actweave_knowledge.extraction.contracts import ExtractionError
    from actweave_knowledge.extraction.unstructured_local.unstructured_pptx_extractor import UnstructuredPPTXExtractor
    from parsing_test_helpers import make_context, make_setting

    original_path = resources._resource_path
    missing = tmp_path / "missing-model"
    monkeypatch.setattr(resources, "_resource_path", lambda logical: missing if logical.startswith("en-core-web-sm/") else original_path(logical))
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("unstructured.partition"):
            pytest.fail("partition imported before resource preflight")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    path = tmp_path / "slides.pptx"
    path.write_bytes(b"not opened by adapter")
    with pytest.raises(ExtractionError) as error:
        UnstructuredPPTXExtractor().extract(make_setting(path), make_context(tmp_path / "work"))
    assert error.value.reason_code == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_build_manifest_is_reproducible(resources, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parents[2] / "scripts/build_extraction_resources.py"
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    for target in (first, second):
        subprocess.run([sys.executable, str(script), "--output", str(target)], check=True, capture_output=True, timeout=30)
    assert first.read_bytes() == second.read_bytes()
    generated = json.loads(first.read_bytes())
    locked = json.loads(resources._LOCK_PATH.read_text())
    platform = resources._platform_key()
    assert generated["platforms"] == {platform: locked["platforms"][platform]}
    assert generated["native_build_probes"] == {platform: locked["native_build_probes"][platform]}


def test_build_manifest_update_preserves_other_platform_entries(resources, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parents[2] / "scripts/build_extraction_resources.py"
    target = tmp_path / "resources.lock.json"
    other_manifest = {"packages": [{"name": "kept", "version": "1"}], "resources": []}
    other_probe = {"libmagic_api_version": 1, "pandoc": "pandoc kept"}
    target.write_text(
        json.dumps(
            {
                "format_version": 1,
                "platforms": {"other-platform": other_manifest},
                "native_build_probes": {"other-platform": other_probe},
            }
        ),
        encoding="utf-8",
    )

    subprocess.run([sys.executable, str(script), "--output", str(target)], check=True, capture_output=True, timeout=30)

    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["platforms"]["other-platform"] == other_manifest
    assert updated["native_build_probes"]["other-platform"] == other_probe
    assert resources._platform_key() in updated["platforms"]


@pytest.mark.parametrize("payload", [{}, {"format_version": 1, "platforms": {"darwin-arm64": {}}}, {"format_version": 1, "platforms": []}, {"format_version": 1, "platforms": {"darwin-arm64": {"packages": [], "resources": None}}}])
def test_corrupt_lock_fails_closed_with_safe_reason(resources, tmp_path, monkeypatch, payload):
    path = tmp_path / "resources.lock.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(resources, "_LOCK_PATH", path)
    assert resources.probe_parser_resources("unstructured.pptx") == "PARSER_DEPENDENCY_UNAVAILABLE"


def test_loaded_libmagic_identity_does_not_depend_on_absolute_loader_name(resources, monkeypatch):
    import magic

    # Linux ctypes commonly records a soname rather than an absolute pathname.
    # Keep the real library and symbol; only model that loader metadata shape.
    monkeypatch.setattr(magic.libmagic, "_name", "libmagic.so.1")
    resources.prepare_local_parser("unstructured.pptx")
