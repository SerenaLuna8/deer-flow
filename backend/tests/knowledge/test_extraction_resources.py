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


def test_pandoc_and_magic_tampering_close_required_parsers(resources, tmp_path, monkeypatch):
    broken = tmp_path / "broken"
    broken.write_bytes(b"wrong binary")
    original = resources._resource_path
    monkeypatch.setattr(resources, "_resource_path", lambda logical: broken if logical.startswith(("pypandoc-binary/", "system/libmagic")) else original(logical))
    assert resources.probe_parser_resources("unstructured.epub") == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert resources.probe_parser_resources("unstructured.eml") == "PARSER_DEPENDENCY_UNAVAILABLE"


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
